# OpenCode Web 数据面能力矩阵

基线：通过当前真实 OpenCode Web 会话访问 `GET /doc`，返回 **162 个路径、188 个操作**；运行版本为 `1.18.30`。完整机器可读路由目录由 `GET /doc` 生成，禁止凭猜测删减 API。

当前实现证据（2026-09-21）：Relay HTTP/HTML、6 MB Provider 响应、P2P HTTP、P2P SSE 首帧、P2P EventSource、P2P WebSocket 错误关闭均已在真实浏览器验证。两台设备均已实测：WSL Agent 使用 `10.0.0.101` host candidate，第二台 ARM64 Agent 使用 `10.0.0.149` host candidate，业务数据未走 VPS。PTY 正常创建、输入、cursor 重连仍需真实交互验收；LAN 独立入口尚未启用（当前设备通过 P2P 优先，Relay 兜底）。

## 路径类别

| 类别 | 代表路径 | 传输 | 生命周期重点 | 验收重点 |
|---|---|---|---|---|
| 静态资源 | `/`, `/assets/*`, `/favicon.ico`, `/site.webmanifest` | HTTP | 缓存、压缩、MIME | 页面资源完整加载 |
| 全局控制 | `/global/health`, `/global/config`, `/global/dispose`, `/path` | HTTP | 请求超时、错误透传 | 状态码、响应头、JSON |
| 全局事件 | `/global/event` | SSE | 心跳、空闲、取消、重连 | `server.connected`、heartbeat、不断流 |
| v2 会话 | `/api/session*` | HTTP + SSE | prompt、interrupt、状态收敛 | 新建、发送、停止、刷新一致 |
| legacy 会话 | `/session*` | HTTP + SSE | abort、prompt、fork、revert | 老 UI 路径仍可用 |
| v2 事件 | `/api/event`, `/api/session/:id/event` | SSE | `Last-Event-ID`、重放、实时事件 | 不丢事件、不重复副作用 |
| PTY | `/api/pty*`, `/pty*` | HTTP + WebSocket | ticket、cursor、resize、关闭码 | 输入、输出、重连、二进制 |
| 文件系统 | `/api/fs/*`, `/file*` | HTTP/原始字节 | 路径、MIME、Range、大文件 | 文件读取和下载不损坏 |
| 项目/工作区 | `/project*`, `/workspace*`, `/api/project-copy*` | HTTP | directory/workspace 路由 | 项目和工作区一致 |
| Provider/模型 | `/provider*`, `/api/provider*`, `/api/model` | HTTP | 配置读取、OAuth 状态 | 列表与详情完整 |
| 工具/Agent | `/api/agent`, `/api/command`, `/tool*`, `/skill*` | HTTP | 工具发现、执行关联 | 工具和 sub-agent 显示一致 |
| 权限 | `/permission*`, `/api/permission*` | HTTP + SSE 状态 | request/reply、取消、超时 | 允许/拒绝状态一致 |
| 问题 | `/question*`, `/api/question*` | HTTP + SSE 状态 | request/reply/reject | 页面操作与任务状态一致 |
| MCP/集成 | `/mcp*`, `/api/integration*`, `/api/credential*` | HTTP | OAuth、轮询、取消 | 不泄漏凭据，错误可重试 |
| VCS/LSP/格式化 | `/vcs*`, `/lsp`, `/formatter` | HTTP/原始字节 | 长请求、错误、文件关联 | 结果和错误完整 |
| TUI/同步 | `/tui/*`, `/sync/*` | HTTP + SSE 状态 | replay、steal、history | 不破坏会话事件顺序 |

## 通用 HTTP 验收字段

每个 HTTP 请求必须记录并按路径透传：method、path、query string、body、`Authorization`、`x-opencode-directory`、`x-opencode-workspace`、`Content-Type`、`Content-Length`、`Content-Encoding`、`Accept`、`Range`、`If-None-Match`、状态码、`WWW-Authenticate`、`Set-Cookie`、MIME 和原始字节。浏览器 AbortSignal 必须能取消上游请求。

带副作用的 `POST`、`PUT`、`PATCH`、`DELETE` 不得因路径切换而盲目重试。

## SSE 验收矩阵

| 场景 | 必须保持的语义 |
|---|---|
| 首帧 | 先注册监听再发送连接事件，不能丢 `server.connected` |
| 心跳 | 保留注释心跳行，不能被代理缓冲或压缩吞掉 |
| 事件 ID | 保留 `id`，重连时转发 `Last-Event-ID` |
| 会话重放 | 支持 `after`，先重放再进入实时流 |
| 空闲 | 空闲不是错误，不得变成 502 |
| 浏览器取消 | 取消上游订阅并清理 queue/task/future |
| 断线 | 自动重连；有副作用请求不能重复提交 |
| Gateway 重启 | 浏览器可恢复，状态最终一致 |

## WebSocket / PTY 验收矩阵

- PTY ticket 必须一次性消费，不能在 Relay/P2P 切换中重复使用。
- PTY cursor 必须保留 `-1`、非负游标和 `0x00 + JSON {cursor}` 控制帧。
- 服务端输出的历史分片、控制帧、实时输出和关闭帧必须保持顺序。
- 输入文本帧和二进制帧都必须保持原始内容。
- 保留关闭码 `1000`、`1001`、`4404` 及关闭原因。
- 所有底层 WebSocket 发送必须经过单 writer，禁止多协程直接并发发送。

## 取消和最终状态验收

每个 prompt/sub-agent 场景都必须证明：

```text
浏览器取消
→ Mesh 收到 interrupt/abort
→ Agent 收到取消
→ 真实 OpenCode 停止任务
→ OpenCode 发布最终状态事件
→ Mesh 转发最终事件
→ 远端页面显示 stopped/aborted
→ 刷新后状态仍一致
```

## 三路径要求

所有实际使用能力都必须记录：

1. LAN 直连：浏览器数据请求不经过 VPS。
2. P2P：VPS 只传认证和信令，不承载业务数据。
3. Relay：HTTP、SSE、WebSocket、PTY 全部可用，是外网和手机的最终兜底。

每次路由切换必须记录当前路径、失败原因、RTT、建链时间和是否发生过重试。当前浏览器可从 `window.__ocmTransport.pc.getStats()` 取得 candidate pair；后续应把这些字段接入可见诊断信息。
