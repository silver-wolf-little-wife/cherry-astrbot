# Cherry Remote 通信协议 v1.1.0

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
| `search` | — | `query` | `{"count","query","matches":[{name,path,matched_on,product?}]}` |

> **应用名解析顺序**（`launch` 的 `name`）：产品名/文件说明（显示名）→ exe 文件名 → PATH。
> 例如 `name: "米哈游启动器"` 会命中 exe 索引中产品名为“米哈游启动器”的 `HYP.exe`，
> 解决“exe 文件名 ≠ 用户认知的应用名”（如 `ZenlessZoneZero.exe` 的产品名是“绝区零”）。

#### 6.5.1 `search` — 按名称模糊搜索应用

同时匹配 **exe 文件名** 与 **产品名/文件说明**（显示名），并去重：

```json
{"method": "app", "params": {"action": "search", "query": "米哈游"}}
```

成功响应 `data`：

```json
{
  "count": 1,
  "query": "米哈游",
  "matches": [
    {
      "name": "HYP.exe",
      "path": "E:\\Program Files\\miHoYo Launcher\\1.16.1.364\\HYP.exe",
      "matched_on": "product",
      "product": "米哈游启动器"
    }
  ]
}
```

- `matched_on`：`filename`=按文件名命中；`product`=按产品名/文件说明命中（此时附带 `product` 字段）。

#### 6.5.2 exe 索引文件格式（v2）

`exe_index.json` 由 C 端启动时后台构建（`build_exe_index: true`），供 B 端/AI 直接读取定位应用：

```json
{
  "hyp.exe": "E:\\Program Files\\miHoYo Launcher\\1.16.1.364\\HYP.exe",
  "...": "...",
  "_product": {
    "米哈游启动器": [
      {"file": "HYP.exe", "path": "E:\\Program Files\\miHoYo Launcher\\1.16.1.364\\HYP.exe"}
    ]
  },
  "_meta": {
    "version": 2,
    "built_at": "2026-08-26 09:30:00",
    "exe_count": 1418,
    "product_count": 923
  }
}
```

- **顶层键**：exe 文件名（小写）→ 完整路径（v1 格式兼容，保持不变）。
- **`_product`**：产品名称/文件说明（小写）→ 文件与路径列表，用于“用户认知的应用名 ≠ 文件名”的查找。
- **`_meta`**：索引元信息（格式版本、构建时间、条目数）。
- **回退链**：文件属性里的产品名称（ProductName）缺失或为空时，回退到文件说明（FileDescription）；
  两者皆无（如无版本资源的裸打包 exe）则仅保留文件名条目，`search`/`launch` 仍可按文件名命中。

### 6.6 `screenshot` — 截屏

```json
{"method": "screenshot", "params": {}}
```

响应 data：`{"image":"<base64 PNG>","format":"png","width":W,"height":H,"size":N}`

### 6.7 `camera` — 摄像头拍照（v1.1.0 新增）

用于让 B 端 AI 了解**电脑周围的环境**（屏幕之外）：房间、设备指示灯、纸质材料等。

```json
{"method": "camera", "params": {"action": "capture", "device": 0, "reason": "用户想看看家里情况"}}
```

`action` 取值：`capture`（**默认**，缺省即拍照）、`list`（枚举摄像头）、`status`（开关与限流状态）。

> ⚠️ 前置条件：C 端 `config.yaml` 必须开启 `camera.enabled: true`，且 `allowed_actions` 含 `camera`，
> 否则回 `CameraDisabled`。该开关默认关闭（隐私优先）。

#### 6.7.1 `action: capture` — 拍一帧

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `device` | int\|string | `0` | 摄像头 index；也可传设备名做尽力匹配（建议先用 `list` 查 index） |
| `width` / `height` | int | 1280 / 720 | 请求分辨率，按 C 端 `camera.max_width/max_height` 夹取 |
| `quality` | int | 80 | JPEG 质量，夹取到 30~95 |
| `warmup_frames` | int | 5 | 丢弃前 N 帧，等自动曝光/对焦稳定 |
| `burst` | int | 1 | 连拍帧数（1~5），多帧时自动选最清晰的一帧（拉普拉斯方差） |
| `format` | string | `jpeg` | `jpeg` / `png` |
| `mirror` | bool | false | 左右镜像 |
| `save_local` | bool | true | 是否在 C 端本地留档（**仅当配置了 `camera.archive_dir` 时生效**） |
| `reason` | string | — | 调用原因，仅写入 C 端审计日志 |
| `timeout` | float | 15 | 单次拍摄超时（秒），夹取到 3~60 |

成功响应 data：

```json
{
  "image": "<base64 JPEG>",
  "format": "jpeg",
  "width": 1280,
  "height": 720,
  "size": 153621,
  "device": {"index": 0, "backend": "dshow", "system_name": "ASUS FHD webcam"},
  "burst": 3,
  "sharpness": 412.7,
  "captured_at": "2026-08-27 10:12:33",
  "source": "direct",
  "elapsed": 1.82,
  "archived_path": null
}
```

- `source`：`direct`（本进程直采）/ `user-session`（服务会话下切交互用户会话采集）/ `ffmpeg`（ffmpeg 兜底后端）。
- `device.backend`：`dshow` / `msmf` / `default`（OpenCV 后端）。
- `sharpness`：拉普拉斯方差，仅用于连拍选帧参考，可能为 `null`。
- 字段命名与 `screenshot` 对齐（`image/format/width/height/size`），B 端可直接复用落盘逻辑。

#### 6.7.2 `action: list` — 枚举摄像头

```json
{"method": "camera", "params": {"action": "list"}}
```

成功响应 data：

```json
{
  "count": 2,
  "devices": [
    {"index": 0, "openable": true, "width": 640, "height": 480, "backend": "dshow", "system_name": "ASUS FHD webcam"},
    {"index": 1, "openable": true, "width": 640, "height": 480, "backend": "dshow", "system_name": "ASUS IR camera"}
  ],
  "system_devices": [
    {"name": "ASUS FHD webcam", "class": "Camera", "status": "OK"},
    {"name": "ASUS IR camera", "class": "Camera", "status": "OK"}
  ],
  "note": "devices 为 OpenCV 实际可打开的设备；system_devices 为系统 PnP 设备名，顺序仅为尽力对应"
}
```

#### 6.7.3 `action: status` — 摄像头功能状态

```json
{"enabled": true, "backend": "auto", "session_id": 0, "user_session_fallback": true,
 "min_interval_seconds": 5, "max_per_hour": 60, "captured_last_hour": 3,
 "cooldown_remaining": 0.0, "archive_dir": null}
```

#### 6.7.4 限流与会话约束

- C 端冷却 `camera.min_interval_seconds`（默认 5s）+ 每小时配额 `camera.max_per_hour`（默认 60），
  超限回 `CameraRateLimited`；**失败的尝试同样计数**（防止失败重试绕过限流）。
- 同一时刻只允许一路拍摄，拍摄进行中再次下发立即回 `CameraBusy`（快速失败，不排队）。
- 服务会话（Session 0，如 NSSM 服务形态）无法访问摄像头，C 端会自动切到交互用户会话采集
  （`source: "user-session"`）；无人登录时回 `CameraNoInteractiveSession`。
- 隐私提醒：照片**会离开本机**（经 B 端进入多模态模型上下文或直接发给用户），
  C 端可选用 `camera.archive_dir` 本地留档以便事后核对。

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

> C 端当前实现中，`error.code` 取**异常类名**（如 `PermissionError` / `FileNotFoundError`）。
> `camera` 相关错误使用下列语义化类名，B 端可据此稳定判定并给出人话提示：

| code（camera） | 含义 | 建议提示 |
|---|---|---|
| `CameraDisabled` | `camera.enabled=false` 或不在白名单 | 该电脑未开启摄像头功能 |
| `CameraBackendUnavailable` | 既无 OpenCV 也无 ffmpeg | 该电脑缺少摄像头采集组件 |
| `CameraNotFound` | 无可用摄像头 / index 或名称不存在 | 没找到摄像头 |
| `CameraOpenFailed` | 设备打不开（被占用、隐私设置禁止、驱动异常） | 摄像头无法打开，可能被其他程序占用 |
| `CameraBusy` | 已有一次拍摄正在进行 | 上一次拍摄还没结束，请稍后再试 |
| `CameraNoInteractiveSession` | Session 0 且无交互用户可兜底 | 请确认有用户登录后再试 |
| `CameraCaptureFailed` | 打开成功但取不到有效画面 / 采集超时 | 取帧失败，请检查摄像头是否被遮挡 |
| `CameraRateLimited` | 触发冷却或小时配额 | 拍摄太频繁，请稍后再试 |


## 8. 安全

- 握手必须携带有效 `token`，无效立即断开。
- 传输层建议 wss/TLS。
- C 端为**纯执行器**：只执行、回传原始结果，不做任何判断与内容加工。
- 指令白名单由 C 端 `allowed_actions` 与 B 端共同约束。
- 审计日志：B 端记录每次下发与结果；C 端记录执行明细。

## 9. 流式文件拉取（`file_pull`，v1.2.0+）

B 端拉取大文件时使用流式分块，避免大段 base64 进入 LLM 上下文：

1. B 端先发 `file.info` 探测大小（复用 6.4 单帧读取）。
2. 小文件（<= `pull_threshold`，默认 8MB）走单帧 `file.read`，base64 一次返回。
3. 大文件流程：
   - B→C：`{"type":"request","id":...,"method":"file_pull","params":{"path":...,"chunk_size":1048576}}`
   - C→B：逐块推送 `{"type":"file_data","id":<请求id>,"index":0..n-1,"total":n,"data":<base64>}`，B 端边收边落盘，不 resolve 请求
   - C→B 最后：`{"type":"response","id":...,"ok":true,"data":{"name":...,"size":...,"chunks":n,"sha256":...}}`
   - B 端校验：分块数一致 + sha256 匹配才保留文件，否则删除并报错
4. 失败/超时/断线：B 端清理临时文件与传输状态。

## 10. B 端工具映射（`astrbot_plugin_cherry_remote`）

| B 端工具 / 指令 | 对应 method | 说明 |
|---|---|---|
| `remote_exec` | `exec` | 执行 shell 命令 |
| `remote_sysinfo` | `sys` | 系统信息 |
| `remote_ping` | `ping` | 连通性 |
| `remote_file` | `file` | 文件操作 |
| `remote_app` | `app` | 应用启停/搜索 |
| `remote_screenshot` | `screenshot` | 截屏（看屏幕内容） |
| `remote_pull_file` | `file_pull` | 拉取文件 |
| `remote_camera` | `camera`（capture） | 摄像头拍照：照片经 `CallToolResult + ImageContent` 交给多模态模型查看 |
| `remote_camera_list` | `camera`（list） | 枚举 C 端摄像头 |
| `/screenshot`、`/camera`、`/pull`、`/use`、`/cherry`、`/devices` | — | 用户手动触发的管理指令 |

### 10.1 斜杠命令的设备选择（v1.3.1）

多台 C 端在线时，`send_command` 不传 `device_id` 会回「多台设备在线」提示，
因此斜杠命令补上两个设备入口（**B 端本地解析，不改变协议**）：

| 写法 | 作用 |
|---|---|
| `/use <设备id>` | 固定**本会话**（`unified_msg_origin`）的目标设备；`/use` 查看，`/use -` 取消 |
| `/camera @设备id [index]`、`/screenshot @设备id`、`/pull @设备id <路径>` | 单次指定设备 |
| `--device <id>` / `-d <id>` / `device=<id>`（写在参数最前面） | 与 `@设备id` 等价 |

- 解析优先级：显式选择器 > 会话固定 > 自动（仅一台在线时）。
- 固定的设备掉线时命令回报明确提示，不会静默失败。
- B 端在回包里附加 `device_id` 字段，便于确认这条结果来自哪台机器。

`camera` 照片在 B 端有三种处理方式（配置项 `camera_mode`）：

- `vision`（默认）：照片作为 `ImageContent` 进入模型上下文，模型可"看见"画面，并按需用
  `send_message_to_user(type='image', path=...)` 转发给用户；
- `forward`：插件直接把照片发给用户，不进模型上下文（模型不支持视觉时使用）；
- `both`：两者都做。

> 隐私提示：照片会离开 C 端进入 B 端（以及所配置的多模态模型服务商）。
> B 端可用 `camera_enabled=false` 整体关掉摄像头工具，C 端默认也已关闭该功能。
