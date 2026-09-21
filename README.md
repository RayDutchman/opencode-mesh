# OpenCode Mesh

一套程序分为 `gateway` 和 `agent` 两种运行模式：

- Gateway：运行在公网 VPS，负责账号登录、设备注册、设备列表、设备切换和 Relay 控制面。
- Agent：运行在 PVE、Windows/WSL 等 OpenCode 所在设备，自动读取 hostname，主动连接 Gateway，并代理本机 OpenCode。

## 当前状态

已完成并通过真实公网浏览器验证：

- 自动生成并持久化设备 ID；
- 自动读取设备 hostname 作为显示名；
- Gateway 登录 Cookie；
- Agent 自动注册；
- 设备列表和在线状态；
- 设备切换 Cookie；
- Gateway↔Agent 单 WebSocket 控制通道；
- HTTP 请求通过隧道转发到 Agent 本地 OpenCode。
- 浏览器到 Agent 的 WebRTC DataChannel 直连，失败时自动回退 Relay；
- 大响应分片、SSE、EventSource、WebSocket/PTY 桥接；
- 请求取消、超时、断线清理和控制面写入串行化；
- `scripts/deploy-agent.sh` 一键部署到另一台 Linux/OpenCode 设备。

LAN 优先目前通过 WebRTC host candidate 实现：同网或可达网络会优先选用 Agent 的局域网候选，VPS 仅负责认证和信令；无法直连时使用 Relay。独立的局域网 HTTP 入口不是必需路径，避免额外暴露 Agent 端口。

## 配置

复制 `config/gateway.example.json` 为 `gateway.json`，复制 `config/agent.example.json` 为 `agent.json`，两端使用相同的 `enroll_token`。真实配置和 `data/` 已在 `.gitignore` 中排除。

不要把 OpenCode 账号密码写入仓库；Agent 访问本机 OpenCode 时沿用其本地认证配置。

## 一键部署 Agent

目标设备需要已经安装并运行 OpenCode，且 SSH 可达：

```bash
MESH_GATEWAY_URL=https://oc.252327.xyz:8443 \
MESH_ENROLL_TOKEN='网关 enroll_token' \
OPENCODE_URL=http://127.0.0.1:40960 \
OPENCODE_USERNAME=opencode \
OPENCODE_PASSWORD='本机 OpenCode 密码' \
scripts/deploy-agent.sh user@设备地址
```

脚本会上传源代码、创建 Python 虚拟环境、安装依赖、写入本地配置，并启用用户级 `opencode-mesh-agent.service`。真实配置不会进入 Git。
