# V2 最小传输 Implementation Plan

**Goal:** 在原生 V2 多 Server 基础上打通透明 P2P 与 Relay。

**Architecture:** 复用现有传输库；首先找到实际请求失败边界，再收敛状态和前端兼容逻辑。

**Tech Stack:** Python、FastAPI、httpx、websockets、aiortc、浏览器 WebRTC。

**Spec:** `docs/superpowers/specs/2026-09-23-v2-minimal-transport-design.md`

## Global Constraints

- 保留数据库和用户配置，保留 README 的 V1 基线。
- 不自动重放结果未知的 mutation；不通过改写业务请求体掩盖传输错误。
- 不要求手机安装 VPN；优先复用成熟库。

## Review Focus

- Request 与 init 的请求头/请求体覆盖语义。
- HTTP hop-by-hop 头与重新编码后的长度。
- 204、HEAD、取消及 SSE 生命周期。
- 设备切换时在途请求和终端的归属。
- 用户编辑 Server 名称后不会被设备同步覆盖。

## 执行记录

- [x] 核实 V1 基线并记录 README。
- [x] 抓取实际浏览器请求，与原生及 Relay/P2P 对比。
- [x] 为确定根因编写失败行为测试，实施最小修复。
- [x] 精简 V1 页面切换与传输模拟，验证原生 Server 身份和请求通道绑定。
- [x] 执行 pytest、Node 行为与语法检查、浏览器消息及终端检查。
- [x] 尝试独立审阅（供应商限流失败），完成自审与部署，记录可验收项及限制。

## 调查记录与执行决定

- 采用主 agent 内联执行，子 agent 仅负责有边界的调查和测试。沿用用户授权的当前主线部署工作区。
- 真实 Chromium 新建验收会话：P2P 下发消息并得到 `MESH_OK`；禁用浏览器 RTC 后同样操作 `/agent` 返回 400，消息未提交。
- 同一上游无效 session 请求：普通 JSON 返回带 Content-Type 的 JSON 400；加入 Transfer-Encoding: chunked 后返回无 Content-Type 的空 400。httpx 构造后同时存在 Content-Length 与 Transfer-Encoding。
- Ruling: 先修复已证实的逐跳 HTTP 头转发，再精简前端 — 避免把大规模替换当成根因修复；代价是分两步验证部署。
- 浏览器测试依赖安装在 `/tmp/opencode/browser-check`，不进入产品运行依赖。
- 请求透明修复提交 `5b9db76`；原生 Server 保留、WSS 作用域、通道绑定和删除接口模拟提交 `f4f93ac`。
- Node 行为测试实证：异步上传期间更换通道原先会把 mutation 发到新设备；修复后拒绝旧请求，取消消息也绑定原通道。
- 自审发现取消请求和超过 32 MiB 的 Request 回退问题：加入取消检查，Relay 使用仍未消费的 Request；测试分别先失败再通过。流式首帧与发送等待统一用 Promise.all，避免发送失败产生未处理的首帧拒绝。
- 独立 reviewer 调用因模型服务 Rate limit exceeded 失败；本轮由主 agent 另作自审，不能记作独立审阅通过。
- Ruling: 保留单条当前设备 P2P 通道，其他显式 Server 请求走各自 Relay — 避免引入多连接池重写；代价是后台非当前 Server 不享受直连优化。
- Ruling: 在已发 mutation 中断时拒绝并交由用户确认，不自动重放 — 防止未知结果重复执行；代价是偶发断线可能需要手动重试。
- 浏览器已验证两台设备的 P2P/Relay 终端初始提示符，且实际输入 `printf` 和 `hostname` 得到对应设备输出；RTC 候选对 bytesSent/bytesReceived 增长，确认不是仅显示 P2P 标签。
- 模型验收遇到独立的供应商限制：免费模型返回限流、ehang 的 GLM 返回账户余额不足；这些是已成功发送后原样显示的模型错误，不是 UnsupportedContentType。继续用现有可用模型验证完整回复。
- 真实首页切换暴露 SDK `new URL('/api/...', baseUrl)` 丢弃设备前缀，旧会话后台请求被选中设备接管。新增限定于同源 Mesh 基址的 URL 构造适配，保留明确设备；裸 origin 固定属于默认 Server。Node 测试先失败再通过，浏览器复测旧会话跨设备 404 消失。
- Ruling: 在 URL 构造阶段补足 Mesh 基址，而不是根据当前页面猜测请求设备 — 此处仍能获得明确 Server；代价是增加一个范围受限的全局 URL 子类，需要在未来升级 SDK 时复验。

## 最终验收记录（2026-09-23）

产品代码 revision：`7aaa2ff45618a1da521456f25697afdae402cd30`，Mesh `0.2.0`，两台 OpenCode `2.0.6`。

| 场景 | 实际结果 |
| --- | --- |
| GTi15 P2P 消息和事件流 | GPT-6 Astra / low 回复本次唯一标记 `MESH_dda48c553e22` |
| GTi15 Relay 消息和事件流 | 禁用 RTC 后回复本次唯一标记 `MESH_783b2f59940c` |
| GTi15 P2P / Relay 终端 | 创建、connect-token、连接及键盘输入均通过；printf 和 hostname 返回预期标记及 GTi15-Ultra |
| ehang P2P / Relay 终端 | 相同操作通过，hostname 返回 ehang-box |
| ehang 消息 | 两种传输均送达，Big Pickle 返回 Rate limit exceeded；尚未取得完整助手回复 |
| 直连证据 | 两台均有 open DataChannel、成功的 nominated ICE candidate-pair 及实际收发字节 |
| 直连失效回退 | 主动关闭已 open 通道并禁止新 RTC 后，后续 `/api/info` 通过 Relay 返回 200 与正确设备信息 |
| 原生多 Server 切换 | 首页 GTi15 → ehang，显示 ehang 会话列表；打开已有 ehang 会话 URL，返回首页再切回 GTi15；未出现跨设备 404 |
| 自动测试 | 112 项通过，包含真实 Node URL/Request/Abort/通道行为及 Agent 请求字节透传 |

本地浏览器日志：`/tmp/opencode/v020-final-switch.log`、`v020-final-relay.log`、`v020-final-ehang.log`、`v020-final-ehang-p2p.log`。日志属于本地诊断资料，不打包公开。

用户验收剩余项：手机开启 v2rayNG 时的真实网络打洞情况；ehang 使用可用模型后的完整回复。当前环境不能替代这两项验收。独立代码审阅未完成，原因是 reviewer 模型限流；不能将自审描述为独立审阅通过。
