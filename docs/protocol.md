# OpenCode Mesh MVP 传输协议

完整的 OpenCode Web 数据面能力、生命周期和三路径验收要求见：
`docs/opencode-web-capability-matrix.md`。

Mesh 的中继协议只能承载传输控制语义，不能丢弃 OpenCode 原始 HTTP、SSE、WebSocket、PTY、取消和断线信息。

Gateway 与 Agent 之间使用一条长期 WebSocket 控制连接。

控制帧为 JSON：

- `request`: Gateway -> Agent，HTTP 请求，body 暂时使用 base64
- `response`: Agent -> Gateway，HTTP 响应
- `ping` / `pong`: 保活

当前版本已验证部分 HTTP 请求响应链路；完整能力必须按矩阵逐项验收，不能只依据页面可打开或单个 HTTP 请求成功作出结论。
