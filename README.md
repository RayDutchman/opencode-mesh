# OpenCode Mesh

一套程序分为 `gateway` 和 `agent` 两种运行模式：

- Gateway：运行在公网（VPS 或任意有公网地址的机器），负责 HTTP Basic Auth、设备注册、设备列表和 Relay 控制面。
- Agent：运行在 OpenCode 所在设备，自动读取 hostname，主动连接 Gateway，并代理本机 OpenCode。

## 当前状态

已完成并通过真实公网浏览器验证：

- 自动生成并持久化设备 ID；
- 自动读取设备 hostname 作为显示名；
- Gateway HTTP Basic Auth（浏览器原生认证，无自建登录页）；
- Agent 自动注册（`enroll_token` + 设备 `agent_token`）；
- 设备列表和在线状态；
- Gateway↔Agent 单 WebSocket 控制通道；
- HTTP / SSE / WebSocket / PTY 通过隧道转发到 Agent 本地 OpenCode；
- 浏览器到 Agent 的 WebRTC DataChannel 直连，失败时自动回退 Relay；
- 多设备：每台设备注册为 OpenCode 原生 Server，各设备项目/会话/tabs 独立；
- 大响应分片、请求取消、超时、断线清理和控制面写入串行化；
- `scripts/deploy-agent.sh` 一键部署到另一台 Linux/OpenCode 设备。

### 设备与路由模型

- 默认设备（`default_device` 或第一个在线设备）使用 `location.origin` 作为 Server URL；
- 其他在线设备使用 `/_mesh/device/<device_id>` 作为 Server URL，Gateway 去掉前缀后透明转发；
- 设备切换交给 OpenCode 原生多 Server UI，Mesh 不注入自定义设备栏，也不维护独立会话索引。

### 数据路径

LAN / WebRTC host candidate → STUN / 外部候选 → VPS Relay。P2P 直连按当前设备上下文建立，切换设备时自动重建连接。

## 配置

复制 `config/gateway.example.json` 为 `gateway.json`，复制 `config/agent.example.json` 为 `agent.json`，两端使用相同的 `enroll_token`。真实配置和 `data/` 已在 `.gitignore` 中排除。

不要把 OpenCode 账号密码写入仓库；Agent 访问本机 OpenCode 时沿用其本地认证配置。

## 一键部署 Agent

目标设备需要已经安装并运行 OpenCode，且 SSH 可达：

```bash
MESH_GATEWAY_URL=https://网关地址 \
MESH_ENROLL_TOKEN='网关 enroll_token' \
OPENCODE_URL=http://127.0.0.1:40960 \
OPENCODE_USERNAME=opencode \
OPENCODE_PASSWORD='本机 OpenCode 密码' \
scripts/deploy-agent.sh user@设备地址
```

脚本会上传源代码、创建 Python 虚拟环境、安装依赖、写入本地配置，并启用用户级 `opencode-mesh-agent.service`。真实配置不会进入 Git。

## 待办

- [ ] 提供 `curl -fsSL ... | bash` 形式的一键安装入口（不绑定具体域名、端口和 OpenCode 端口，通过环境变量传入，覆盖 Gateway 与 Agent 两种安装）。
