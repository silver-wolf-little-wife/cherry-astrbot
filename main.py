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
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

try:
    from astrbot.core.agent.tool import ToolExecResult
except ImportError:  # pragma: no cover
    ToolExecResult = str  # type: ignore

from .ws_server import RemoteWsServer

try:  # 多模态图片回传：AstrBot 会把 ImageContent 交给多模态模型（mcp 为 astrbot 依赖）
    from mcp.types import CallToolResult, ImageContent, TextContent
except ImportError:  # pragma: no cover —— 环境无 mcp 时降级为纯文本返回
    CallToolResult = None  # type: ignore[assignment]
    ImageContent = None  # type: ignore[assignment]
    TextContent = None  # type: ignore[assignment]


# C 端 camera 错误码 → 给用户看的人话提示
_CAMERA_ERROR_HINTS = {
    "CameraDisabled": "该电脑未开启摄像头功能（需在 C 端 config.yaml 设置 camera.enabled: true）",
    "CameraBackendUnavailable": "该电脑缺少摄像头采集组件（未安装 OpenCV，也没有 ffmpeg）",
    "CameraNotFound": "没找到摄像头设备",
    "CameraOpenFailed": "摄像头无法打开：可能被其他程序占用，或被系统隐私设置禁止桌面应用访问相机",
    "CameraBusy": "上一次拍摄还没结束，请稍后再试",
    "CameraNoInteractiveSession": "C 端运行在服务会话且当前没有用户登录，无法访问摄像头",
    "CameraCaptureFailed": "取帧失败：请检查摄像头是否被遮挡或设备异常",
    "CameraRateLimited": "拍摄太频繁（C 端限流），请稍后再试",
    "NotImplementedError": "C 端版本过低，不支持 camera 指令（请升级 cherry-remote-app 到 v1.4.0+）",
}

_IMAGE_EXTS = {
    "jpeg": ".jpg",
    "jpg": ".jpg",
    "png": ".png",
    "webp": ".webp",
    "bmp": ".bmp",
    "gif": ".gif",
}


def _get_plugin_data_dir() -> Path:
    """获取插件数据目录：data/plugin_data/cherry_remote/。"""
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    d = Path(get_astrbot_data_path()) / "plugin_data" / "cherry_remote"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_image(data: dict, subdir: str = "screenshots", default_ext: str = ".png", keep: int | None = None) -> str:
    """把 C 端返回的 base64 图片解码并存到 B 端本地，返回文件路径。

    subdir：screenshots（截屏）/ camera（摄像头照片）；
    keep：非空时只保留该目录下最近 keep 张（自动清理旧图，避免磁盘无限增长）。
    """
    img_bytes = base64.b64decode(data["image"])
    ext = _IMAGE_EXTS.get(str(data.get("format") or "").lower(), default_ext)
    directory = _get_plugin_data_dir() / subdir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"shot_{int(time.time())}_{uuid.uuid4().hex[:6]}{ext}"
    path.write_bytes(img_bytes)
    if keep and keep > 0:
        _prune_images(directory, keep)
    return str(path)


def _prune_images(directory: Path, keep: int) -> None:
    """只保留目录下最新的 keep 张图（按修改时间）。"""
    try:
        files = sorted(
            (p for p in directory.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in files[keep:]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001 —— 清理失败不影响主流程
        logger.warning(f"清理旧图片失败（忽略）: {e}")


def _save_screenshot_image(data: dict) -> str:
    """把 C 端返回的 base64 截图解码并存到 B 端本地，返回文件路径。"""
    return _save_image(data, subdir="screenshots", default_ext=".png")


def _camera_error_hint(error: Any) -> str:
    """把 C 端的 camera 错误码翻译成人话提示。"""
    if isinstance(error, dict):
        code = str(error.get("code") or "")
        message = str(error.get("message") or "")
        hint = _CAMERA_ERROR_HINTS.get(code)
        if hint:
            return f"{hint}（{code}: {message}）" if message else hint
        return f"{code}: {message}" if code else str(error)
    return str(error)


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


async def _send_image_to_user(context: Any, path: str) -> bool:
    """把本地图片直接发给触发本次工具调用的会话，返回是否发送成功。"""
    try:
        inner = getattr(context, "context", None)
        star_ctx = getattr(inner, "context", None)
        event = getattr(inner, "event", None)
        if star_ctx is not None and event is not None:
            await star_ctx.send_message(event.unified_msg_origin, MessageChain().file_image(path))
            return True
    except Exception as e:  # noqa: BLE001 —— 直发失败则回退为返回路径
        logger.warning(f"照片直接发送失败，改为返回路径: {e}")
    return False


def _persist_pulled(resp: dict) -> str:
    """把拉取结果落盘，返回本地路径（流式模式直接用落盘路径，单帧模式需写盘）。"""
    if resp.get("mode") == "stream":
        return resp["local_path"]
    pulls = _get_plugin_data_dir() / "pulls"
    pulls.mkdir(parents=True, exist_ok=True)
    fname = f"{int(time.time())}_{uuid.uuid4().hex[:6]}_{resp.get('name') or 'file'}"
    path = pulls / fname
    content = resp.get("content")
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(str(content), encoding="utf-8")
    return str(path)


def _pulled_chain(local_path: str, name: str, size_kb: int) -> list:
    """按文件类型构造发送链：图片直显，其余作为附件。"""
    ext = Path(local_path).suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        return [
            Comp.Image.fromFileSystem(local_path),
            Comp.Plain(f"{name}（{size_kb}KB）"),
        ]
    return [
        Comp.File(name=name, file=local_path),
        Comp.Plain(f"{name}（{size_kb}KB）"),
    ]


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
                "device_id": {
                    "type": "string",
                    "description": "目标设备 id（多设备在线时指定；仅一台时可不填）。",
                },
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
            resp = await server.send_command(
                "exec", params, device_id=kwargs.get("device_id"), timeout=timeout + 10
            )
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
            "properties": {
                "device_id": {
                    "type": "string",
                    "description": "目标设备 id（多设备在线时指定）。",
                },
            },
        }
    )

    async def call(self, context: Any, **kwargs) -> ToolExecResult:
        server: RemoteWsServer | None = getattr(self, "_server", None)
        if server is None:
            return json.dumps({"ok": False, "error": "连接器尚未初始化"}, ensure_ascii=False)
        try:
            resp = await server.send_command(
                "sys", {}, device_id=kwargs.get("device_id"), timeout=30
            )
            return json.dumps(resp, ensure_ascii=False, default=str)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


@dataclass
class RemotePingTool(FunctionTool):
    """探测远程电脑连通性。"""

    name: str = "remote_ping"
    description: str = "检测远程电脑（C端）是否在线，返回 pong。"
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "device_id": {
                    "type": "string",
                    "description": "目标设备 id（多设备在线时指定）。",
                },
            },
        }
    )

    async def call(self, context: Any, **kwargs) -> ToolExecResult:
        server: RemoteWsServer | None = getattr(self, "_server", None)
        if server is None:
            return json.dumps({"ok": False, "error": "连接器尚未初始化"}, ensure_ascii=False)
        try:
            resp = await server.send_command(
                "ping", {}, device_id=kwargs.get("device_id"), timeout=15
            )
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
                "device_id": {
                    "type": "string",
                    "description": "目标设备 id（多设备在线时指定）。",
                },
            },
            "required": ["action", "path"],
        }
    )

    async def call(self, context: Any, **kwargs) -> ToolExecResult:
        server: RemoteWsServer | None = getattr(self, "_server", None)
        if server is None:
            return json.dumps({"ok": False, "error": "连接器尚未初始化"}, ensure_ascii=False)
        params = {k: v for k, v in kwargs.items() if v is not None and k != "device_id"}
        try:
            resp = await server.send_command(
                "file", params, device_id=kwargs.get("device_id"), timeout=60
            )
            return json.dumps(resp, ensure_ascii=False, default=str)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


@dataclass
class RemoteAppTool(FunctionTool):
    """远程启动/结束应用。"""

    name: str = "remote_app"
    description: str = (
        "在远程电脑（C端）上启动/结束/搜索应用程序。"
        "action：launch(启动)/terminate(结束)/search(在 exe 索引中搜索应用路径)。"
        "search 同时匹配 exe 文件名与产品名/文件说明（显示名，如“米哈游启动器”→HYP.exe），"
        "命中产品名时返回 matched_on=product 与 product 字段。若不确定应用名，先 search 查 exe 索引。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["launch", "terminate", "search"],
                    "description": "launch 启动 / terminate 结束 / search 搜索 exe 索引。",
                },
                "name": {
                    "type": "string",
                    "description": "应用名或完整路径（launch 用；terminate 时按进程名模糊匹配）。",
                },
                "query": {
                    "type": "string",
                    "description": "search 时的应用名关键字（不填则返回索引前 50 条）。",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "启动参数（launch 时可选）。",
                },
                "pid": {"type": "integer", "description": "按 PID 结束进程（terminate 时可选）。"},
                "device_id": {
                    "type": "string",
                    "description": "目标设备 id（多设备在线时指定）。",
                },
            },
            "required": ["action"],
        }
    )

    async def call(self, context: Any, **kwargs) -> ToolExecResult:
        server: RemoteWsServer | None = getattr(self, "_server", None)
        if server is None:
            return json.dumps({"ok": False, "error": "连接器尚未初始化"}, ensure_ascii=False)
        params = {k: v for k, v in kwargs.items() if v is not None and k != "device_id"}
        try:
            resp = await server.send_command(
                "app", params, device_id=kwargs.get("device_id"), timeout=30
            )
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
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "device_id": {
                    "type": "string",
                    "description": "目标设备 id（多设备在线时指定）。",
                },
            },
        }
    )

    async def call(self, context: Any, **kwargs) -> ToolExecResult:
        server: RemoteWsServer | None = getattr(self, "_server", None)
        if server is None:
            return json.dumps({"ok": False, "error": "连接器尚未初始化"}, ensure_ascii=False)
        try:
            resp = await server.send_command(
                "screenshot", {}, device_id=kwargs.get("device_id"), timeout=30
            )
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
        if not resp.get("ok"):
            return json.dumps({"ok": False, "error": resp.get("error")}, ensure_ascii=False)

        data = resp["data"]
        try:
            path = _save_screenshot_image(data)
        except Exception as e:
            return json.dumps({"ok": False, "error": f"保存截图失败: {e}"}, ensure_ascii=False)

        sent = await _send_image_to_user(context, path)

        return _json(
            {
                "ok": True,
                "sent_to_user": sent,
                "path": path,
                "width": data.get("width"),
                "height": data.get("height"),
                "size": data.get("size"),
            }
        )


@dataclass
class RemotePullFileTool(FunctionTool):
    """从远程电脑拉取文件到本地并直接发送。"""

    name: str = "remote_pull_file"
    description: str = (
        "从远程电脑（C端）拉取文件到本地并直接发送给用户（图片直显，其余作为附件发送）。"
        "支持任意类型与大小，自动选择单帧或流式传输。参数 path 为远程电脑上的文件完整路径。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "远程电脑上的文件完整路径，如 D:\\xx\\file.zip",
                },
                "device_id": {
                    "type": "string",
                    "description": "目标设备 id（多设备在线时指定）。",
                },
            },
            "required": ["path"],
        }
    )

    async def call(self, context: Any, **kwargs) -> ToolExecResult:
        server: RemoteWsServer | None = getattr(self, "_server", None)
        if server is None:
            return json.dumps({"ok": False, "error": "连接器尚未初始化"}, ensure_ascii=False)
        try:
            resp = await server.pull_file(
                kwargs["path"], device_id=kwargs.get("device_id"), timeout=600
            )
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
        if not resp.get("ok"):
            return json.dumps({"ok": False, "error": resp.get("error")}, ensure_ascii=False)
        try:
            local_path = _persist_pulled(resp)
        except Exception as e:
            return json.dumps({"ok": False, "error": f"保存文件失败: {e}"}, ensure_ascii=False)

        sent = False
        try:
            inner = getattr(context, "context", None)
            star_ctx = getattr(inner, "context", None)
            event = getattr(inner, "event", None)
            if star_ctx is not None and event is not None:
                chain = MessageChain(
                    chain=_pulled_chain(
                        local_path, resp.get("name"), (resp.get("size") or 0) // 1024
                    )
                )
                await star_ctx.send_message(event.unified_msg_origin, chain)
                sent = True
        except Exception as e:  # noqa: BLE001 —— 直发失败则回退为返回路径
            logger.warning(f"文件直接发送失败，改为返回路径: {e}")

        return json.dumps(
            {
                "ok": True,
                "sent_to_user": sent,
                "mode": resp.get("mode"),
                "name": resp.get("name"),
                "size": resp.get("size"),
                "local_path": local_path,
            },
            ensure_ascii=False,
        )


@dataclass
class RemoteCameraTool(FunctionTool):
    """用远程电脑摄像头拍照（看屏幕之外的物理环境）。"""

    name: str = "remote_camera"
    description: str = (
        "用远程电脑（C端）的摄像头拍摄一张现场照片，用于了解电脑周围的环境"
        "（房间、设备指示灯、纸质材料等）。"
        "适合回答「家里现在什么情况」「桌上有什么」这类需要看物理环境的问题；"
        "只看屏幕内容请用 remote_screenshot，取文件内容请用 remote_pull_file。"
        "拍照会点亮摄像头指示灯，且受 C 端限流（默认 5 秒冷却、每小时 60 次）。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "device": {
                    "type": "integer",
                    "description": "摄像头 index，默认 0；多摄像头时先用 remote_camera_list 查询。",
                },
                "width": {"type": "integer", "description": "照片宽度，默认 1280。"},
                "height": {"type": "integer", "description": "照片高度，默认 720。"},
                "burst": {
                    "type": "integer",
                    "description": "连拍帧数（1~5），多帧时自动选最清晰的一帧，默认 1。",
                },
                "mirror": {"type": "boolean", "description": "是否左右镜像，默认 false。"},
                "reason": {
                    "type": "string",
                    "description": "调用原因，写入 C 端审计日志便于事后核对，建议填写。",
                },
                "device_id": {
                    "type": "string",
                    "description": "目标设备 id（多设备在线时指定）。",
                },
            },
        }
    )

    async def call(self, context: Any, **kwargs) -> ToolExecResult:
        server: RemoteWsServer | None = getattr(self, "_server", None)
        if server is None:
            return _json({"ok": False, "error": "连接器尚未初始化"})

        params = {
            k: v
            for k, v in kwargs.items()
            if v is not None and k not in ("device_id", "send_to_user")
        }
        try:
            resp = await server.send_command(
                "camera", params, device_id=kwargs.get("device_id"), timeout=45
            )
        except Exception as e:
            return _json({"ok": False, "error": str(e)})
        if not resp.get("ok"):
            return _json({"ok": False, "error": _camera_error_hint(resp.get("error"))})

        data = resp["data"]
        mode = str(getattr(self, "_camera_mode", "vision") or "vision").lower()
        keep = int(getattr(self, "_camera_keep", 50) or 0)
        try:
            path = _save_image(data, subdir="camera", default_ext=".jpg", keep=keep)
        except Exception as e:
            return _json({"ok": False, "error": f"保存照片失败: {e}"})

        device = data.get("device") or {}
        sent = False
        if mode in ("forward", "both") and kwargs.get("send_to_user", True):
            sent = await _send_image_to_user(context, path)

        if mode in ("vision", "both") and CallToolResult is not None:
            text = (
                f"已在远程电脑上拍摄一张照片：{data.get('width')}x{data.get('height')}，"
                f"{data.get('size')} 字节，设备 index={device.get('index')}"
                f"（{device.get('system_name') or '未识别名称'}），"
                f"拍摄时间 {data.get('captured_at')}，本地留存路径 {path}。"
                "请查看图片内容并据此回答用户。"
                + ("照片已直接发送给对方。" if sent else "")
            )
            mime = "image/png" if str(data.get("format")).lower() == "png" else "image/jpeg"
            return CallToolResult(
                content=[
                    TextContent(type="text", text=text),
                    ImageContent(type="image", data=data["image"], mimeType=mime),
                ]
            )

        return _json(
            {
                "ok": True,
                "sent_to_user": sent,
                "path": path,
                "width": data.get("width"),
                "height": data.get("height"),
                "size": data.get("size"),
                "device": device,
                "captured_at": data.get("captured_at"),
                "source": data.get("source"),
                "note": "当前 camera_mode 未把照片送入模型上下文，仅落盘/直发",
            }
        )


@dataclass
class RemoteCameraListTool(FunctionTool):
    """列出远程电脑的摄像头设备。"""

    name: str = "remote_camera_list"
    description: str = (
        "列出远程电脑（C端）的摄像头设备（index、分辨率、设备名）。"
        "拍照前可用它确认设备；若提示未开启摄像头功能（CameraDisabled），"
        "说明该电脑的 C 端配置里 camera.enabled 仍为 false。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "device_id": {
                    "type": "string",
                    "description": "目标设备 id（多设备在线时指定）。",
                },
            },
        }
    )

    async def call(self, context: Any, **kwargs) -> ToolExecResult:
        server: RemoteWsServer | None = getattr(self, "_server", None)
        if server is None:
            return _json({"ok": False, "error": "连接器尚未初始化"})
        try:
            resp = await server.send_command(
                "camera", {"action": "list"}, device_id=kwargs.get("device_id"), timeout=45
            )
        except Exception as e:
            return _json({"ok": False, "error": str(e)})
        if not resp.get("ok"):
            return _json({"ok": False, "error": _camera_error_hint(resp.get("error"))})
        return _json(resp.get("data"))


@register(
    "astrbot_plugin_cherry_remote",
    "littlewifeofsilverwolf",
    "远程操控连接器：桥接 AstrBot 与远程电脑 App",
    "1.3.0",
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

        self.server = RemoteWsServer(
            port=port,
            token=token,
            heartbeat_timeout=heartbeat_timeout,
            pull_threshold=int(self.config.get("pull_threshold", 8 * 1024 * 1024)),
            max_pull_size=int(self.config.get("max_pull_size", 200 * 1024 * 1024)),
            pull_dir=str(_get_plugin_data_dir() / "pulls"),
        )
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
            RemotePullFileTool,
        ):
            tool = tool_cls()
            tool._server = self.server  # type: ignore[attr-defined]
            built.append(tool)

        # 摄像头工具：可由配置整体关闭（默认开启；C 端仍需 camera.enabled=true 才真正可用）
        if bool(self.config.get("camera_enabled", True)):
            camera_mode = str(self.config.get("camera_mode", "vision") or "vision")
            camera_keep = int(self.config.get("camera_keep", 50) or 0)
            for tool_cls in (RemoteCameraTool, RemoteCameraListTool):
                tool = tool_cls()
                tool._server = self.server  # type: ignore[attr-defined]
                tool._camera_mode = camera_mode  # type: ignore[attr-defined]
                tool._camera_keep = camera_keep  # type: ignore[attr-defined]
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

    @filter.command("devices")
    async def devices(self, event: AstrMessageEvent):
        """列出当前在线设备。"""
        if self.server is None:
            yield event.plain_result("Cherry Remote 尚未初始化。")
            return
        devices = self.server.device_summary()
        if not devices:
            yield event.plain_result("暂无在线设备。")
            return
        lines = [f"- {d['device_id']}（session {d['session_id'][:8]}）" for d in devices]
        yield event.plain_result("在线设备：\n" + "\n".join(lines))

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

    @filter.command("camera")
    async def camera(self, event: AstrMessageEvent):
        """用 C 端摄像头拍一张现场照片并直接发送。用法：/camera [摄像头index]"""
        if self.server is None:
            yield event.plain_result("Cherry Remote 尚未初始化。")
            return
        parts = event.get_message_str().strip().split(maxsplit=1)
        device = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip().isdigit() else 0
        try:
            resp = await self.server.send_command(
                "camera",
                {"device": device, "reason": "用户手动 /camera 指令"},
                timeout=45,
            )
        except Exception as e:
            yield event.plain_result(f"拍照失败: {e}")
            return
        if not resp.get("ok"):
            yield event.plain_result(f"拍照失败: {_camera_error_hint(resp.get('error'))}")
            return
        data = resp["data"]
        try:
            path = _save_image(
                data,
                subdir="camera",
                default_ext=".jpg",
                keep=int(self.config.get("camera_keep", 50) or 0),
            )
        except Exception as e:
            yield event.plain_result(f"拍照成功但保存失败: {e}")
            return
        size_kb = (data.get("size") or 0) // 1024
        yield event.chain_result(
            [
                Comp.Image.fromFileSystem(path),
                Comp.Plain(
                    f"摄像头照片 {data.get('width')}x{data.get('height')}"
                    f"（{size_kb}KB，设备 index={device}，来源 {data.get('source')}）"
                ),
            ]
        )

    @filter.command("pull")
    async def pull(self, event: AstrMessageEvent):
        """从 C 端拉取文件并直接发送。用法：/pull <远程文件路径>（路径含空格无需引号）"""
        if self.server is None:
            yield event.plain_result("Cherry Remote 尚未初始化。")
            return
        msg = event.get_message_str().strip()
        parts = msg.split(maxsplit=1)
        path = parts[1].strip().strip('\"') if len(parts) > 1 else ""
        if not path:
            yield event.plain_result(
                "用法：/pull <远程文件路径>，例如 /pull D:\\temp\\report.pdf"
            )
            return
        try:
            resp = await self.server.pull_file(path, timeout=600)
        except Exception as e:
            yield event.plain_result(f"拉取失败: {e}")
            return
        if not resp.get("ok"):
            yield event.plain_result(f"拉取失败: {resp.get('error')}")
            return
        try:
            local_path = _persist_pulled(resp)
        except Exception as e:
            yield event.plain_result(f"文件已拉到本地但保存失败: {e}")
            return
        size_kb = (resp.get("size") or 0) // 1024
        yield event.chain_result(_pulled_chain(local_path, resp.get("name"), size_kb))

    async def terminate(self) -> None:
        """插件卸载/停用时：停止服务端，释放资源。"""
        if self.server:
            await self.server.stop()
        logger.info("Cherry Remote 连接器已停止。")
