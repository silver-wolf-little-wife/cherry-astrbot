# Cherry Remote 通信协议 v1.0.0

> 双仓库共享协议。本文档为权威版本，`cherry-astrbot`（插件）与 `cherry-remote-app`（App）必须保持同步。

## 1. 概述

- **传输**：WebSocket（生产环境建议 wss/TLS）。
- **方向**：C 端 App **主动外连** B 端插件服务端（穿透家庭 NAT），B 永不主动连 C。
- **帧格式**：UTF-8 JSON 文本帧。
- **角色**：B = 请求方（下发指令），C = 执行方（纯执行器，无任何 AI 逻辑）。
- **协议风格**：JSON-RPC 风格，请求/响应以 `id` 关联。

## 2. 连接握手

C 连接成功后，必须首先发送 `hello`：

```json
{"type": "hello", "token": "<auth_token>", "device_id": "home-pc", "client_version": "1.0.0"}
```

B 响应：

```json
{"type": "hello_ack", "ok": true, "session_id": "<uuid>", "server_version": "1.0.0"}
```

失败时 B 返回 `ok:false` 并立即断开：

```json
{"type": "hello_ack", "ok": false, "error": "invalid_token"}
```

握手失败错误码：`invalid_token` / `missing_device_id`。

## 3. 心跳

- C 每 `heartbeat_interval`（默认 15s）发送 `{"type": "ping"}`。
- B 收到即回 `{"type": "pong"}`。
- B 在 `heartbeat_timeout`（默认 60s）内未收到某设备**任何帧**，判定掉线并清理设备记录。
- C 若超过 `heartbeat_interval * 3` 未收到 B 任何帧，主动断开并重连。

## 4. 请求（B → C）

```json
{
  "type": "request",
  "id": "<uuid>",
  "method": "exec",
  "params": { }
}
```

## 5. 响应（C → B）

成功：

```json
{
  "type": "response",
  "id": "<uuid>",
  "ok": true,
  "data": { },
  "error": null
}
```

失败：

```json
{
  "type": "response",
  "id": "<uuid>",
  "ok": false,
  "data": null,
  "error": {"code": "exec_failed", "message": "..."}
}
```

## 6. Method 定义

### 6.1 `exec` — 执行 shell 命令

```json
{
  "method": "exec",
  "params": {
    "command": "dir C:\\Users\\x",
    "cwd": "C:\\Users\\x",
    "timeout": 30,
    "env": {"KEY": "value"}
  }
}
```

`cwd` / `timeout` / `env` 均可选。成功响应 `data`：

```json
{
  "stdout": "...",
  "stderr": "...",
  "exit_code": 0,
  "timed_out": false,
  "elapsed": 1.23
}
```

超时：C 端 `kill` 进程并回 `timed_out:true`。

### 6.2 `sys` — 系统信息

```json
{"method": "sys", "params": {}}
```

成功响应 `data`：

```json
{
  "hostname": "DESKTOP-ABC",
  "os": "Windows",
  "release": "10",
  "machine": "AMD64",
  "cpu": {"percent": 12.5, "count": 8, "freq": {"current": 3600, "max": 4600, "min": 800}},
  "memory": {"total": 17179869184, "available": 8589934592, "used": 8589934592, "percent": 50.0},
  "disk": [{"mount": "C:\\", "device": "C:", "total": 512110190592, "used": 204804096000, "percent": 40.0}],
  "boot_time": 1730000000.0
}
```

### 6.3 `ping` — 连通性测试

```json
{"method": "ping", "params": {}}
```

成功响应 `data`：`{"pong": true}`

### 6.4 `file` — 文件操作

```json
{
  "method": "file",
  "params": {
    "action": "list",
    "path": "C:\\Users\\x\\Desktop",
    "recursive": false
  }
}
```

`action` 取值与响应：

| action | 必填参数 | 可选参数 | 响应 data |
|---|---|---|---|
| `list` | `path` | `recursive` | `{"path","count","entries":[{name,path,type,size,mtime}]}` |
| `read` | `path` | — | `{"path","encoding","size","content"}`（文本用 utf-8/locale，二进制用 base64） |
| `write` | `path`,`content` | `encoding=base64` | `{"path","ok","bytes"}` |
| `copy` | `path`,`dest` | — | `{"ok","src","dest"}` |
| `delete` | `path` | — | `{"ok","deleted"}` |
| `info` | `path` | — | `{"path","type","size","mtime","absolute"}` |

> ⚠️ `delete` 会永久删除文件/目录，属高危操作，请在 `allowed_actions` 白名单层面控制。

### 6.5 `app` — 应用启停

```json
{
  "method": "app",
  "params": {"action": "launch", "name": "notepad.exe", "args": []}
}
```

| action | 必填参数 | 可选参数 | 响应 data |
|---|---|---|---|
| `launch` | `name` | `args`, `cwd` | `{"ok","pid","launched"}` |
| `terminate` | `pid` 或 `name` | — | `{"ok","terminated":[pid,...]}` |

### 6.6 `screenshot` — 截屏

```json
{"method": "screenshot", "params": {}}
```

响应 data：`{"image":"<base64 PNG>","format":"png","width":W,"height":H,"size":N}`

## 7. 错误码

| code | 含义 |
|---|---|
| `invalid_token` | 认证 token 错误 |
| `missing_device_id` | 缺少设备标识 |
| `unknown_device` | 目标设备不在线 |
| `timeout` | 指令执行超时 |
| `exec_failed` | 命令执行失败 |
| `not_supported` | method 未实现 |
| `not_allowed` | method 不在白名单 |
| `internal_error` | 内部异常 |

## 8. 安全

- 握手必须携带有效 `token`，无效立即断开。
- 传输层建议 wss/TLS。
- C 端为**纯执行器**：只执行、回传原始结果，不做任何判断与内容加工。
- 指令白名单由 C 端 `allowed_actions` 与 B 端共同约束。
- 审计日志：B 端记录每次下发与结果；C 端记录执行明细。
