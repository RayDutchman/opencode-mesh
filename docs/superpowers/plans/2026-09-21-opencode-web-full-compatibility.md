# OpenCode Web 完整兼容与快速数据面实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保持 `https://oc.252327.xyz:8443/` 作为统一访问入口的前提下，完整兼容当前 OpenCode Web 实际使用的数据面，并按局域网直连、P2P、VPS Relay 的顺序选择最快可用路径。

**Architecture:** VPS 提供静态资源、认证、设备发现、路由协商和最后的 Relay fallback；WSL Agent 连接真实 `http://10.0.0.101:40960`。浏览器运行时通过统一 transport adapter 路由 HTTP、SSE、PTY WebSocket 和普通 WebSocket，LAN 优先，P2P 次之，Relay 最后。OpenCode 的原始 HTTP、SSE、WebSocket、认证头、事件 ID、PTY cursor/ticket 和取消语义保持不变。

**Tech Stack:** Python 3、FastAPI/Uvicorn、httpx、websockets、原生 OpenCode Web UI、浏览器 Service Worker/Fetch/EventSource/WebSocket 适配、WebRTC DataChannel（P2P 阶段）。

## Global Constraints

- 真实 OpenCode 服务固定为 `http://10.0.0.101:40960`，不得重启、修改或把它当作可中断的测试服务。
- 不修改 VPS 的 Xray、Hysteria、FRP、Lucky、防火墙、网络配置，不重启 VPS。
- Gateway 的临时登录密码为 `password`，所有链路验收通过后再修改正式密码。
- 不引入模拟 OpenCode 作为验收依据；每项功能必须访问真实 OpenCode。
- 保留 HTTP/1.1、SSE、WebSocket、PTY 原始语义，不将所有流统一粗暴转换成普通 JSON 响应。
- 每项能力必须记录 LAN、P2P、Relay 三种路径的状态；P2P 未可用时必须自动回退 Relay。
- 所有代码注释使用中文，函数和变量命名使用英文。
- 不自动提交 git commit。

---

### Task 1: 固化 OpenCode Web 能力矩阵

**Files:**
- Create: `docs/opencode-web-capability-matrix.md`
- Create: `docs/opencode-web-route-catalog.json`
- Modify: `docs/protocol.md`

**Interfaces:**
- Consumes: OpenCode `GET /doc`、当前版本 `1.18.30` 的真实 Web Network 记录、OpenCode Web 源码中的 `/api/*`、legacy API、SSE 和 PTY 协议。
- Produces: 每个请求的 method/path/headers/body/response/stream/lifecycle/route-fallback/verification 字段。

- [ ] **Step 1: 记录真实 API 文档**

使用当前认证会话请求 `GET /doc`，保存去除凭据后的路由目录；同时记录浏览器实际发出的请求，覆盖页面加载、会话、prompt、停止、工具、sub-agent、权限、文件、PTY、设置和刷新。

- [ ] **Step 2: 编写能力矩阵**

矩阵至少包含：静态资源、普通 HTTP、全局 SSE、会话 SSE、PTY WebSocket、普通 WebSocket、文件下载、Data URI 上传、prompt、interrupt/abort、permission、question、tool、sub-agent、session status、reconnect、Last-Event-ID、PTY cursor 和 ticket。

- [ ] **Step 3: 为每项能力定义三路径证据**

每项能力必须规定 LAN、P2P、Relay 的成功响应、断线行为、取消行为和刷新后的最终状态。

- [ ] **Step 4: 验证文档完整性**

执行检查，确保矩阵中的每个 OpenAPI 路由组都有 transport、生命周期和验收用例；未覆盖的路由必须列出明确原因，而不是留空。

### Task 2: 建立可测试的 Relay 基线

**Files:**
- Modify: `src/main.py`
- Create: `tests/test_relay_protocol.py`
- Create: `tests/test_stream_lifecycle.py`

**Interfaces:**
- Consumes: Task 1 的 route catalog。
- Produces: `GatewayBridge`, `PendingRequest`, `StreamSession` 和统一的 disconnect/cancel cleanup 行为。

- [ ] **Step 1: 为请求状态和流状态写失败测试**

测试必须覆盖：请求超时清理 pending、Agent 断线失败 pending、浏览器取消请求、SSE 首帧/空闲/结束、重复 response、迟到 response、WebSocket 关闭和 PTY ticket/二进制帧。

- [ ] **Step 2: 将 Agent 消息接收与请求处理解耦**

接收循环只负责解析和分发；每个普通 request、stream request、WS/PTY 控制事件使用独立任务；所有任务在连接关闭时集中取消。

- [ ] **Step 3: 为同一 WebSocket 建立单写入队列**

普通响应、SSE chunk、PTY 数据、heartbeat 不得并发直接调用底层 `send`；通过单一 writer 保证消息和帧顺序。

- [ ] **Step 4: 完成取消和清理**

浏览器断开、HTTP request task 取消、SSE generator 退出、Agent 断线和 Gateway 关闭必须释放对应 future、queue、task 和 stream registry。

- [ ] **Step 5: 验证 Relay 基线**

使用真实 OpenCode 依次验证 `/global/health`、`/project`、`/provider`、`/path`、`/global/event`、session list、prompt、interrupt、权限和 PTY；所有测试通过后才进入 LAN/P2P。

### Task 3: 抽象浏览器数据面 Transport Adapter

**Files:**
- Modify: `src/main.py`
- Create: `src/transport_manifest.py`
- Create: `src/static_adapter.py`
- Create: `tests/test_transport_manifest.py`
- Create: `tests/test_static_adapter.py`

**Interfaces:**
- Consumes: Relay endpoint、设备认证 Cookie、路由候选和 Task 1 route catalog。
- Produces: `/_mesh/transport-manifest`、浏览器可加载的 transport adapter、候选探测和 fallback 状态。

- [ ] **Step 1: 定义 manifest**

Manifest 必须声明：协议版本、设备 ID、LAN 候选、P2P signaling 地址、Relay 地址、认证方式、支持的 HTTP/SSE/WS/PTY 能力、候选有效期和当前优先级。

- [ ] **Step 2: 实现候选健康探测**

探测必须使用真实 OpenCode 的 `/api/health` 或 `/global/health`，记录 RTT、TLS/CORS/Origin 失败和超时；健康探测失败不得影响 Relay。

- [ ] **Step 3: 实现 Fetch/XHR 路由**

保留 method、query、body、Authorization、目录/workspace headers、Content-Type、Content-Encoding、Range、AbortSignal 和原始错误；失败后只能在请求尚未产生不可重试副作用时切换路径。

- [ ] **Step 4: 实现 SSE 路由**

保留 `text/event-stream`、心跳、事件 ID、`Last-Event-ID`、AbortSignal 和自动重连；切换路径时不得重复提交 prompt，只允许重连事件订阅或使用明确的事件游标。

- [ ] **Step 5: 实现 WebSocket/PTY 路由**

保留文本/二进制帧、PTY cursor 控制帧、一次性 ticket、关闭码、关闭原因和重连；普通 HTTP fallback 不能替代 WebSocket。

- [ ] **Step 6: 以实际静态页面验证 adapter 注入**

只允许在静态 HTML/JS 加载阶段注入 adapter，不修改真实 OpenCode API 数据；页面必须在 adapter 未加载或 manifest 不可用时继续使用 Relay。

### Task 4: LAN 直连路径

**Files:**
- Modify: `src/main.py`
- Modify: `config/agent.local.json`
- Create: `deploy/opencode-mesh-lan.service.example`
- Create: `tests/test_lan_route.py`

**Interfaces:**
- Consumes: Task 3 transport manifest and browser adapter。
- Produces: LAN HTTPS/HTTP、SSE、WebSocket、PTY compatible endpoint with same authentication and route semantics。

- [ ] **Step 1: 选择并验证 WSL mirrored LAN address**

只读确认浏览器所在 LAN 能访问 `10.0.0.101`；不得绑定或修改真实 OpenCode 的 `40960` 监听配置。

- [ ] **Step 2: 提供同源可验证入口**

LAN endpoint 必须能通过合法 TLS/Origin/CORS 配置承载 `/api/*`、legacy API、SSE 和 PTY WS；认证失败必须返回原始 401/WWW-Authenticate 语义。

- [ ] **Step 3: 验证 LAN 优先选择**

同 LAN 时记录浏览器实际数据请求未经过 VPS；断开 LAN 后同一页面自动回退 P2P 或 Relay。

### Task 5: P2P 数据路径

**Files:**
- Modify: `src/main.py`
- Create: `src/p2p_signaling.py`
- Create: `src/p2p_transport.py`
- Create: `tests/test_p2p_signaling.py`
- Create: `tests/test_p2p_transport.py`

**Interfaces:**
- Consumes: VPS authentication, device registry, signaling manifest, Task 3 adapter。
- Produces: browser-to-WSL authenticated transport for HTTP request/response, SSE ordered events, WebSocket/PTY bidirectional frames and close semantics。

- [ ] **Step 1: 定义信令会话**

VPS 只传递经过认证的 offer/answer/ICE candidate，不承载业务数据；会话必须有短期 token、设备 ID、过期时间和单次用途。

- [ ] **Step 2: 定义 DataChannel framing**

消息必须区分 request、response、stream-open、stream-chunk、stream-end、cancel、ws-open、ws-frame、ws-close 和 error；二进制数据不得强制转换为 Unicode。

- [ ] **Step 3: 实现重连和 fallback**

P2P 建立失败、ICE 超时、DataChannel 关闭或 peer 不支持时，adapter 必须回到 Relay；已产生副作用的 prompt 不得盲目重试。

- [ ] **Step 4: 在两类网络环境验证**

验证同 LAN、不同 NAT/外网、手机网络；记录实际路径、RTT、建链时间、失败原因和最终 fallback。

### Task 6: 完整业务生命周期验收

**Files:**
- Create: `tests/e2e/test_opencode_web_matrix.py`
- Create: `tests/e2e/scenarios.json`
- Modify: `docs/opencode-web-capability-matrix.md`

**Interfaces:**
- Consumes: Task 1 route catalog and Tasks 2–5 transports。
- Produces: 可重复的真实 OpenCode Web 验收报告。

- [ ] **Step 1: 页面和会话场景**

验证登录、设备选择、项目、provider、model、session list、创建、刷新、恢复和多标签页。

- [ ] **Step 2: 生成和事件场景**

验证 prompt、流式 assistant、tool、permission、question、sub-agent、全局事件、会话事件、心跳、Last-Event-ID 和事件重放。

- [ ] **Step 3: 停止和一致性场景**

验证 `/api/session/:sid/interrupt`、legacy `/session/:sid/abort`、本地任务终止、最终事件、远端 UI 状态、刷新后状态和重复点击停止。

- [ ] **Step 4: PTY 和文件场景**

验证 PTY 创建、输入、输出、resize、cursor 重连、关闭码、文件读取、目录查找、大文件和 Data URI 附件。

- [ ] **Step 5: 故障场景**

逐项断开 LAN、P2P、Relay、Agent、浏览器 SSE、PTY WS；确认正确 fallback、取消和资源清理。

- [ ] **Step 6: 长时场景**

执行至少 15 分钟 SSE、30 分钟混合请求、重复刷新/停止/重连，并检查 pending/task/queue/内存没有持续增长。

### Task 7: 正式切换与交付

**Files:**
- Modify: `config/agent.local.json`
- Modify: VPS Gateway 配置（仅 Mesh 配置）
- Modify: `docs/opencode-web-capability-matrix.md`

- [ ] **Step 1: 保存完整验收报告**

记录各能力三路径结果、失败路径、fallback 和未支持的 OpenCode 实验功能。

- [ ] **Step 2: 修改正式登录密码**

只在用户可通过公网正常使用、Relay/P2P/LAN fallback 和长时测试通过后执行；不得把新密码写入仓库或输出到日志。

- [ ] **Step 3: 最终公网验收**

从 WSL 本机、同 LAN 浏览器、外网浏览器和手机网络各执行核心场景，确认 URL 不变且路径按预期选择。
