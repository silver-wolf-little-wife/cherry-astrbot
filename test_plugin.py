# -*- coding: utf-8 -*-
"""B 端插件自测：斜杠命令的设备指定 + 摄像头工具（无需 AstrBot 环境）。

做法：先桩掉 `astrbot` / `aiohttp` / `mcp`，再以包形式加载 `main.py`，
然后用假 server / 假 event 直接驱动命令与工具，验证关键行为。

用法：
    python test_plugin.py

覆盖：
  · 设备选择器解析（@设备id / --device / -d / device=）
  · 命令词剥离（兼容不同 AstrBot 版本）
  · /camera、/screenshot 的设备指定与位置参数兜底
  · /use 会话固定、单次覆盖、固定设备离线提示
  · /pull 设备前缀 + 含空格路径不被破坏
  · ws_server 多设备提示与回包 device_id
  · remote_camera 工具的 CallToolResult + ImageContent / 错误码翻译 / forward 直发 / 落盘裁剪 / 注册开关
"""

import asyncio
import base64
import importlib.util
import io
import os
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="cherry_b_test_")

# ---------- 桩 aiohttp ----------


class _Router:
    def add_get(self, *a, **k):
        return None


class _WebApp:
    def __init__(self, *a, **k):
        self.router = _Router()


class _AppRunner:
    def __init__(self, *a, **k):
        pass

    async def setup(self):
        return None


class _TCPSite:
    def __init__(self, *a, **k):
        pass

    async def start(self):
        return None


aiohttp = types.ModuleType("aiohttp")
aiohttp.WSMsgType = type("WSMsgType", (), {"TEXT": 1})
aiohttp.web = types.SimpleNamespace(
    Request=object,
    WebSocketResponse=object,
    Application=_WebApp,
    AppRunner=_AppRunner,
    TCPSite=_TCPSite,
)
sys.modules["aiohttp"] = aiohttp

# ---------- 桩 mcp.types ----------


class TextContent:
    def __init__(self, type="text", text=""):
        self.type, self.text = type, text


class ImageContent:
    def __init__(self, type="image", data="", mimeType=None):
        self.type, self.data, self.mimeType = type, data, mimeType


class CallToolResult:
    def __init__(self, content=None, isError=False):
        self.content, self.isError = content or [], isError


mcp = types.ModuleType("mcp")
mcp_types = types.ModuleType("mcp.types")
mcp_types.TextContent = TextContent
mcp_types.ImageContent = ImageContent
mcp_types.CallToolResult = CallToolResult
mcp.types = mcp_types
sys.modules["mcp"] = mcp
sys.modules["mcp.types"] = mcp_types

# ---------- 桩 astrbot ----------


class FunctionTool:
    name = ""
    description = ""
    parameters: dict = {}


class _Comp:
    """message_components 的替身：Image/File/Plain 共用，记录文本便于断言。"""

    def __init__(self, *a, **k):
        self.args = a
        self.kwargs = k
        self.text = a[0] if a else k.get("text", "")

    @classmethod
    def fromFileSystem(cls, path):
        return cls(path)


class MessageChain:
    def __init__(self, chain=None, type=None):
        self.chain = chain or []

    def file_image(self, path):
        self.chain.append(("image", path))
        return self


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def _mod(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


_mod("astrbot")
_mod("astrbot.api", FunctionTool=FunctionTool, logger=_Logger())
_mod("astrbot.api.message_components", Image=_Comp, File=_Comp, Plain=_Comp)
_mod(
    "astrbot.api.event",
    AstrMessageEvent=object,
    filter=types.SimpleNamespace(command=lambda *a, **k: (lambda f: f)),
)
_mod("astrbot.api.star", Context=object, Star=object, register=lambda *a, **k: (lambda cls: cls))
_mod("astrbot.core.message.message_event_result", MessageChain=MessageChain)
_mod("astrbot.core.agent.tool", ToolExecResult=str)
_mod("astrbot.core.utils.astrbot_path", get_astrbot_data_path=lambda: DATA)

_pkg = types.ModuleType("cherry_b_plugin")
_pkg.__path__ = [REPO]
sys.modules["cherry_b_plugin"] = _pkg
_spec = importlib.util.spec_from_file_location("cherry_b_plugin.main", os.path.join(REPO, "main.py"))
plugin_mod = importlib.util.module_from_spec(_spec)
sys.modules["cherry_b_plugin.main"] = plugin_mod
_spec.loader.exec_module(plugin_mod)

from cherry_b_plugin.ws_server import RemoteWsServer  # noqa: E402

RESULTS: dict = {}
UMO = "webchat:FriendMessage:u1"


_FALLBACK_IMAGE_B64 = {
    "JPEG": (
        "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
        "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
        "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
    ),
    "PNG": (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAA"
        "AABJRU5ErkJggg=="
    ),
}


def _image_b64(fmt: str = "JPEG") -> str:
    """生成一张极小的真图片（优先用 Pillow，没有则回退到内置常量）。"""
    try:
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (10, 5), (9, 9, 9)).save(buf, format=fmt)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except ImportError:
        return _FALLBACK_IMAGE_B64.get(fmt.upper(), _FALLBACK_IMAGE_B64["JPEG"])


def _real_server(ids=("ChengXiyue", "002")) -> RemoteWsServer:
    """真实 RemoteWsServer（只填 devices，不发网络包），用于验证真实的多设备提示。"""
    server = RemoteWsServer(
        port=1, token="t", heartbeat_timeout=60, pull_threshold=1, max_pull_size=1, pull_dir=DATA
    )
    server.devices = {i: {"ws": None, "session_id": f"s{i}", "last_seen": 0} for i in ids}
    return server


class FakeEvent:
    def __init__(self, text, umo=UMO):
        self._text = text
        self.unified_msg_origin = umo
        self.replies: list = []

    def get_message_str(self):
        return self._text

    def plain_result(self, text):
        self.replies.append(("plain", text))
        return ("plain", text)

    def chain_result(self, chain):
        self.replies.append(("chain", chain))
        return ("chain", chain)


class FakeServer:
    """只记录调用参数的假 server；多设备且未指定时抛错，模拟真实行为。"""

    def __init__(self, ids=("ChengXiyue", "002")):
        self.devices = {i: {"session_id": f"{i}0000000"} for i in ids}
        self.calls: list = []

    def device_summary(self):
        return [{"device_id": i, "session_id": v["session_id"]} for i, v in self.devices.items()]

    async def send_command(self, method, params=None, device_id=None, timeout=30):
        self.calls.append((method, params, device_id))
        if not device_id and len(self.devices) > 1:
            raise RuntimeError("多台设备在线")
        target = device_id or next(iter(self.devices))
        if method == "camera":
            return {
                "ok": True,
                "device_id": target,
                "data": {
                    "image": _image_b64(),
                    "format": "jpeg",
                    "width": 10,
                    "height": 5,
                    "size": 900,
                    "source": "direct",
                    "device": {"index": 0, "backend": "dshow", "system_name": "Fake Cam"},
                    "captured_at": "2026-09-21 12:00:00",
                },
            }
        return {
            "ok": True,
            "device_id": target,
            "data": {"image": _image_b64("PNG"), "format": "png", "width": 10, "height": 5, "size": 800},
        }

    async def pull_file(self, path, device_id=None, timeout=600):
        self.calls.append(("pull_file", path, device_id))
        if not device_id and len(self.devices) > 1:
            raise RuntimeError("多台设备在线")
        return {"ok": True, "mode": "single", "name": os.path.basename(path), "size": 10, "content": b"x"}


def new_plugin(server):
    plugin = plugin_mod.CherryRemote.__new__(plugin_mod.CherryRemote)
    plugin.config = {"camera_keep": 10}
    plugin.server = server
    plugin._session_device = {}
    return plugin


async def run_command(plugin, event, name):
    return [item async for item in getattr(plugin, name)(event)]


def collect_text(items) -> str:
    lines = []
    for kind, payload in items:
        if kind == "plain":
            lines.append(payload)
        else:
            for comp in payload:
                if isinstance(comp, _Comp) and isinstance(comp.text, str):
                    lines.append(comp.text)
    return "\n".join(lines)


async def test_parsing():
    cases = {
        "": (None, ""),
        "1": (None, "1"),
        "@002 1": ("002", "1"),
        "--device 002": ("002", ""),
        "-d ChengXiyue D:\\temp\\a b.zip": ("ChengXiyue", "D:\\temp\\a b.zip"),
        "device=002": ("002", ""),
        "--device=002 1": ("002", "1"),
        "D:\\temp\\a.zip": (None, "D:\\temp\\a.zip"),
    }
    RESULTS["parse_device_prefix"] = all(
        plugin_mod._extract_device_prefix(raw) == expect for raw, expect in cases.items()
    )

    tails = {"/camera @002 1": "@002 1", "/camera": "", "/camera@bot 1": "1", "002": "002"}
    RESULTS["command_tail"] = all(
        plugin_mod._command_tail(FakeEvent(raw), "camera") == expect for raw, expect in tails.items()
    )


async def test_commands():
    # 多台在线且未指定 → 必须给出可复制的指定方式（走真实 ws_server 提示）
    plugin = new_plugin(_real_server())
    out = collect_text(await run_command(plugin, FakeEvent("/camera"), "camera"))
    RESULTS["multi_device_hint_in_reply"] = "多台设备在线" in out and "/use" in out and "@" in out

    # @设备id 单次指定
    server = FakeServer()
    plugin = new_plugin(server)
    out = collect_text(await run_command(plugin, FakeEvent("/camera @002"), "camera"))
    RESULTS["camera_explicit_at"] = server.calls[-1][2] == "002" and "设备 002" in out

    # 位置参数写设备名（非数字）
    server = FakeServer()
    plugin = new_plugin(server)
    await run_command(plugin, FakeEvent("/camera ChengXiyue 1"), "camera")
    RESULTS["camera_positional_name"] = (
        server.calls[-1][2] == "ChengXiyue" and server.calls[-1][1].get("device") == 1
    )

    # 位置参数写纯数字设备 id（恰好是在线设备 id 时按设备处理）
    server = FakeServer()
    plugin = new_plugin(server)
    await run_command(plugin, FakeEvent("/camera 002"), "camera")
    RESULTS["camera_positional_digit_device"] = (
        server.calls[-1][2] == "002" and server.calls[-1][1].get("device") == 0
    )

    # 数字不匹配任何设备 id → 仍按摄像头 index
    server = FakeServer()
    plugin = new_plugin(server)
    await run_command(plugin, FakeEvent("/use 002"), "use")
    await run_command(plugin, FakeEvent("/camera 1"), "camera")
    RESULTS["camera_index_not_device"] = (
        server.calls[-1][2] == "002" and server.calls[-1][1].get("device") == 1
    )

    # /use 固定 + 查看 + 取消
    server = FakeServer()
    plugin = new_plugin(server)
    await run_command(plugin, FakeEvent("/use 002"), "use")
    out = collect_text(await run_command(plugin, FakeEvent("/use"), "use"))
    await run_command(plugin, FakeEvent("/use -"), "use")
    RESULTS["use_show_and_clear"] = "本会话固定设备：002" in out and plugin._session_device == {}

    # 显式 @ 覆盖会话固定
    server = FakeServer()
    plugin = new_plugin(server)
    await run_command(plugin, FakeEvent("/use ChengXiyue"), "use")
    await run_command(plugin, FakeEvent("/screenshot @002"), "screenshot")
    RESULTS["explicit_overrides_pin"] = server.calls[-1][2] == "002"

    # /screenshot 用固定设备 + 位置参数写设备名
    server = FakeServer()
    plugin = new_plugin(server)
    await run_command(plugin, FakeEvent("/use 002"), "use")
    out = collect_text(await run_command(plugin, FakeEvent("/screenshot"), "screenshot"))
    RESULTS["screenshot_pinned"] = server.calls[-1][2] == "002" and "设备 002" in out

    server = FakeServer()
    plugin = new_plugin(server)
    await run_command(plugin, FakeEvent("/use ChengXiyue"), "use")
    await run_command(plugin, FakeEvent("/screenshot 002"), "screenshot")
    RESULTS["screenshot_positional_device"] = server.calls[-1][2] == "002"

    # /pull 设备前缀 + 含空格路径保持原文
    server = FakeServer()
    plugin = new_plugin(server)
    await run_command(plugin, FakeEvent("/pull @002 D:\\temp\\a b.zip"), "pull")
    RESULTS["pull_explicit_device"] = (
        server.calls[-1][0] == "pull_file"
        and server.calls[-1][1] == "D:\\temp\\a b.zip"
        and server.calls[-1][2] == "002"
    )

    # /pull 未写设备时用会话固定，路径不被误当设备
    server = FakeServer()
    plugin = new_plugin(server)
    await run_command(plugin, FakeEvent("/use 002"), "use")
    await run_command(plugin, FakeEvent("/pull D:\\temp\\a b.zip"), "pull")
    RESULTS["pull_path_not_treated_as_device"] = (
        server.calls[-1][1] == "D:\\temp\\a b.zip" and server.calls[-1][2] == "002"
    )

    # 固定设备离线 → 明确提示
    plugin = new_plugin(FakeServer(ids=("002",)))
    plugin._session_device[UMO] = "ChengXiyue"
    out = collect_text(await run_command(plugin, FakeEvent("/camera"), "camera"))
    RESULTS["pinned_offline_hint"] = "当前离线" in out and "/use" in out

    # /devices 提示指定方式
    plugin = new_plugin(FakeServer())
    out = collect_text(await run_command(plugin, FakeEvent("/devices"), "devices"))
    RESULTS["devices_hint"] = "/use" in out and "@" in out


async def test_ws_server():
    server = _real_server()
    try:
        server._pick_device(None)
        hint_ok = False
    except RuntimeError as e:
        hint_ok = "/use" in str(e) and "@" in str(e)
    RESULTS["ws_multi_device_hint"] = hint_ok

    class FakeWs:
        def __init__(self):
            self.sent: list = []

        async def send_json(self, payload):
            self.sent.append(payload)

    ws = FakeWs()
    server.devices = {"002": {"ws": ws, "session_id": "s2", "last_seen": 0}}
    task = asyncio.create_task(
        server.send_command("system", {"action": "status"}, device_id="002", timeout=5)
    )
    await asyncio.sleep(0.1)
    server._resolve({"type": "response", "id": ws.sent[-1]["id"], "ok": True, "data": {"status": "ok"}})
    resp = await task
    RESULTS["response_carries_device_id"] = resp.get("device_id") == "002"


async def test_camera_tool():
    data = {
        "image": _image_b64(),
        "format": "jpeg",
        "width": 1280,
        "height": 720,
        "size": 1234,
        "device": {"index": 0, "backend": "dshow", "system_name": "Fake Cam"},
        "captured_at": "2026-09-21 12:00:00",
        "source": "direct",
    }

    class ToolServer:
        def __init__(self, resp):
            self.resp = resp
            self.calls: list = []

        async def send_command(self, method, params=None, device_id=None, timeout=30):
            self.calls.append((method, params, device_id, timeout))
            return self.resp

    tool = plugin_mod.RemoteCameraTool()
    tool._server = ToolServer({"ok": True, "data": data})
    tool._camera_mode = "vision"
    res = await tool.call(None, device=0, reason="自测")
    RESULTS["tool_vision_calltoolresult"] = (
        isinstance(res, CallToolResult)
        and isinstance(res.content[0], TextContent)
        and isinstance(res.content[1], ImageContent)
        and res.content[1].data == data["image"]
        and res.content[1].mimeType == "image/jpeg"
    )

    tool = plugin_mod.RemoteCameraTool()
    tool._server = ToolServer({"ok": False, "error": {"code": "CameraDisabled", "message": "x"}})
    res = await tool.call(None)
    RESULTS["tool_error_hint"] = "未开启摄像头功能" in res and "CameraDisabled" in res

    tool._server = ToolServer({"ok": False, "error": {"code": "NotImplementedError", "message": "x"}})
    res = await tool.call(None)
    RESULTS["tool_error_hint_upgrade"] = "升级" in res

    tool = plugin_mod.RemoteCameraTool()
    tool._server = ToolServer({"ok": True, "data": data})
    tool._camera_mode = "forward"
    tool._camera_keep = 50

    class _InnerCtx:
        def __init__(self):
            self.sent: list = []

        async def send_message(self, umo, chain):
            self.sent.append(umo)

    inner = _InnerCtx()
    context = types.SimpleNamespace(
        context=types.SimpleNamespace(
            context=inner, event=types.SimpleNamespace(unified_msg_origin=UMO)
        )
    )
    payload = json_loads(await tool.call(context, send_to_user=True))
    RESULTS["tool_forward_mode"] = (
        payload.get("sent_to_user") is True
        and len(inner.sent) == 1
        and os.path.isfile(payload.get("path") or "")
    )

    for _ in range(3):
        plugin_mod._save_image(data, subdir="prunetest", default_ext=".jpg", keep=2)
    files = os.listdir(os.path.join(DATA, "plugin_data", "cherry_remote", "prunetest"))
    RESULTS["tool_save_and_prune"] = len(files) == 2

    tool = plugin_mod.RemoteCameraListTool()
    tool._server = ToolServer({"ok": True, "data": {"count": 2, "devices": [{"index": 0}, {"index": 1}]}})
    RESULTS["tool_camera_list"] = json_loads(await tool.call(None)).get("count") == 2

    plugin = plugin_mod.CherryRemote.__new__(plugin_mod.CherryRemote)
    plugin.config = {"camera_enabled": False}
    plugin.server = object()
    names_off = [t.name for t in plugin._build_tools()]
    plugin.config = {"camera_enabled": True, "camera_mode": "vision", "camera_keep": 5}
    tools_on = plugin._build_tools()
    names_on = [t.name for t in tools_on]
    camera = next((t for t in tools_on if t.name == "remote_camera"), None)
    RESULTS["tool_registration"] = (
        "remote_camera" not in names_off
        and "remote_camera" in names_on
        and "remote_camera_list" in names_on
        and getattr(camera, "_camera_mode", None) == "vision"
    )


def json_loads(text):
    import json

    return json.loads(text)


async def main() -> int:
    await test_parsing()
    await test_commands()
    await test_ws_server()
    await test_camera_tool()

    print("\n===== B PLUGIN TEST REPORT =====")
    all_ok = True
    for key in sorted(RESULTS):
        ok = bool(RESULTS[key])
        all_ok = all_ok and ok
        print(f"[{'PASS' if ok else 'FAIL'}] {key}")
    print("===== " + ("ALL PASS" if all_ok else "HAS FAILURES") + " =====")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
