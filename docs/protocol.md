# OpenCode Mesh 传输协议

Gateway 与 Agent 使用一条长期 WebSocket 控制连接。浏览器请求可以走 Gateway Relay，也可以在同一设备上下文建立 WebRTC DataChannel P2P；两条路径使用相同的业务消息语义。

## 身份与连接

- 浏览器访问 Gateway 使用 HTTP Basic Auth。
- Agent 注册 `POST /_mesh/register` 使用共享 `enroll_token`，成功后得到设备专属 `agent_token`。
- Agent 控制连接使用 `X-Mesh-Agent-Token`，不把 token 放进 URL query。
- Agent 断线后按有界指数退避重新注册和连接；Gateway 不把失效控制连接显示为在线。

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
- 带副作用的请求不因 P2P/Relay 切换自动重复提交，除非上层明确允许重试。

## 错误与清理

稳定错误 reason 包括请求超限、响应超限、payload 超限、非法编码、帧序列错误、流缓冲溢出和连接失败。可重试错误由浏览器或 Agent 退避重试；不可重试错误关闭当前流或连接。

所有 close、cancel、disconnect 和 timeout 路径必须幂等，不能留下 pending future、流队列、P2P peer、WebSocket bridge 或分片装配状态。

## 验收

当前验收方法和覆盖边界见 [maintenance.md](maintenance.md)，旧能力矩阵仅作历史参考。受影响的变更应验证 HTTP、SSE、WebSocket、PTY、取消、P2P、Relay、设备切换及对应故障恢复，不将计划中的验收项当作已经通过的证据。
