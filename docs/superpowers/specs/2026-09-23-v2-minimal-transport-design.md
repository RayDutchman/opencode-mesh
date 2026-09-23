# V2 最小传输适配设计

> **历史状态声明：** 本文是特定时期的历史记录，不是当前实施指令；当前以 [`../../../README.md`](../../../README.md)、[`../../architecture.md`](../../architecture.md)、[`../../protocol.md`](../../protocol.md) 和 [`../../maintenance.md`](../../maintenance.md) 为准。

用户已授权自主完成设计、实现、验证和部署，无需中途确认。V1 历史基线见 README。

## 目标与边界

保留原生 V2 的多 Server、名称、会话缓存和终端。Mesh 只提供每设备稳定入口、P2P 优先和同设备 Relay 兜底。手机无需 VPN，并以开启 v2rayNG 的真实环境作为最终用户验收条件。

复用 RTCPeerConnection、aiortc、httpx、websockets、FastAPI。优先减少现有适配代码，不增加通用协议框架。HTTP 请求体及响应语义必须透明，不再把旧模型字段或空体转换作为修复。不得重放发送结果未知的 mutation。

## 调查与实现顺序

先复现真实浏览器请求失败，比较原生 frp、Mesh Relay 和 P2P 的请求头、请求体和状态码；以实际 V2 客户端的请求构造方式决定接入点。根因修复优先于大规模替换。

请求目标必须从明确的 Server URL 获取；页面选中 Server 仅用于展示，不得覆盖其他 Server 的请求归属。每个长连接绑定其创建时的设备。跨源原生 Server 透传。

## 验证

以行为测试验证请求头与字节透传、204、取消、SSE、终端二进制和不同 Server 并发。全量 pytest、JavaScript 语法、浏览器实际操作、部署后检查分别记录。无法在当前环境验证的手机网络行为明确交给用户验收，不能声称通过。
