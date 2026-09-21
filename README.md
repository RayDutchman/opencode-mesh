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
- `curl | bash` 一键安装 / 卸载脚本。

### 设备与路由模型

- 默认设备（`default_device` 或第一个在线设备）使用 `location.origin` 作为 Server URL；
- 其他在线设备使用 `/_mesh/device/<device_id>` 作为 Server URL，Gateway 去掉前缀后透明转发；
- 设备切换交给 OpenCode 原生多 Server UI，Mesh 不注入自定义设备栏，也不维护独立会话索引。

### 数据路径

LAN / WebRTC host candidate → STUN / 外部候选 → VPS Relay。P2P 直连按当前设备上下文建立，切换设备时自动重建连接。

## 安装

在目标设备上执行（交互式，会询问配置）：

```bash
# Agent（OpenCode 所在设备）
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/install.sh | bash

# Gateway（公网服务器）
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/install.sh | bash -s -- gateway
```

脚本会自动：下载源码、创建 Python 虚拟环境、安装依赖、生成配置、写入并启用 systemd 服务。

### 非交互安装（脚本化 / CI）

通过环境变量预填，跳过提问：

```bash
MESH_GATEWAY_URL=https://网关地址 \
MESH_ENROLL_TOKEN='网关 enroll_token' \
OPENCODE_URL=http://127.0.0.1:40960 \
OPENCODE_USERNAME=opencode \
OPENCODE_PASSWORD='本机 OpenCode 密码' \
bash -c 'curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/install.sh | bash -s -- agent'
```

### 配置说明

- `OPENCODE_URL` 必须按本机实际地址填写（含端口），不固定为 `40960`。
- `OPENCODE_PASSWORD` 可选：本机 OpenCode 未启用认证（未设置 `OPENCODE_SERVER_PASSWORD`）时留空即可，Agent 将以无认证方式访问。
- 运行身份决定 systemd 级别：`root` 安装到 `/opt/opencode-mesh` + 系统级服务；普通用户安装到 `~/.local/share/opencode-mesh` + 用户级服务（自动启用 linger）。

## 卸载

```bash
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/uninstall.sh | bash -s -- agent
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/uninstall.sh | bash -s -- gateway
```

停止并禁用服务、删除 systemd unit 和安装目录；交互时可选备份 `data/`（含设备身份）。

## 平台支持

- Linux（含 WSL、ARM64/RK3588）已实测。
- Windows 原生（非 WSL）暂未适配；如需支持请反馈，工作量大可另行评估。
- iOS 暂不考虑。

## 配置参考

复制 `config/gateway.example.json` 为 `gateway.json`，复制 `config/agent.example.json` 为 `agent.json`，两端使用相同的 `enroll_token`。真实配置和 `data/` 已在 `.gitignore` 中排除。

不要把 OpenCode 账号密码写入仓库；Agent 访问本机 OpenCode 时沿用其本地认证配置。

## 待办

- [ ] 设备注销机制：Agent 卸载后 Gateway 侧仍残留离线设备记录（当前需手动清理 `gateway-state.json`），卸载脚本应主动通知 Gateway 注销。
