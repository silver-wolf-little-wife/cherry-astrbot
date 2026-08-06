# SPDX-License-Identifier: AGPL-3.0-only
"""Cherry Remote —— AstrBot 远程操控连接器。

纯连接器：桥接 AstrBot（B端）与远程电脑上的 cherry-remote-app（C端）。
- 内嵌 aiohttp WebSocket 服务端，接受 C 端 App 主动外连（穿透 NAT）。
- 注册 FunctionTool，使 AstrBot Agent 可将用户需求转为远程指令下发。
- 回收执行结果回传 Agent 研判后，由 AstrBot 回复原会话。
"""

import asyncio
import base64
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import FunctionTool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message import MessageChain
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.agent.tool import ToolExecResult
except ImportError:  # pragma: no cover
    ToolExecResult = str  # type: ignore

from .ws_server import RemoteWsServer


def _get_plugin_data_dir() -> Path:
    """获取插件数据目录：data/plugin_data/cherry_remote/。"""
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    d = Path(get_astrbot_data_path()) / "plugin_data" / "cherry_remote"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_screenshot_image(data: dict) -> str:
    """把 C 端返回的 base64 截图解码并存到 B 端本地，返回文件路径。"""
    img_bytes = base64.b64decode(data["image"])
    shots = _get_plugin_data_dir() / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    fname = f"shot_{int(time.time())}_{uuid.uuid4().hex[:6]}.png"
    path = shots / fname
    path.write_bytes(img_bytes)
    return str(path)


@dataclass
class RemoteExecTool(FunctionTool):
    """远程执行 shell 命令。"""

    name: str = "remote_exec"
    description: str = (
        "在远程电脑（C端）上执行一条 shell 命令并返回 stdout/stderr 与退出码。"
        "用于查看/操作远程电脑上的程序、文件与系统。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 shell 命令。"},
                "timeout": {
                    "type": "integer",
                    "description": "超时秒数，默认 30。",
                    "default": 30,
                },
                "cwd": {"type": "string", "description": "工作目录，可选。"},
            },
            "required": ["command"],
        }
    )

    async def call(self, context: Any, **kwargs) -> ToolExecResult:
        server: RemoteWsServer | None = getattr(self, "_server", None)
        if server is None:
            return json.dumps({"ok": False, "error": "连接器尚未初始化"}, ensure_ascii=False)
        timeout = int(kwargs.get("timeout") or 30)
        params = {"command": kwargs["command"], "timeout": timeout}
        if kwargs.get("cwd"):
            params["cwd"] = kwargs["cwd"]
        try:
            resp = await server.send_command("exec", params, timeout=timeout + 10)
            return json.dumps(resp, ensure_ascii=False, default=str)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


@dataclass
class RemoteSysInfoTool(FunctionTool):
    """获取远程电脑系统信息。"""

    name: str = "remote_sysinfo"
    description: str = (
        "获取远程电脑（C端）的系统信息，包括 CPU/内存/磁盘使用率、主机名、操作系统等。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
        }
    )

    async def call(self, context: Any, **kwargs) -> ToolExecResult:
        server: RemoteWsServer | None = getattr(self, "_server", None)
        if server is None:
            return json.dumps({"ok": False, "error": "连接器尚未初始化"}, ensure_ascii=False)
        try:
            resp = await server.send_command("sys", {}, timeout=30)
            return json.dumps(resp, ensure_ascii=False, default=str)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


@dataclass
class RemotePingTool(FunctionTool):
    """探测远程电脑连通性。"""

    name: str = "remote_ping"
    description: str = "检测远程电脑（C端）是否在线，返回 pong。"
    parameters: dict = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )

    async def call(self, context: Any, **kwargs) -> ToolExecResult:
        server: RemoteWsServer | None = getattr(self, "_server", None)
        if server is None:
            return json.dumps({"ok": False, "error": "连接器尚未初始化"}, ensure_ascii=False)
        try:
            resp = await server.send_command("ping", {}, timeout=15)
            return json.dumps(resp, ensure_ascii=False, default=str)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


@dataclass
class RemoteFileTool(FunctionTool):
    """远程文件操作。"""

    name: str = "remote_file"
    description: str = (
        "对远程电脑（C端）进行文件操作。action 取值：list(列目录)、read(读文件)、"
        "write(写文件)、copy(复制)、delete(删除)、info(文件信息)。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "read", "write", "copy", "delete", "info"],
                    "description": "要执行的文件操作。",
                },
                "path": {"type": "string", "description": "源文件/目录路径。"},
                "dest": {"type": "string", "description": "目标路径（copy 时必填）。"},
                "content": {"type": "string", "description": "写入内容（write 时使用）。"},
                "recursive": {
                    "type": "boolean",
                    "description": "list 时是否递归列出，默认 false。",
                },
            },
            "required": ["action", "path"],
        }
    )

    async def call(self, context: Any, **kwargs) -> ToolExecResult:
        server: RemoteWsServer | None = getattr(self, "_server", None)
        if server is None:
            return json.dumps({"ok": False, "error": "连接器尚未初始化"}, ensure_ascii=False)
        params = {k: v for k, v in kwargs.items() if v is not None}
        try:
            resp = await server.send_command("file", params, timeout=60)
            return json.dumps(resp, ensure_ascii=False, default=str)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


@dataclass
class RemoteAppTool(FunctionTool):
    """远程启动/结束应用。"""

    name: str = "remote_app"
    description: str = "在远程电脑（C端）上启动或结束应用程序。action：launch(启动)/terminate(结束)。"
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["launch", "terminate"],
                    "description": "launch 启动 / terminate 结束。",
                },
                "name": {
                    "type": "string",
                    "description": "应用名或路径（terminate 时按进程名模糊匹配）。",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "启动参数（launch 时可选）。",
                },
                "pid": {"type": "integer", "description": "按 PID 结束进程（terminate 时可选）。"},
            },
            "required": ["action"],
        }
    )

    async def call(self, context: Any, **kwargs) -> ToolExecResult:
        server: RemoteWsServer | None = getattr(self, "_server", None)
        if server is None:
            return json.dumps({"ok": False, "error": "连接器尚未初始化"}, ensure_ascii=False)
        params = {k: v for k, v in kwargs.items() if v is not None}
        try:
            resp = await server.send_command("app", params, timeout=30)
            return json.dumps(resp, ensure_ascii=False, default=str)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


@dataclass
class RemoteScreenshotTool(FunctionTool):
    """远程截屏。"""

    name: str = "remote_screenshot"
    description: str = (
        "截取远程电脑（C端）的完整屏幕（含所有显示器），保存为服务器本地 PNG 文件，"
        "并尝试直接把图片发送给用户。"
    )
    parameters: dict = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )

    async def call(self, context: Any, **kwargs) -> ToolExecResult:
        server: RemoteWsServer | None = getattr(self, "_server", None)
        if server is None:
            return json.dumps({"ok": False, "error": "连接器尚未初始化"}, ensure_ascii=False)
        try:
            resp = await server.send_command("screenshot", {}, timeout=30)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
        if not resp.get("ok"):
            return json.dumps({"ok": False, "error": resp.get("error")}, ensure_ascii=False)

        data = resp["data"]
        try:
            path = _save_screenshot_image(data)
        except Exception as e:
            return json.dumps({"ok": False, "error": f"保存截图失败: {e}"}, ensure_ascii=False)

        sent = False
        try:
            inner = getattr(context, "context", None)
            star_ctx = getattr(inner, "context", None)
            event = getattr(inner, "event", None)
            if star_ctx is not None and event is not None:
                chain = MessageChain().file_image(path)
                await star_ctx.send_message(event.unified_msg_origin, chain)
                sent = True
        except Exception as e:  # noqa: BLE001 —— 直发失败则回退为返回路径
            logger.warning(f"截图直接发送失败，改为返回路径: {e}")

        return json.dumps(
            {
                "ok": True,
                "sent_to_user": sent,
                "path": path,
                "width": data.get("width"),
                "height": data.get("height"),
                "size": data.get("size"),
            },
            ensure_ascii=False,
        )


@register(
    "astrbot_plugin_cherry_remote",
    "littlewifeofsilverwolf",
    "远程操控连接器：桥接 AstrBot 与远程电脑 App",
    "0.1.0",
)
class CherryRemote(Star):
    """Cherry Remote —— 远程操控连接器。

    桥接 AstrBot 与远程电脑上的 cherry-remote-app：
    - 建立 B 端 WebSocket 服务，接受 C 端 App 主动外连（穿透 NAT）
    - 注册 FunctionTool，使 AstrBot Agent 可将用户需求转为远程指令下发
    - 回收执行结果回传 Agent 研判，最终回复发回原会话
    """

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.server: RemoteWsServer | None = None
        self._server_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        """启动 WebSocket 服务端并注册 FunctionTool。"""
        port = int(self.config.get("ws_port", 8765))
        token = str(self.config.get("auth_token", ""))
        heartbeat_timeout = int(self.config.get("heartbeat_timeout", 60))

        self.server = RemoteWsServer(port=port, token=token, heartbeat_timeout=heartbeat_timeout)
        self._server_task = asyncio.create_task(self.server.start())

        tools = self._build_tools()
        if tools:
            self.context.add_llm_tools(*tools)
        logger.info("Cherry Remote 初始化完成。")

    def _build_tools(self) -> list[FunctionTool]:
        if self.server is None:
            return []
        built: list[FunctionTool] = []
        for tool_cls in (
            RemoteExecTool,
            RemoteSysInfoTool,
            RemotePingTool,
            RemoteFileTool,
            RemoteAppTool,
            RemoteScreenshotTool,
        ):
            tool = tool_cls()
            tool._server = self.server  # type: ignore[attr-defined]
            built.append(tool)
        return built

    @filter.command("cherry")
    async def cherry(self, event: AstrMessageEvent):
        """Cherry Remote 状态查询。发送 `/cherry` 检查插件与设备状态。"""
        if self.server is None:
            yield event.plain_result("Cherry Remote 尚未初始化。")
            return
        devices = self.server.device_summary()
        if not devices:
            yield event.plain_result(
                "Cherry Remote 已就绪，但暂无远程设备在线。请先启动 C 端 cherry-remote-app 并接入。"
            )
            return
        lines = [f"- {d['device_id']}（session {d['session_id'][:8]}）" for d in devices]
        yield event.plain_result("Cherry Remote 已就绪，在线设备：\n" + "\n".join(lines))

    @filter.command("screenshot")
    async def screenshot(self, event: AstrMessageEvent):
        """截取 C 端完整屏幕并直接以图片发送。"""
        if self.server is None:
            yield event.plain_result("Cherry Remote 尚未初始化。")
            return
        try:
            resp = await self.server.send_command("screenshot", {}, timeout=30)
        except Exception as e:
            yield event.plain_result(f"截屏失败: {e}")
            return
        if not resp.get("ok"):
            yield event.plain_result(f"截屏失败: {resp.get('error')}")
            return
        data = resp["data"]
        try:
            path = _save_screenshot_image(data)
        except Exception as e:
            yield event.plain_result(f"截屏成功但保存失败: {e}")
            return
        size_kb = (data.get("size") or 0) // 1024
        yield event.chain_result(
            [
                Comp.Image.fromFileSystem(path),
                Comp.Plain(f"截图 {data.get('width')}x{data.get('height')}（{size_kb}KB）"),
            ]
        )

    async def terminate(self) -> None:
        """插件卸载/停用时：停止服务端，释放资源。"""
        if self.server:
            await self.server.stop()
        logger.info("Cherry Remote 连接器已停止。")
