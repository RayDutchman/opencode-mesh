# OpenCode Mesh 原理与架构

本文描述 **OpenCode Mesh 0.2.1** 的架构，当前以 **OpenCode V2.0.6** 为验证版本；部署与验收状态见[稳定性实施记录](./superpowers/plans/2026-09-23-v2-stabilization.md)。V1 稳定基线见根目录 README；当前开发线不再维护 V1 前端兼容。

本文是架构总览，不替代具体协议和部署手册：

- 当前维护范围、已知限制与交接方法见 [maintenance.md](maintenance.md)。本文及协议描述当前行为，历史 plans/specs 仅作有日期和版本的证据。
- 传输消息、分片信封和错误语义见 [`protocol.md`](./protocol.md)。
- V2 设计边界见 [`V2 最小传输适配设计`](./superpowers/specs/2026-09-23-v2-minimal-transport-design.md)，实测结果与限制见 [`执行与验收记录`](./superpowers/plans/2026-09-23-v2-minimal-transport.md)。
- [`opencode-web-capability-matrix.md`](./opencode-web-capability-matrix.md) 和路由目录保留历史资料，不代表当前 V2 的完整验收范围。
- 安装、卸载和配置操作见根目录 [`README.md`](../README.md)。

## 1. 项目目标

OpenCode Mesh 将多台设备上的 OpenCode 统一暴露到一个公网 Gateway。用户只需要访问一个 Web 地址，就可以在 OpenCode 原生 Web UI 中选择不同设备，并访问对应设备上的项目、会话、终端和事件流。

项目本身不修改 OpenCode 的业务协议，也不要求每台设备开放公网监听端口。它在 OpenCode Web UI 和本机 OpenCode 之间增加一层设备发现、身份认证、传输选择和故障恢复能力。

Server 名称、项目、会话、模型选择和终端界面交给 OpenCode 原生管理。手机只需浏览器，不要求安装 VPN 客户端；能否直连仍取决于实际网络和 WebRTC 可达性。

核心目标有四个：

1. 让 Agent 所在设备只需要主动向外连接，降低设备暴露面。
2. 在浏览器和设备之间优先使用 WebRTC DataChannel，减少公网 Gateway 的带宽和延迟。
3. 当 P2P 不可用时，由 Gateway Relay 继续提供完整访问能力。
4. 对断线、重连、超时、过大消息和并发流进行有界处理，避免请求永久挂起或内存无限增长。

## 2. 总体架构

```text
                         公网 HTTPS
浏览器/OpenCode Web UI --------------------> Gateway
     │                                         │
     │ WebRTC DataChannel（优先）              │ 长期 WebSocket 控制连接
     │                                         │
     └────────────────────────────────────────> Agent
                                               │
                                               │ HTTP / WebSocket
                                               ▼
                                      本机 OpenCode 服务
                                      127.0.0.1:4096 等
```

系统包含四类运行角色：

| 角色 | 部署位置 | 主要职责 |
|---|---|---|
| Gateway | 公网服务器 | 浏览器认证、设备注册、设备路由、Relay 中继、P2P 信令 |
| Agent | 每台运行 OpenCode 的设备 | 注册设备、维持控制连接、代理本机 HTTP/WebSocket、响应 P2P 请求 |
| 浏览器适配层 | Gateway 返回的 OpenCode HTML 内 | 保留 Server URL 作用域，适配 fetch/WebSocket，选择 P2P 或 Relay |
| OpenCode | Agent 所在设备 | 实际提供项目、会话、终端和事件流业务 |

Gateway 和 Agent 使用同一套 Python 入口，通过 `--mode` 选择运行角色：

```bash
python -m src.main --mode gateway --config config/gateway.json
python -m src.main --mode agent --config config/agent.json
```

Gateway 由 FastAPI/uvicorn 托管；Agent 是 asyncio 常驻进程。Agent 不监听公网端口，只主动连接 Gateway 和本机 OpenCode。

### 2.1 VPS 同时运行 OpenCode

本节为部署设计边界，本轮未在 VPS 上安装 OpenCode 或同机独立 Agent。

同一 VPS 可以同时运行 Gateway 和一套 OpenCode，但两者仍是独立角色：另起一个 Agent，将 `opencode_url` 指向该 VPS 的本地 OpenCode，例如 `http://127.0.0.1:4096`（按实际监听端口配置），再注册到 Gateway。浏览器以该 Agent 的明确 `device_id` 访问，不将 Gateway 的 origin 当作此 OpenCode 的身份，也不为 VPS 增加特殊业务路由。

每个 Agent 必须使用独立 `state_file`，因为设备身份和 Agent token 保存在其中；同机多个 Agent 不能共享它。设备显示名用于辨认，不用于判断身份。Gateway 浏览器认证与本地 OpenCode 认证分别配置。

## 3. 两条传输路径

### 3.1 P2P 直连

P2P 适合浏览器和 Agent 网络条件允许直接建立 WebRTC 连接的场景。业务数据不经过 Gateway 的应用层中继。

建立过程如下：

1. 浏览器从 Gateway 获取 transport manifest，得到设备信息、能力和 STUN 配置。
2. 浏览器创建 `RTCPeerConnection` 和有序 DataChannel。
3. 浏览器把 WebRTC offer 提交给 Gateway。
4. Gateway 通过 Agent 的长期控制连接转发 `p2p_offer`。
5. Agent 使用 aiortc 创建 answer，再通过 Gateway 返回给浏览器。
6. DataChannel 打开后，浏览器适配层开始通过 P2P 发送 HTTP、SSE 和 WebSocket 业务消息。

P2P 不是另一套业务协议。它和 Relay 使用相同的请求、响应、流和 WebSocket 语义，区别只在于承载方式。

当前浏览器适配层维护一条面向当前设备的 P2P 通道。后台访问其他明确 Server 的请求使用各自的 Relay，不会借用当前设备通道。页面和静态资源仍由 Gateway 提供；“P2P”不表示所有流量都绕过 VPS。

P2P 不可用时按请求所处阶段处理：

- 尚未通过 P2P 发送的请求使用 Relay，不等待 P2P 恢复。
- P2P 初始协商失败时按退避策略重试。
- 已建立的通道断开时清理旧状态，再按当前设备重新连接。
- 已通过旧通道发送的请求可能失败，不自动通过 Relay 重放；后续请求可走 Relay，详见 §12.3。

### 3.2 Gateway Relay

Relay 是可靠兜底路径，数据流如下：

```text
浏览器
  │ HTTP / WebSocket
  ▼
Gateway
  │ JSON 控制消息（长期 Agent WebSocket）
  ▼
Agent
  │ HTTP / WebSocket
  ▼
本机 OpenCode
```

Gateway 不为每个设备建立新的公网监听端口，而是把多个浏览器请求复用到该设备的一条 Agent 控制连接上。控制消息携带请求 ID，Gateway 用 pending future 或流状态把 Agent 返回的数据交给对应浏览器请求。

Relay 主要处理四类业务：

- 普通 HTTP：请求和响应完整传输，带有请求/响应大小限制。
- SSE：通过首帧状态、数据帧和结束帧保持流式传输。
- WebSocket：通过 `ws_open`、`ws_data`、`ws_close` 等控制消息实现双向桥接。
- 取消和断连：浏览器、Gateway 或 Agent 任一侧取消时，向另一侧发送取消消息并清理状态。

## 4. Gateway 的职责

Gateway 是系统的公网入口和控制平面，主要职责如下。

### 4.1 浏览器认证和设备路由

浏览器使用 HTTP Basic Auth 访问 Gateway。认证通过后，浏览器可以查看设备列表并访问指定设备。

请求可以显式指定设备：

```text
/_mesh/device/{device_id}/...
```

V2 页面使用原生路由：

```text
/server/{base64url(server_url)}/session/{session_id}
```

其中 `server_url` 可以是 `https://gateway/_mesh/device/{device_id}`。Gateway 能解析该 Server 路由，并为设备页面重写静态资源地址；`/_mesh/device/{device_id}` 是请求基址，不能直接当作 V2 前端页面路由。

没有设备上下文的页面入口由 Gateway 根据 `default_device` 和在线状态选择前端资源来源，Gateway 本身不注册为业务 Server。直接访问裸 origin 的 `/api/*` 返回 `400 device_required`；业务请求使用明确设备基址。显式设备请求不会因默认设备变化而改投另一台设备。旧 origin 会话书签仅对同源页面跳转迁移到默认设备的明确地址。

设备状态由控制连接、心跳和最后活跃时间共同决定，过期连接不会继续被当作健康设备使用。

### 4.2 Agent 注册和控制连接

Agent 首次启动时使用共享的 `enroll_token` 注册，Gateway 返回设备专属 `agent_token`。后续控制 WebSocket 使用 `X-Mesh-Agent-Token`，不把凭据放入 URL。

控制连接承担以下功能：

- 设备在线状态和心跳。
- Relay 请求转发。
- P2P offer/answer 信令转发。
- 浏览器 WebSocket 桥接。
- 取消、错误和断连通知。

同一个 `device_id` 建立新连接时，Gateway 会关闭旧连接，并使用代际检查防止旧连接的尾部消息污染新连接的状态。

### 4.3 Relay 生命周期管理

Gateway 为每个请求维护有限生命周期：

1. 创建请求 ID 和 pending 状态。
2. 在控制连接上有界发送请求。
3. 等待 Agent 响应或流事件。
4. 超时、取消、连接断开或响应完成时释放状态。

发送、响应读取、流队列和 P2P 信令均有超时或大小上限。流队列溢出时产生显式错误，而不是静默删除事件。

## 5. Agent 的职责

Agent 是设备侧的连接器和协议代理，负责把 Gateway 请求转换为本机 OpenCode 请求。

### 5.1 连接生命周期

Agent 启动后执行以下循环：

```text
读取本地状态
  ↓
注册或恢复设备身份
  ↓
建立 Gateway 控制 WebSocket
  ↓
发送心跳并处理控制消息
  ↓
连接断开时清理状态
  ↓
指数退避后重新注册和连接
```

设备身份和 Agent token 持久化在本地状态文件中。Gateway 状态变化或 token 不一致时，Agent 使用受保护的恢复流程重新取得设备控制权。

### 5.2 本机 OpenCode 代理

Agent 不理解 OpenCode 的业务语义，只负责传输和边界保护：

- 使用 httpx 请求本机 OpenCode HTTP 接口。
- 对响应执行有界流式读取，避免一次性把无限大小的响应读入内存。
- 使用 websockets 连接本机 WebSocket/PTY 接口。
- 过滤不应跨越边界的认证、Cookie、Host 和代理头。
- 对请求、响应、流和 WebSocket 数据执行大小限制和错误转换。

请求体从传输信封解码后按原始字节交给 httpx，不补空 JSON，也不转换模型字段。请求已重新组装，因此必须剥离 `Content-Length`、`Transfer-Encoding` 等旧分帧头、其他逐跳头以及 `Connection` 声明的头，由 HTTP 库生成新连接的分帧。否则同时带上长度和 chunked 编码会导致上游返回空 400，V2 SDK 随后可能报 `UnsupportedContentType`。

上游 HTTP 请求使用 `Accept-Encoding: identity`。响应过滤旧长度、编码、逐跳头和 `Set-Cookie`，与实际返回字节保持一致；不把 HTML 或空错误体伪装成业务 JSON。

### 5.3 P2P 应答和资源清理

Agent 接收 Gateway 转发的 P2P offer，使用 aiortc 创建 answer，并为每个 P2P peer 保存有限的装配器、序列号和任务状态。

控制连接重建、DataChannel 关闭、请求取消和超时都会清理：

- P2P peer 和后台任务。
- 未完成的消息装配。
- pending 请求和浏览器 WebSocket 桥。
- 发送序列和接收队列。

## 6. 浏览器适配层

Gateway 只对符合条件的 OpenCode HTML 页面注入 `src/static_adapter.py` 中的 JavaScript。适配层保留原生业务界面，补充设备入口、请求作用域和网络传输。

### 6.1 浏览器接口边界

| 浏览器 API | 适配行为 |
|---|---|
| `fetch` | 优先走 P2P，不可用时使用原生请求走 Relay |
| `WebSocket` | 通过 P2P 或 Relay 桥接文本、二进制和 subprotocol |
| `URL` | 仅在同源 Mesh Server 基址与绝对 `/api/...` 路径组合时保留设备前缀，其他情况沿用原生解析 |
| `XMLHttpRequest` / `EventSource` | 保留原生实现，不再提供自定义模拟类 |

V2 SDK 的 SSE 使用 fetch 流。Mesh 转发流式响应状态、头和数据，由 SDK 处理事件解析及业务重连，不另造 EventSource 语义。

Mesh 生成的协议/上游错误均声明 JSON 类型。流式首帧前失败返回与 Relay 一致的 HTTP 错误；收到首帧后发生故障，只能中断已开始的响应流，不能再替换状态码。

状态栏显示当前设备、在线状态、P2P/Relay 和对应 RTT。它描述当前设备的传输状态，不代表其他 Server 的后台请求也走同一通道。

### 6.2 请求设备归属

设备身份在选择 P2P 或 Relay **之前**确定：

1. 明确的 `/_mesh/device/{id}/...` 请求保持原目标。
2. V2 SDK 使用 `new URL('/api/...', serverUrl)` 时，原生 URL 解析会丢弃基址路径。适配层在此刻保留 Mesh 设备前缀，避免等到 fetch 时再猜目标。
3. 启动时原生内建 Server 已是明确设备基址；旧客户端裸 origin 请求的适配仍固定绑定默认设备，不随页面选择切换。
4. 跨源外部 Server 保持原地址，交给原生传输，不进入 Mesh P2P。

例如，同时访问 A 的旧会话和 B 的会话列表时，两条请求各自保留 A/B 的明确地址；不能因为首页刚选了 B，就把 A 的请求也发到 B。页面和选中 Server 用于选择当前 P2P 连接及显示状态，不覆盖请求中已有的 Server 身份。

### 6.3 原生 Server 列表与设备切换

设备发现通过 `/_mesh/devices` 获取设备，再探测在线设备的 `/api/info`。JSON 响应且版本以 `2.` 开头的设备会被补充到原生 Server 列表。配置中的主设备身份在暂时离线/探测超时时仍保留，不改指另一台设备。按 URL 去重，不覆盖用户名称、外部地址或其他存储字段。

V2.0.6 原生入口硬编码 `location.origin`，没有外部启动配置钩子。`src/frontend.py` 因而只在版本隔离的 `/_mesh/ui/1/{device_id}/_assets/...` 资源命名空间中，精确替换已验证的唯一入口 getter，并让入口模块等待 `window.__ocmBootstrap.ready`。新命名空间防止复用旧 immutable 资源。若入口契约变化则返回明确错误，而不是猜测替换其他 JavaScript。

发现与存储迁移在原生应用启动前完成，不再迟到刷新用户正在编辑的页面。仅移除同源 origin 的重复 Server 列表项，迁移其默认选择、首页选择和已知 PWA 旧路由；明确设备条目优先保留用户改名。原生 canonical Server 改为明确设备后，既有 `local` 项目状态保留给原生迁移逻辑处理；不清空 localStorage 或数据库。

设备切换时，适配层会：

1. 更新当前路由设备 ID。
2. 关闭旧设备的 P2P peer、流和 pending 请求。
3. 按新设备重新获取 manifest 和建立传输。
4. 在新连接尚未就绪时，让请求继续通过 Relay。

请求和 WebSocket 捕获创建时的通道；异步读取上传体、发送二进制帧或取消请求时，仍使用该通道，而不是后来切换出的全局通道。旧通道不可用时报告失败，不把旧请求发给新设备。

WebSocket 在 `ws_open` 发出前被关闭时，本地确定终止并清理；已发出后则按发送队列顺序关闭，迟到数据不再交给终端，终止事件幂等。

## 7. 消息分片与可靠性边界

P2P DataChannel 和 Agent 控制 WebSocket 都需要面对单帧大小、缓冲区和断线问题。因此 P2P 载荷使用统一信封：

```json
{
  "message_id": "logical-message-id",
  "sequence": 0,
  "data": "base64 payload",
  "final": true
}
```

其核心规则是：

- `message_id` 隔离并行逻辑消息。
- `sequence` 必须从 0 开始严格递增。
- `final` 只表示当前逻辑消息完成。
- `data` 使用严格 base64 编码。
- 请求、响应、流和 WebSocket 数据均受统一大小上限约束。
- 未完成装配、完成墓碑和错误墓碑都受数量和 TTL 限制。
- DataChannel 背压等待超过超时后关闭当前 P2P 通道，使在途操作失败、后续请求可走 Relay，不重放结果未知的请求。

浏览器 P2P 请求体上限为 32 MiB，为 base64 和 JSON 信封开销留出空间。已知超限的请求零读取转 Relay；未知大小的流由唯一 reader 读取至超过上限（最多保留上限加一个源块），然后将已读块和剩余 reader 按序交给受消费者背压控制的 Relay 流。读取期间取消会立即释放源流，不使用 clone/tee 全量缓冲。这不是已发送请求的重试，Relay 自身的大小限制仍然有效。

具体字段、控制消息类型和稳定错误 reason 以 [`protocol.md`](./protocol.md) 为准。

## 8. 认证边界

系统有三层身份边界：

```text
浏览器 --Basic Auth--> Gateway
Agent  --enroll_token--> Gateway 注册
Agent  --agent_token--> Gateway 控制 WebSocket
```

边界原则如下：

- Gateway 的 Basic Auth 只用于浏览器访问。
- `enroll_token` 只用于设备加入 Mesh，不转发给本机 OpenCode。
- `agent_token` 只用于指定设备的控制连接。
- Gateway 的浏览器认证头、Cookie、Host 和代理链路头不会被转发到 Agent。
- 配置和状态文件使用私有权限保存。
- 生产环境默认要求 HTTPS；明文 Gateway 只用于明确允许的内网测试。

## 9. 故障恢复模型

故障恢复不是由单一组件完成，而是按边界分层处理：

| 故障 | 处理方式 |
|---|---|
| P2P 初始协商失败 | 当前请求走 Relay；后台按退避策略重试 |
| P2P DataChannel 断开 | 清理 peer 和 pending 状态，重新协商 |
| Agent 控制连接断开 | Gateway 标记设备离线；Agent 清理旧任务后退避重连 |
| 控制消息发送超时 | 关闭异常连接并让对应请求失败，不永久等待 |
| HTTP/SSE 响应过大 | 在边界处拒绝或中断，返回稳定错误 |
| 流队列溢出 | 发送显式流错误，避免静默丢事件 |
| 请求被取消 | 双向传播 `cancel`，释放 future、队列和装配器 |
| 设备切换 | 重建当前 P2P 连接；每个请求仍按自身明确 Server 路由，其他 Server 可走 Relay |

可靠性回归测试位于 `tests/test_mesh_reliability.py`，覆盖分片顺序、大小限制、重复结束、P2P 状态清理和真实 Agent 消息处理路径。`tests/test_v2_transport.py` 使用实际 Node URL/Request/Abort 行为和 Agent HTTP 请求捕获，覆盖设备作用域、上传体、取消、逐跳头及原生 Server 名称保留。

## 10. 文件架构

```text
opencode-mesh/
├── pyproject.toml
├── README.md
├── CHANGELOG.md
├── .github/workflows/
├── src/
│   ├── __init__.py
│   ├── main.py
│   ├── frontend.py
│   ├── p2p.py
│   └── static_adapter.py
├── config/
│   ├── gateway.example.json
│   └── agent.example.json
├── deploy/
│   ├── Caddyfile.example
│   ├── opencode-mesh-gateway.service.example
│   └── opencode-mesh-agent.service.example
├── scripts/
│   ├── install.sh
│   ├── uninstall.sh
│   ├── deploy-agent.sh
│   ├── deploy-release.sh
│   ├── bootstrap.sh
│   └── check_auth.py
├── docs/
│   ├── architecture.md
│   ├── protocol.md
│   ├── opencode-web-capability-matrix.md
│   └── opencode-web-route-catalog.json
└── tests/
    ├── test_mesh_reliability.py
    ├── test_v2_transport.py
    ├── test_v2_bootstrap.py
    ├── test_v2_errors.py
    ├── test_v2_upload_backpressure.py
    └── test_v2_websocket.py
```

### 10.1 Python 核心代码

#### `src/main.py`

项目运行入口和主要业务编排代码。它同时包含 Gateway 和 Agent 两个运行角色：

- Gateway 路由、Basic Auth、设备注册、设备选择。
- Relay HTTP/SSE/WebSocket 代理。
- Agent 控制连接、心跳、注册和重连。
- Agent 本机 HTTP/WebSocket 代理。
- P2P 信令、消息收发和生命周期清理。
- HTML 注入入口和 uvicorn 启动参数。

这是当前项目最大的单体文件，职责按 `Gateway`、`Agent` 和公共辅助函数分区。

#### `src/frontend.py`

集中适配已验证的 V2 入口 getter，隔离静态资源缓存，并迁移旧 Gateway origin 页面书签。未知启动契约显式失败，不猜测改写其他模块。

#### `src/p2p.py`

P2P 和分片基础设施：

- 分片信封和 base64 编码/解码。
- `ChunkAssembler` 多消息重组器。
- sequence、大小、预算、TTL 和墓碑守卫。
- aiortc offer/answer 辅助逻辑。

该文件不负责设备路由，也不负责 OpenCode HTTP 业务。

#### `src/static_adapter.py`

注入浏览器的 JavaScript 源码。主要负责：

- P2P 建连、重连和设备切换。
- P2P/Relay 请求选择。
- fetch、WebSocket 适配及受限的 URL 基址保留。
- 原生 Server 列表的 V2 设备补充和请求设备归属。
- 浏览器侧分片信封发送和接收。
- fetch 响应流桥接、取消和传输状态栏。

### 10.2 配置与部署文件

- `config/gateway.example.json`：Gateway 配置模板，包括监听地址、认证、注册密钥和状态文件。
- `config/agent.example.json`：Agent 配置模板，包括 Gateway 地址、加入密钥、本机 OpenCode 地址和状态文件。
- `deploy/Caddyfile.example`：使用 Caddy 为 Gateway 提供 HTTPS 反向代理的示例。
- `deploy/*.service.example`：Gateway 和 Agent 的 systemd 服务模板。

真实配置、Agent token 和设备状态不应写入 Git；本地配置文件和 data 目录由 `.gitignore` 排除。

### 10.3 运维脚本

- `scripts/install.sh`：安装依赖、写入配置、生成 systemd 服务，并处理 root/普通用户两种安装范围。
- `scripts/uninstall.sh`：显式按 `agent`、`gateway` 或 `all` 卸载，Agent 模式会先尝试注销设备，并支持保留设备身份。
- `scripts/deploy-agent.sh`：通过 SSH 将 Agent 部署到远程 Linux 设备。
- `scripts/deploy-release.sh`：按已提交 revision 部署本机或远程 Gateway/Agent，保留源码备份并记录 `.mesh-revision`。
- `scripts/bootstrap.sh`：本地开发环境初始化。
- `scripts/check_auth.py`：使用 ASGI transport 验证认证、注册所有权、请求头隔离、注销和文件权限。

### 10.4 版本控制

`src/__init__.py` 中的 `__version__` 是唯一的软件版本来源，`pyproject.toml` 通过 setuptools 动态读取。Gateway 注入浏览器的状态栏显示该版本；`transport-manifest.version` 仍然是协议版本。发布使用 `vX.Y.Z` Git tag，`scripts/install.sh` 默认安装 `main`，设置 `MESH_VERSION=vX.Y.Z` 可固定到指定发布版本。

### 10.5 文档与测试

- `docs/architecture.md`：本文，解释系统原理和组件关系。
- `docs/protocol.md`：控制消息和 P2P 分片协议规格。
- `docs/opencode-web-capability-matrix.md`：历史 OpenCode Web 路径能力和验收矩阵。
- `docs/opencode-web-route-catalog.json`：历史机器可读路由目录。
- `tests/test_mesh_reliability.py`：可靠性回归测试。
- `tests/test_v2_transport.py`：V2 请求透明性和浏览器适配行为测试。

## 11. 启动和请求示例

### 11.1 启动 Gateway

```bash
python -m src.main --mode gateway --config config/gateway.json
```

Gateway 通常只监听本机地址，再由 Caddy 或 Nginx 暴露 HTTPS。

### 11.2 启动 Agent

```bash
python -m src.main --mode agent --config config/agent.json
```

Agent 启动后主动注册 Gateway，并连接本机 OpenCode。浏览器无需直接访问 Agent 的监听端口。

### 11.3 一次 Relay 请求的生命周期

```text
浏览器 fetch
  ↓
浏览器适配层固定 Server 目标，选择 Relay
  ↓
Gateway 生成 request id
  ↓
Agent 控制连接转发 request
  ↓
Agent 请求本机 OpenCode
  ↓
Agent 返回 response
  ↓
Gateway 校验大小、过滤头并返回浏览器
```

P2P 可用时，浏览器适配层会跳过 Gateway 的 Relay 数据路径，但请求语义、大小限制、取消和错误处理仍遵守同一套协议边界。

## 12. 设计取舍

### 12.1 为什么 Agent 不监听公网端口

Agent 只主动连接 Gateway，可以避免为每台设备配置端口转发、防火墙入站规则和公网证书。设备只需要能够访问 Gateway，特别适合位于 NAT、家庭网络或移动网络后的设备。

### 12.2 为什么同时保留 P2P 和 Relay

单独使用 P2P 可以降低延迟和带宽成本，但会受到 NAT、企业防火墙和 WebRTC ICE 条件影响。单独使用 Relay 虽然简单可靠，但所有数据都经过公网 Gateway。两者结合后，P2P 负责性能，Relay 负责可达性。

### 12.3 为什么不让 P2P 自动重试所有请求

P2P 和 Relay 切换可能发生在请求已经部分发送之后。对带副作用的 POST 或命令执行请求自动重放，可能导致业务重复执行。因此传输层只负责恢复连接和报告失败，是否重试由上层业务决定。

## 13. 阅读和修改建议

理解代码时建议按以下顺序阅读：

1. 先读本文，建立 Gateway/Agent/P2P/Relay 的边界。
2. 再读 [`src/main.py`](../src/main.py) 中 `Gateway`、`Agent` 和启动入口。
3. 阅读 [`src/p2p.py`](../src/p2p.py)，理解分片和资源限制。
4. 阅读 [`src/static_adapter.py`](../src/static_adapter.py)，理解浏览器侧传输选择。
5. 最后对照 [`protocol.md`](./protocol.md) 和可靠性测试，确认修改是否破坏协议和清理语义。

修改跨越 Gateway、Agent 和浏览器适配层时，应同时检查：

- Relay 和 P2P 是否使用同样的业务语义。
- 请求、响应、流和 WebSocket 是否都有大小上限。
- timeout、cancel、close、disconnect 是否释放所有状态。
- 设备切换和重连是否保留每个在途请求、流和终端创建时的设备归属。
- 明确 Server 请求和裸 origin 默认 Server 是否会被页面选择意外改写。
- 文档中的协议字段是否与 `protocol.md` 和测试保持一致。
