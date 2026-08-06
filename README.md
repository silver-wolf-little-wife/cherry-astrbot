# astrbot_plugin_cherry_remote

**Cherry Remote** —— AstrBot 远程操控连接器。

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

所有工具支持可选 `device_id` 参数（多设备定向下发）。

## 命令

- `/cherry` —— 插件与在线设备状态
- `/devices` —— 列出在线设备
- `/screenshot` —— 直接截取 C 端屏幕并发图

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
5. AstrBot 全局配置建议：`computer_use_runtime = local`（让 skills 可执行；与本插件无关，但影响 skills/MCP 能力）。

## 使用

- 直接对话：`帮我截个图发给我`、`打开 C 盘某目录`、`让 C 电脑 ping 一下百度`（需 AstrBot 启用 Agent/Tool 模式）。
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

## 作者

littlewifeofsilverwolf
