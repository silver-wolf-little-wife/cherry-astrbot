# astrbot_plugin_cherry_remote

**Cherry Remote** —— AstrBot 远程操控连接器。

桥接 AstrBot（B地·云服务器）与远程电脑上的 cherry-remote-app（C地·家庭局域网 PC），实现「手机发需求 → AstrBot 调 AI 生成指令 → 插件转发 → 远程电脑执行 → 结果回传」的完整闭环。

## 定位

本插件是**纯连接器**，不承载 AI 生成逻辑：

- 内嵌 WebSocket 服务端，接受 C 端 App 主动外连（穿透 NAT）。
- 注册 FunctionTool（`remote_exec` / `remote_screenshot` / `remote_file` / `remote_app` / `remote_sysinfo`），AstrBot Agent 在普通对话中自主调用。
- 回收执行结果并回传用户。

## 安装

1. 将本插件放入 AstrBot 的 `data/plugins/` 目录。
2. 在 AstrBot 插件管理中启用「Cherry Remote」。
3. 配置 `ws_port`（默认 8765）、`auth_token`（与 App 端一致的密钥）。

## 使用

- 直接对话：`帮我截个图发给我`（需启用 Agent/Tool 模式）。
- 手动测试：`/cherry` 查看插件状态。

## 开发状态

- [x] 插件骨架（命名、注册、状态指令）
- [ ] M2 插件骨架：WebSocket 服务端 + 指令桥接
- [ ] M3 App 骨架联调
- [ ] M4 功能扩展与安全加固
- [ ] M5 Agent 化 / FunctionTool

详见根目录 `PLAN.md`（项目计划）与 `docs/PROTOCOL.md`（通信协议，待定稿）。

## 作者

littlewifeofsilverwolf
