# Mesh Reliability Hardening Design

## 背景

代码审查基于 `79b0d5d` 发现了协议数据完整性、连接生命周期、P2P 浏览器适配、安装卸载安全和文档一致性问题。目标是在不改变现有 Gateway/Agent 部署边界的前提下，修复确认成立的问题，并保持以下约束：

- WSL 到 Windows 的 Relay 路径继续可用。
- ehang-box 的局域网 P2P 路径继续可用。
- 不修改 Lucky、Xray、Hysteria、FRP、防火墙或真实 OpenCode。
- 所有批次先在本地验证，全部完成后再部署公网和设备。
- 不向仓库提交真实凭据、token、设备身份或运行时状态。

## 分批策略

采用三批实现，批次之间保持清晰边界，最终统一部署：

1. 协议与后端可靠性。
2. 浏览器适配和 P2P/Relay 行为。
3. 安装、卸载、systemd、配置示例和文档。

每批完成后运行对应的本地验证；不在中间版本重启公网服务。

## 第一批：协议与后端可靠性

### 控制连接发送

将所有 Gateway 到 Agent 的控制消息发送统一经过有界超时。发送失败或超时后关闭失效控制连接，并让上层请求得到明确错误，不允许 WebSocket、SSE、P2P offer 或取消操作永久等待。

### 流状态机

P2P 流的首个带状态帧只负责建立浏览器端 Response，不删除流状态。后续 `stream_chunk` 持续写入 ReadableStream，只有 `stream_end` 或 `stream_error` 才删除 pending/stream 状态并关闭或报错。

Relay 队列达到上限时不再静默删除旧数据。当前流应收到明确的溢出错误并关闭，由浏览器按 SSE 重连规则重新建立；保留有界内存，避免用无限队列替代问题。

### 响应大小和分片

增加统一的响应大小上限，普通 HTTP、Relay 流和 P2P 流都必须受控。超限请求得到可识别的错误，不继续累积内存。

P2P 使用统一的双向分片消息格式，至少包含逻辑消息 ID、序号、分片内容和结束标志。请求体、WebSocket data、普通响应和流响应使用相同的重组规则，并检查重复、乱序、缺片和总大小。Relay 与 P2P 对同一请求保持一致的大小限制。

### 连接和资源生命周期

- 同一个 `device_id` 的新控制连接不能让旧连接继续写入共享状态。
- 控制连接重建时关闭旧 P2P peer，并清理 pending request、stream、bridge 和 answer future。
- Agent 注册遇到 429 使用指数退避和 jitter。
- token 不同步时提供受 enroll token 保护的恢复路径，或让 Agent 重新生成身份并明确记录原因。

## 第二批：浏览器适配

### P2P 初始连接和切换

- P2P 初始失败也进入指数退避重试，而不是整页生命周期永久使用 Relay。
- 重试始终使用当前路由设备 ID，不使用上一次成功连接的设备 ID。
- 设备切换、P2P 失败和页面卸载时关闭旧 peer、DataChannel、流和定时器。

### 浏览器协议覆盖

- 增加 XHR 到同一传输适配层。
- 正确处理 Blob、ArrayBuffer、TypedArray 和 DataView 的 byte offset/length。
- 透传 WebSocket subprotocol 和 binaryType 语义。
- 浏览器发送的大请求和 WebSocket data 使用第一批定义的分片协议。
- SSE 支持断线重连、退避和 `Last-Event-ID`。
- 非安全上下文提供 UUID fallback，使 HTTP Gateway 测试路径不因 `crypto.randomUUID()` 崩溃。

### 状态栏和回退

状态栏继续显示当前实际使用的 P2P/Relay 路径和 RTT。P2P 不可用时只回退对应请求，不影响 Relay；重连期间不能把旧设备的 P2P 状态显示为当前设备状态。

## 第三批：安装、卸载和文档

### 卸载安全

- `uninstall.sh` 必须显式接受 `agent`、`gateway` 或 `all`，缺省和非法值都直接退出。
- 未明确保留数据时，删除行为必须清晰可见；保留数据时原地保留身份和状态，而不是复制到随后删除的临时目录。
- 只清理由本次安装启用的 systemd user linger；不误伤其它服务的 linger 配置。

### 部署脚本和 systemd

- `deploy-agent.sh` 先写配置，再启用服务；配置失败不能留下 crash-loop。
- user service 正确处理 linger 和 `systemctl --user` 可用性。
- `deploy-agent.sh` 与 installer 对 HTTP Gateway 使用同一 `allow_insecure_gateway` 逻辑并显示明文风险。
- root/system 与 user service 使用匹配的 systemd 模板和 target。
- Gateway 端口统一为当前实际使用的 `18080`，Caddy 示例、配置示例和 README 一致。

### 凭据和公开文档

避免把 enroll token 放进必须复制执行的命令行，减少进入 shell history、进程列表和终端回滚的机会。公开文档不包含内网地址、真实域名或运行时身份；协议文档和能力矩阵与实现保持一致。

## 错误处理原则

- 网络失败、消息超限、分片缺失、设备离线和认证失败使用不同的内部错误原因，向浏览器暴露稳定的有限错误类别。
- 可重试错误必须由客户端重试；不可重试错误必须关闭当前流或连接，不能留下半活状态。
- 所有清理路径应幂等，重复 close、cancel、disconnect 不得抛出未处理异常。
- 不通过增加无限队列、无限重试或更长超时掩盖资源问题。

## 验证方案

### 静态和单机验证

- Python 编译检查。
- Shell 语法检查。
- 浏览器适配器 JavaScript 语法检查。
- 认证、注册所有权、注销和文件权限冒烟检查。
- 流状态机消息序列：首帧、多个 chunk、结束、错误、缺片和溢出。
- 双向大帧分片：顺序、乱序、重复、超限和传输失败。
- 控制通道超时、设备切换、P2P 初始失败重试和 SSE `Last-Event-ID`。
- 安装器/卸载器参数校验、保留数据、HTTP Gateway 和 systemd unit 生成检查。

### 端到端验证

本地启动 Gateway、Agent 和假 OpenCode，验证 HTTP、SSE、WebSocket、PTY、Relay、P2P、断线重连和设备切换。所有验证通过后，才部署 VPS、WSL 和 ehang-box，最后从公网手机验证 Relay，并从可达局域网验证 P2P。

## 非目标

- 不引入新的外部依赖，除非现有实现无法完成协议验证。
- 不改变 Gateway 的 Basic Auth、enroll token 和每设备 agent token 的基本身份模型。
- 不强制所有设备升级到同一个 OpenCode 版本；只保证适配器对当前已验证版本正确工作。
- 不在本轮引入多租户、TURN 服务或新的公网网络拓扑。
