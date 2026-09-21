<h3 align="center">⚠️ 双仓库协作项目 / Two-Repo Project</h3>
<p align="center"><b>本项目需要两个仓库共同部署才能完整运作：</b></p>
<p align="center">
<a href="https://github.com/silver-wolf-little-wife/cherry-astrbot"><b>cherry-astrbot</b></a>（B 端 · AstrBot 插件） ↔ <a href="https://github.com/silver-wolf-little-wife/cherry-remote-app"><b>cherry-remote-app</b></a>（C 端 · 执行器）
</p>

> [!IMPORTANT]
> 本仓库是 **B 端 AstrBot 插件**，必须与 **C 端执行器 [cherry-remote-app](https://github.com/silver-wolf-little-wife/cherry-remote-app)** 配合部署。请同时获取两个仓库。

---

# astrbot_plugin_cherry_remote

**Cherry Remote** —— AstrBot 远程操控连接器。当前版本 **v1.3.1**。

桥接 AstrBot（B地·云服务器）与远程电脑上的 cherry-remote-app（C地·家庭局域网 PC），实现「手机发需求 → AstrBot 调 AI 生成指令 → 插件转发 → 远程电脑执行 → 结果回传 B 端研判 → 回复原会话」的完整闭环。

## 定位

本插件是**纯连接器**，不承载 AI 生成逻辑：

- 内嵌 WebSocket 服务端，接受 C 端 App 主动外连（穿透 NAT）。
- 注册 FunctionTool，AstrBot Agent 在普通对话中自主调用。
- 回收执行结果回传 Agent 研判。

## 已注册工具

| 工具 | 能力 |
|---|---|
| `remote_exec` | 远程执行 shell 命令 |
| `remote_sysinfo` | 系统信息（CPU/内存/磁盘） |
| `remote_ping` | 连通性检测 |
| `remote_file` | 文件 list/read/write/copy/delete/info |
| `remote_app` | 启动/结束/搜索应用（exe 索引） |
| `remote_screenshot` | 截屏（保存本地 PNG + 直发图片给用户） |
| `remote_camera` | **摄像头拍照**（v1.3.0 新增）：拍一张 C 端周围环境的照片交给多模态模型查看，用于了解屏幕之外的物理环境 |
| `remote_camera_list` | **列出 C 端摄像头设备**（v1.3.0 新增） |
| `remote_pull_file` | 拉取远程文件到本地并直发（图片直显，其余作附件；小文件单帧、大文件流式分块 + sha256 校验） |

所有工具支持可选 `device_id` 参数（多设备定向下发）。

## 摄像头（`remote_camera`）

让 AI「看一眼 C 端周围」的工具，与 `remote_screenshot`（看屏幕内容）互补：

- 照片经 `CallToolResult + ImageContent` 交给**多模态模型**（模型真的能"看见"画面），并按需转发给用户；
- 模型不支持看图片时，把 `camera_mode` 设为 `forward`（插件直接把照片发给用户，不进模型上下文）；
- **前置条件**：C 端 `config.yaml` 必须设置 `camera.enabled: true`（C 端默认关闭，隐私优先），
  否则工具会返回「该电脑未开启摄像头功能」；
- 隐私：照片会离开 C 端进入 B 端与模型服务商，C 端另有限流（默认 5 秒冷却 / 每小时 60 次）与审计日志；
  不想提供该能力时把 `camera_enabled` 设为 `false` 即可整体关闭。

## 命令

- `/cherry` —— 插件与在线设备状态
- `/devices` —— 列出在线设备（并给出指定设备的方式）
- `/use <设备id>` —— **固定本会话使用的设备**（`/use` 查看、`/use -` 取消）
- `/screenshot [@设备id]` —— 截取 C 端屏幕并发图
- `/camera [@设备id] [摄像头index]` —— 用 C 端摄像头拍照并发图（需 C 端已开启 `camera.enabled`）
- `/pull [@设备id] <远程路径>` —— 拉取 C 端文件并发图/发文件（路径含空格无需引号）

### 多台设备时怎么指定（v1.3.1 修复）

**多台 C 端同时在线时，斜杠命令必须指定设备**，否则只会提示「多台设备在线」。两种方式：

```
/use ChengXiyue          # 方式一：固定本会话设备（推荐，之后不用每次写）
/camera 1                # 这条就发给了 ChengXiyue 的 1 号摄像头

/camera @002             # 方式二：单次指定（不改动会话固定）
/screenshot @ChengXiyue
/pull @002 D:\temp\a.zip
```

- 设备选择器支持 `@设备id`、`--device 设备id`、`-d 设备id`、`device=设备id`，写在参数最前面。
- 固定的设备掉线时会明确提示（不静默失败），`/use` 重新指定即可。
- AI 对话（工具调用）走 `device_id` 参数，一直支持多设备；本次修的是**斜杠命令没有这个入口**。

## 安装（B 端·云服务器）

1. **Docker 部署 AstrBot**，映射插件 WS 端口到宿主机：
   ```yaml
   # docker-compose.yml 片段
   ports:
     - "8765:8765"   # 插件 WebSocket 端口
   ```
2. 将本插件放入 AstrBot 的 `data/plugins/`（或从 GitHub 安装）。
3. 在 AstrBot 插件管理中启用「Cherry Remote」。
4. 配置（`_conf_schema.json`）：
   - `ws_port`：WS 服务端口（默认 8765）
   - `auth_token`：**必须与 C 端 config.yaml 完全一致**
   - `heartbeat_timeout`：心跳超时
   - `pull_threshold`：单帧拉取阈值（字节，默认 8MB，超过走流式分块）
   - `max_pull_size`：单次拉取大小上限（字节，默认 200MB）
   - `camera_enabled`：是否向 AI 注册摄像头工具（默认 true）
   - `camera_mode`：照片处理方式 `vision`（进模型上下文，默认）/ `forward`（直接发用户）/ `both`
   - `camera_keep`：B 端本地保留的摄像头照片数量（默认 50，超出自动删除最旧的）
5. AstrBot 全局配置建议：`computer_use_runtime = local`（让 skills 可执行；与本插件无关，但影响 skills/MCP 能力）。

## 使用

- 直接对话：`帮我截个图发给我`、`打开 C 盘某目录`、`让 C 电脑 ping 一下百度`、`把 C 电脑上的 xxx.zip 发给我`、`看看家里电脑周围什么情况`（摄像头，需 C 端已开启）、（需 AstrBot 启用 Agent/Tool 模式）。
- 多设备：对话中指定设备名，或先 `/devices` 查看。

## 通信协议

见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)（与 cherry-remote-app 共享）。

## 开发状态

- [x] M1 协议定稿
- [x] M2 插件骨架（WS 服务端 + FunctionTool）
- [x] M3 App 骨架联调
- [x] M4 功能扩展（file/app/screenshot + 审计）
- [x] M5 Agent 化（FunctionTool 工具集）
- [x] M6 安全加固（急停/多设备/防重复连接）
- [x] M7 文件拉取（remote_pull_file + /pull，流式分块 + sha256 校验）
- [x] M8 摄像头工具集（remote_camera / remote_camera_list + /camera，照片交给多模态模型查看）
- [x] M9 斜杠命令支持指定设备（`@设备id` 单次指定 + `/use` 会话固定，修复多台在线时命令必失败）

## 作者

littlewifeofsilverwolf
