# OpenCode Mesh 传输协议

Gateway 与 Agent 使用一条长期 WebSocket 控制连接。浏览器请求可以走 Gateway Relay，也可以在同一设备上下文建立 WebRTC DataChannel P2P；两条路径使用相同的业务消息语义。

## 身份与连接

- 浏览器访问 Gateway 使用 HTTP Basic Auth。
- Agent 注册 `POST /_mesh/register` 使用共享 `enroll_token`，成功后得到设备专属 `agent_token`。
- Agent 控制连接使用 `X-Mesh-Agent-Token`，不把 token 放进 URL query。
- Agent 断线后按有界指数退避重新注册和连接；Gateway 不把失效控制连接显示为在线。

## 上游健康报告

Agent 在独立循环中探测本机 OpenCode 的 `/api/info`，完成后间隔 5 秒进行下一次，单次总期限 2 秒，不跟随重定向；连续两次失败才转为失败状态，一次成功恢复 healthy。如本机配置了 Basic Auth，探测使用该认证，但认证信息不进入控制消息或设备公开状态。结果通过 `agent_hello` 和 `pong` 的 `upstream_health`、`upstream_health_age` 上报；状态变化立即上报，15 秒心跳携带最近结果的实际年龄，不会把旧结果重新算作刚探测成功。

`upstream_health` 为运行时状态：`unknown` 表示没有可接受的近期报告，`healthy` 表示探测到兼容的 OpenCode V2，`unreachable` 表示连接或超时失败，`auth_failed` 表示本机上游认证被拒绝，`unhealthy` 表示其余非健康响应。Gateway 只接受 0～30 秒的数值年龄（不含布尔值），以自己的单调时钟换算检查时间；检查时间超过 30 秒或报告格式无效时降为 `unknown`。该状态不持久化，Agent 重连后从 `unknown` 重新开始。

设备控制连接的 `online` 与上游健康分离：`available` 仍仅在 `online` 且 `upstream_health=healthy` 时为真。`online + unknown`（包括旧 Mesh 未报告健康字段或报告过期）不等于上游失败：浏览器可以将其作为灰色候选项手动尝试，并由启动适配器实际请求 `/api/info` 验证兼容的 V2；这不是 V1 兼容承诺。`unreachable`、`auth_failed`、`unhealthy` 和 Agent 离线均不可选。健康探测失败不会关闭 Agent 控制连接，不对 HTTP、SSE、WebSocket 或 P2P 路由进行 fail-fast，也不会触发请求重放。

## 控制消息

控制消息是 JSON 对象，常见类型如下：

| 类型 | 方向 | 作用 |
|---|---|---|
| `request` / `response` | Gateway ↔ Agent | 普通 HTTP 请求和响应，body 使用 base64 |
| `stream_request` / `stream_chunk` | Gateway ↔ Agent | SSE 或其它流式 HTTP 请求、状态首帧和数据帧 |
| `stream_end` / `stream_error` | Agent → Gateway | 正常结束或带稳定 reason 的流错误 |
| `ws_open` / `ws_opened` | Gateway ↔ Agent | 建立上游 WebSocket 和返回协商协议 |
| `ws_data` / `ws_closed` / `ws_error` | Gateway ↔ Agent | WebSocket 文本、二进制数据和关闭事件 |
| `cancel` / `cancelled` | Gateway ↔ Agent | 取消请求或报告取消结果 |
| `ping` / `pong` | 双向 | 控制连接和 P2P RTT 保活 |
| `p2p_offer` / `p2p_answer` | Gateway ↔ Agent | WebRTC 信令 |

普通响应至少包含 `id`、`status`、`headers` 和 base64 `body`。流首帧包含 `status`/`headers`，流数据包含 base64 `body`。连接级错误应包含稳定的 `reason`，不要让客户端依赖异常文本。

`p2p_offer` 使用 Gateway 生成的 `p2p-` 会话 ID。浏览器在 Gateway 等待 answer 时断开，或 answer 等待超时/失败且尚未返回 answer，Gateway 会清理本地 answer 状态，并在原 Agent 控制连接上发送同一 ID 的 `cancel`。Agent 将 `p2p-` 前缀的 `cancel` 仅解释为 P2P 会话取消：取消对应 offer 任务并关闭该会话 peer；它不与普通 HTTP/SSE request ID 共用命名空间。

普通控制 `request` 和 `stream_request` 的 ID 在 Agent 上是有界的执行/重放守卫，而不是结果缓存：相同 ID 仍在执行时忽略重复消息，不能替换已在途操作；完成或取消后仅保留无 body 墓碑，最多 256 条、最长 300 秒。窗口内同 ID 普通请求返回 `409`，流请求返回 `stream_error`，改用 `ws_open` 复用该 ID 返回 `ws_error`；窗口之外不承诺永久去重或 exactly-once。活动 WS bridge 的重复 open 也被忽略，但 WS 关闭本身不生成普通请求墓碑。

滚动部署先更新 Agent，再更新 Gateway；旧 Gateway 没有协商取消通知时，新 Agent 的未连接期限仍可兜底清理。

## P2P 分片信封

P2P 上所有浏览器请求、响应 body、流数据和 WebSocket 控制/数据消息都使用以下信封：

```json
{
  "message_id": "logical-message-id",
  "sequence": 0,
  "data": "base64 payload",
  "final": true
}
```

- `message_id` 用于隔离并行消息，不能复用到另一个逻辑请求。
- `sequence` 从 0 开始严格递增；重复、跳号和缺失被拒绝。P2P 同一通道内的活动请求只允许一个执行任务；完成后不保存结果缓存，也不在有限状态窗口之外承诺 exactly-once 或永久去重。
- `data` 是当前分片的 base64 字节；`final=true` 表示一个逻辑消息结束。
- 同一个流可以复用 `message_id` 发送多个 `stream_chunk` 逻辑消息；每个逻辑消息最后一帧 `final=true`，`stream_end` 单独作为最终消息。
- 控制消息的完整 JSON 放在 `data` 中；带响应元数据的首帧额外携带 `type`、`id`、`status` 或 `headers`。
- 接收端必须限制单消息、总装配和响应大小，并在取消、超时、断线和错误时释放装配状态。Agent 侧每条 DataChannel 最多保留 64 条活动/错误装配状态；完成 ID 仅保留无 payload 的去重墓碑，最多 256 条、最长 300 秒。墓碑达到上限时淘汰最旧项，因此只在该有限窗口内拒绝完成后的重放。
- Agent 在最终分片完成、解析并校验逻辑消息后，会在派发可能缓慢的本机请求前释放分片字节预算；完成墓碑不持有 payload。Agent 的 300 秒 TTL 在后续收到 frame 时检查，取消、断线和错误路径也会释放对应状态。
- 浏览器接收端独立于 Agent：最多保留 128 条入站装配，单条和全部尚未交付 payload 均各受 64 MiB 限制，未完成装配的 deadline 为 120 秒。已完成的 SSE chunk 和 WebSocket 帧立即释放其 payload 与 deadline；持续流保留的零 payload 状态仍计入 128 条并发条目，但不把整个流累计到字节预算或未完成 deadline。
- 浏览器已终止 HTTP/SSE ID 的迟到分片在最多 128 条、120 秒墓碑窗口内被忽略；未知分片不能仅凭不匹配的外层业务 ID 取消其他请求。该窗口不用于活跃的 SSE 或连续 WS 信封。
- 64 MiB 指已解码装配 payload 预算，不是进程峰值内存承诺；DataChannel 已交付的 JSON、base64 字符串和 final 拼接副本仍会产生额外瞬时内存。

默认协议消息/响应限制为 64 MiB，具体预算以配置和实现为准。浏览器 P2P 请求体另有 32 MiB 门限，为 base64 和 JSON 信封留出空间；超限在发送前转 Relay。DataChannel 背压等待有超时，超过后关闭当前 P2P 通道，后续请求可回退 Relay，已发 mutation 不自动重放。

## HTTP、SSE 和 WebSocket

- Gateway 保留 method、path、query、body、OpenCode 业务 headers、状态码和原始响应字节；Gateway Basic Auth/Cookie/Host 等边界凭据不会转发给 Agent。
- SSE 通过 fetch 响应流转发首帧状态、响应头及原始数据顺序，并传播取消。事件解析与业务重连由 V2 SDK 负责；Mesh 不模拟 EventSource，也不自行承诺 `Last-Event-ID` 重连策略。
- 每个 WebSocket bridge 的转发队列默认最多 128 帧、4 MiB，预算包含已入队和正在发送的帧；生产者不等待共享接收循环，溢出产生显式 `ws_error` 并终止桥接，而不静默丢帧。单个 bridge 的发送保持串行；文本和二进制帧均保持原始内容，subprotocol 和关闭码透传。
- P2P 不可用或协商失败时，单个请求可以回退 Relay，不应让旧设备的 P2P 状态污染当前设备。
- Gateway 可用 `p2p_enabled=false` 关闭 P2P：`/_mesh/transport-manifest` 报 `p2p.disabled=true`、`p2p.enabled=false`，浏览器保持 Relay 且不进入重连退避（与 `enabled=false` 的设备离线态不同），`/_mesh/p2p/offer` 返回 409。该字段是 manifest 的一部分，属传输选择契约。
- 带副作用的请求不因 P2P/Relay 切换自动重复提交，除非上层明确允许重试。

## 错误与清理

稳定错误 reason 包括请求超限、响应超限、payload 超限、非法编码、帧序列错误、流缓冲溢出和连接失败。可重试错误由浏览器或 Agent 退避重试；不可重试错误关闭当前流或连接。

所有 close、cancel、disconnect 和 timeout 路径必须幂等，不能留下 pending future、流队列、P2P peer、WebSocket bridge 或分片装配状态。已 answer 的 P2P peer 不因正常闲置被 watchdog 关闭；watchdog 只限制 answer 后尚未打开 DataChannel 的会话。

## 验收矩阵

以下是验收要求，不是已经通过的声明；实际证据与未验证项见 [maintenance.md](maintenance.md)。上游路径、参数及事件格式以被测 OpenCode V2 的 `/openapi.json` 和实际客户端为准，不使用旧版本路由快照推断兼容范围。

### 业务覆盖

| 类别 | 传输 | 核对内容 |
|---|---|---|
| 页面、静态资源与文件 | HTTP / 原始字节 | 设备归属、资源完整性、MIME、下载与缓存响应 |
| 项目、会话、模型和配置 | HTTP | 方法、参数、状态码、错误及结果归属 |
| 权限、问题与任务状态 | HTTP + SSE | 操作后状态收敛，刷新后仍一致 |
| 全局与会话事件 | SSE | 心跳、顺序、空闲、取消和业务重连 |
| 终端 | HTTP + WebSocket | 创建、输入、输出、resize、重连和关闭 |
| 工具、Agent、MCP 等上游能力 | 按上游接口 | 请求透明转发，原生 UI 的结果与错误完整 |

### HTTP 字段与认证边界

逐项比较 method、path、query、请求体字节、`x-opencode-directory`、`x-opencode-workspace`、`Content-Type`、`Accept`、`Range`、`If-None-Match`、状态码、MIME 与响应字节。还应检查 `Content-Length`、`Content-Encoding`、`WWW-Authenticate` 和 `Set-Cookie` 的实际处理，不能把它们一概当作需要原样透传的字段：

- Gateway 的 `Authorization`、Cookie 和代理链路头不得进入设备；上游认证由该实例配置提供。验收只记录认证是否正确，不记录凭据值。
- 重新分帧时剥离旧长度、编码和逐跳头，由 HTTP 库生成与实际字节一致的头；上游 `Set-Cookie` 按隔离策略过滤。
- AbortSignal 应释放对应请求和传输资源。结果未知的已发 `POST`、`PUT`、`PATCH`、`DELETE` 不因切换路径盲目重放。

### SSE

| 场景 | 必须核对的语义 |
|---|---|
| 首帧 | HTTP 状态与响应头先于数据，已发连接事件不丢失 |
| 心跳与空闲 | 保留原始心跳字节，代理不缓冲整个流；不能仅因空闲返回 502 |
| 顺序与重放 | 保留事件顺序及上游提供的 ID；转发客户端实际携带的重放参数或头 |
| 取消 | 关闭对应上游订阅，清理队列、任务和 pending 状态 |
| 断线与 Gateway 重启 | 验证原生 SDK 的恢复和最终状态；不由 Mesh 另造 EventSource 重连规则，不重复提交写请求 |

### WebSocket / PTY

- 文本帧、二进制帧、历史输出、控制帧和实时输出保持内容与顺序，协商的 subprotocol、关闭码及原因保持一致。
- 若被测版本使用 ticket、cursor 等机制，核对一次性 ticket 不被自动重放，游标参数和控制帧不被改写；具体格式以该版本为准。
- 每个 bridge 串行发送；拥塞和队列溢出显式报错并清理，不静默丢帧。

### 停止任务与最终状态

**取消 HTTP 订阅不等于停止 OpenCode 任务。** 验证用户点击“停止”时，应沿原设备检查实际业务 interrupt/abort 请求，而不是仅观察 fetch 抛出 AbortError：

```text
浏览器发出停止操作 → Mesh 接收 → 原设备 Agent 转发
→ OpenCode 停止任务 → 发布最终状态 → Mesh 转发事件
→ 页面显示停止结果 → 刷新后状态仍一致
```

### 网络场景与诊断

至少区分同机/局域网 P2P、跨网络 P2P、Gateway Relay 三种场景；前两者是同一 P2P 机制的不同网络环境，不是第三套协议。Relay 要覆盖 HTTP、SSE、WebSocket 和 PTY。页面、静态资源与信令仍可经过 Gateway，不能以业务 P2P 成功宣称所有流量直连。

切换时记录设备归属、实际传输、失败原因、RTT、建链时间及是否重试。浏览器可通过 `window.__ocmTransport.pc?.getStats()` 核对选中的 candidate pair；证据须脱敏。发生中断时同时核对在途请求没有改投其他设备，未知结果的写请求没有被自动重发。
