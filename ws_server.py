# SPDX-License-Identifier: AGPL-3.0-only
"""B 端 WebSocket 服务端（仅依赖 aiohttp，独立于 AstrBot，便于测试）。"""

import asyncio
import base64
import hashlib
import json
import os
import tempfile
import time
import uuid

from aiohttp import WSMsgType, web


class RemoteWsServer:
    """B 端 WebSocket 服务端：接受并管理 C 端 App 的长连接。

    协议见 docs/PROTOCOL.md：hello 握手 → 双向 JSON 帧。
    - 请求（B→C）：{"type":"request","id":...,"method":...,"params":...}
    - 响应（C→B）：{"type":"response","id":...,"ok":...,"data":...}
    - 流式数据帧（C→B，file_pull）：{"type":"file_data","id":...,"index":...,"total":...,"data":base64}
    """

    def __init__(
        self,
        port: int,
        token: str,
        heartbeat_timeout: int = 60,
        pull_threshold: int = 8 * 1024 * 1024,
        max_pull_size: int = 200 * 1024 * 1024,
        pull_dir: str | None = None,
    ):
        self.port = port
        self.token = token
        self.heartbeat_timeout = heartbeat_timeout
        # 文件拉取策略：小于 pull_threshold 走单帧（复用 file.read），否则走流式分块
        self.pull_threshold = pull_threshold
        self.max_pull_size = max_pull_size
        self.pull_dir = pull_dir  # 流式文件落盘目录（None 则用系统临时目录）
        self.devices: dict[str, dict] = {}  # device_id -> {ws, session_id, last_seen}
        self._pending: dict[str, asyncio.Future] = {}
        # request_id -> {file, received, sha, temp, error}（file_pull 流式传输状态）
        self._transfers: dict[str, dict] = {}
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
        # 清理未完成的流式传输
        for rid in list(self._transfers.keys()):
            self._abort_transfer(rid)
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
                elif mtype == "file_data":
                    # 流式分块：直接落盘，不 resolve Future（不进 LLM 上下文）
                    self._handle_file_data(data)
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
                "server_version": "1.2.0",
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

    async def pull_file(
        self,
        path: str,
        device_id: str | None = None,
        timeout: float = 600.0,
    ) -> dict:
        """从 C 端拉取文件到本地。

        小文件（≤ pull_threshold）走单帧模式（复用 file.read，返回 bytes/str）；
        大文件走流式模式（file_pull 分块推送，B 端边收边落盘，返回本地路径）。

        返回：{"ok": True, "mode": "single"/"single_text"/"stream", "name", "size",
               "content": bytes|str（单帧模式） 或 "local_path": str（流式模式）}
        """
        if self._pick_device(device_id) is None:
            raise RuntimeError(
                f"目标设备不在线: {device_id or '(未指定)'}；当前在线 {list(self.devices.keys()) or '无'}"
            )
        # 1) 探测文件是否存在及大小
        info = await self.send_command(
            "file", {"action": "info", "path": path}, device_id=device_id, timeout=15
        )
        if not info.get("ok"):
            return {
                "ok": False,
                "error": (info.get("error") or {}).get("message", "file.info 失败"),
            }
        fsize = int((info.get("data") or {}).get("size") or 0)
        if self.max_pull_size and fsize > self.max_pull_size:
            return {
                "ok": False,
                "error": f"文件大小 {fsize} 超过 max_pull_size={self.max_pull_size}",
            }
        name = str(path).replace("\\", "/").split("/")[-1] or "file"

        # 2) 单帧模式：复用 file.read（结果不经 LLM，直接返回给调用方）
        if fsize <= self.pull_threshold:
            resp = await self.send_command(
                "file",
                {"action": "read", "path": path},
                device_id=device_id,
                timeout=max(60, fsize // (1024 * 1024) * 2 + 30),
            )
            if not resp.get("ok"):
                return {
                    "ok": False,
                    "error": (resp.get("error") or {}).get("message", "file.read 失败"),
                }
            data = resp.get("data") or {}
            if data.get("encoding") == "base64":
                return {
                    "ok": True,
                    "mode": "single",
                    "name": name,
                    "size": int(data.get("size") or 0),
                    "content": base64.b64decode(data.get("content", "")),
                }
            return {
                "ok": True,
                "mode": "single_text",
                "name": name,
                "size": len(data.get("content", "").encode("utf-8")),
                "content": data.get("content", ""),
            }

        # 3) 流式模式：file_pull 分块推送，边收边落盘
        ws: web.WebSocketResponse = self._pick_device(device_id)["ws"]
        req_id = str(uuid.uuid4())
        temp_dir = self.pull_dir or tempfile.gettempdir()
        temp_path = os.path.join(
            temp_dir,
            f"cherry_pull_{int(time.time())}_{uuid.uuid4().hex[:6]}_{name}",
        )
        temp_file = open(temp_path, "wb")
        self._transfers[req_id] = {
            "file": temp_file,
            "received": 0,
            "sha": hashlib.sha256(),
            "temp": temp_path,
            "error": None,
        }
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await ws.send_json(
                {
                    "type": "request",
                    "id": req_id,
                    "method": "file_pull",
                    "params": {"path": path, "chunk_size": 1024 * 1024},
                }
            )
            resp = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._abort_transfer(req_id)
            raise TimeoutError(f"拉取文件超时（{timeout}s）") from None
        t = self._transfers.pop(req_id, None)
        if t is None:
            try:
                temp_file.close()
            except Exception:
                pass
            return {"ok": False, "error": "传输状态丢失"}
        temp_file.close()
        if not resp.get("ok"):
            os.remove(temp_path)
            return {
                "ok": False,
                "error": (resp.get("error") or {}).get("message", "file_pull 失败"),
            }
        meta = resp.get("data") or {}
        if t.get("error"):
            os.remove(temp_path)
            return {"ok": False, "error": t["error"]}
        if t["received"] != meta.get("chunks"):
            os.remove(temp_path)
            return {
                "ok": False,
                "error": f"分块不完整：期望 {meta.get('chunks')}，实际 {t['received']}",
            }
        if t["sha"].hexdigest() != meta.get("sha256"):
            os.remove(temp_path)
            return {"ok": False, "error": "sha256 校验失败，文件已丢弃"}
        return {
            "ok": True,
            "mode": "stream",
            "name": name,
            "size": int(meta.get("size") or 0),
            "local_path": temp_path,
        }

    def _handle_file_data(self, data: dict) -> None:
        """处理 C 端推送的文件分块帧：直接写盘，不 resolve Future。"""
        rid = data.get("id")
        t = self._transfers.get(rid)
        if not t:
            return  # 未知或已结束的传输，忽略
        if data.get("index") != t["received"]:
            t["error"] = (
                f"分块乱序：期望 {t['received']}，实际 {data.get('index')}"
            )
            return
        try:
            payload = base64.b64decode(data.get("data", ""))
        except Exception:
            t["error"] = "分块 base64 解码失败"
            return
        try:
            t["file"].write(payload)
        except Exception as e:
            t["error"] = f"写盘失败: {e}"
            return
        t["sha"].update(payload)
        t["received"] += 1

    def _abort_transfer(self, req_id: str) -> None:
        """中止传输：关闭文件句柄、删除临时文件、移除 pending。"""
        t = self._transfers.pop(req_id, None)
        if t:
            try:
                t["file"].close()
            except Exception:
                pass
            try:
                os.remove(t["temp"])
            except OSError:
                pass
        self._pending.pop(req_id, None)

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
