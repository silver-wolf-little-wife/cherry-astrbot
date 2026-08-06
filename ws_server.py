# SPDX-License-Identifier: MIT
"""B 端 WebSocket 服务端（仅依赖 aiohttp，独立于 AstrBot，便于测试）。"""

import asyncio
import json
import time
import uuid

from aiohttp import WSMsgType, web


class RemoteWsServer:
    """B 端 WebSocket 服务端：接受并管理 C 端 App 的长连接。

    协议见 docs/PROTOCOL.md：hello 握手 → 双向 JSON 帧。
    - 请求（B→C）：{"type":"request","id":...,"method":...,"params":...}
    - 响应（C→B）：{"type":"response","id":...,"ok":...,"data":...}
    """

    def __init__(self, port: int, token: str, heartbeat_timeout: int = 60):
        self.port = port
        self.token = token
        self.heartbeat_timeout = heartbeat_timeout
        self.devices: dict[str, dict] = {}  # device_id -> {ws, session_id, last_seen}
        self._pending: dict[str, asyncio.Future] = {}
        self._app = web.Application()
        self._app.router.add_get("/ws", self._handle_ws)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._hb_task: asyncio.Task | None = None

    async def start(self) -> None:
        """启动服务端与心跳清理任务。"""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host="0.0.0.0", port=self.port)
        await self._site.start()
        self._hb_task = asyncio.create_task(self._heartbeat_loop())
        print(f"[ws_server] Cherry Remote WebSocket 服务已启动: ws://0.0.0.0:{self.port}/ws")

    async def stop(self) -> None:
        """停止服务端并清理资源。"""
        if self._hb_task:
            self._hb_task.cancel()
            try:
                await self._hb_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024)
        await ws.prepare(request)
        device_id: str | None = None
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if device_id and device_id in self.devices:
                    self.devices[device_id]["last_seen"] = time.monotonic()
                mtype = data.get("type")
                if mtype == "hello":
                    device_id = await self._handle_hello(ws, data)
                    if not device_id:
                        break  # 认证失败，已关闭
                elif mtype == "ping":
                    await ws.send_json({"type": "pong"})
                elif mtype == "response":
                    self._resolve(data)
        finally:
            if device_id and self.devices.get(device_id, {}).get("ws") is ws:
                self.devices.pop(device_id, None)
                print(f"[ws_server] 设备 {device_id} 已断开，当前在线 {len(self.devices)} 台")
        return ws

    async def _handle_hello(self, ws: web.WebSocketResponse, data: dict) -> str | None:
        token = data.get("token", "")
        device_id = data.get("device_id", "")
        if token != self.token:
            await ws.send_json({"type": "hello_ack", "ok": False, "error": "invalid_token"})
            await ws.close(code=4001, message=b"invalid token")
            return None
        if not device_id:
            await ws.send_json({"type": "hello_ack", "ok": False, "error": "missing_device_id"})
            await ws.close(code=4002, message=b"missing device_id")
            return None
        # 同一 device_id 已在线时，先关闭旧连接，避免重复实例抢占/路由混乱
        old = self.devices.get(device_id)
        if old and old.get("ws") is not ws:
            try:
                await old["ws"].close(code=4000, message=b"device reconnected")
            except Exception:
                pass
            print(f"[ws_server] 设备 {device_id} 旧连接已关闭（重复接入）")
        session_id = str(uuid.uuid4())
        self.devices[device_id] = {
            "ws": ws,
            "session_id": session_id,
            "last_seen": time.monotonic(),
        }
        await ws.send_json(
            {
                "type": "hello_ack",
                "ok": True,
                "session_id": session_id,
                "server_version": "1.0.0",
            }
        )
        print(f"[ws_server] 设备接入: {device_id}（session={session_id[:8]}），在线 {len(self.devices)} 台")
        return device_id

    async def send_command(
        self,
        method: str,
        params: dict | None = None,
        device_id: str | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """向指定（或唯一）设备下发一条指令，并等待响应。"""
        device = self._pick_device(device_id)
        if device is None:
            raise RuntimeError(
                f"目标设备不在线: {device_id or '(未指定)'}；当前在线 {list(self.devices.keys()) or '无'}"
            )
        ws: web.WebSocketResponse = device["ws"]
        req_id = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await ws.send_json(
            {"type": "request", "id": req_id, "method": method, "params": params or {}}
        )
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise TimeoutError(f"指令 {method} 执行超时（{timeout}s）") from None

    def _pick_device(self, device_id: str | None) -> dict | None:
        if device_id and device_id in self.devices:
            return self.devices[device_id]
        if not device_id:
            if len(self.devices) == 1:
                return next(iter(self.devices.values()))
            if len(self.devices) > 1:
                raise RuntimeError(
                    f"多台设备在线，请指定 device_id：{list(self.devices.keys())}"
                )
        return None

    def _resolve(self, data: dict) -> None:
        rid = data.get("id")
        fut = self._pending.pop(rid, None)
        if fut and not fut.done():
            fut.set_result(data)

    def device_summary(self) -> list[dict]:
        return [
            {"device_id": did, "session_id": info["session_id"]}
            for did, info in self.devices.items()
        ]

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_timeout)
            now = time.monotonic()
            for did in list(self.devices.keys()):
                info = self.devices.get(did)
                if not info:
                    continue
                if now - info["last_seen"] > self.heartbeat_timeout:
                    print(f"[ws_server] 设备 {did} 心跳超时，清理下线")
                    self.devices.pop(did, None)
                    try:
                        await info["ws"].close()
                    except Exception:
                        pass
