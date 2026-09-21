# OpenCode Mesh

把运行在多台设备上的 [OpenCode](https://opencode.ai) 通过一个公网 Gateway 暴露给浏览器：用 OpenCode 原生 Web UI 访问任意一台设备，数据优先走 WebRTC 直连，打不通时自动回退到 Gateway 中继。

## 架构

```
浏览器 ──HTTPS──> Gateway（公网服务器）
                    │  ① WebRTC 直连（LAN / STUN 打洞，最优）
                    │  ② Relay 中继（WebSocket 控制通道，兜底）
                    ▼
                  Agent（每台 OpenCode 设备）──> 本机 OpenCode
```

- **Gateway**：部署在有公网地址的服务器上，负责浏览器 HTTP Basic Auth、设备注册与路由、以及 Relay 中继。
- **Agent**：部署在每台运行 OpenCode 的设备上，主动连接 Gateway，并代理本机 OpenCode。它只对外连接，不监听任何端口。

## 快速开始

### 第 1 步：部署 Gateway（公网服务器）

在公网服务器上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/install.sh | bash -s -- gateway
```

按提示填写：

- **登录用户名 / 密码**：之后浏览器访问 Gateway 时使用，可自定义。
- **enroll_token**：**留空即自动生成并打印**。

> ⚠️ **enroll_token 是 Gateway 的“加入密钥”，请把它保存下来**。下一步在每台设备上安装 Agent 时都要填这个值。它相当于整个 Mesh 的准入凭据，不要外泄。

安装完成后脚本会直接打印一段**可粘贴到设备执行的 Agent 安装命令**（已包含 Gateway 地址和 enroll_token），照抄即可。若安装时填写了 Gateway 公网地址（或设置 `MESH_PUBLIC_URL`），命令里就是真实地址，否则是占位符。

Gateway 默认只监听 `127.0.0.1:18080`，请用反向代理为它提供 HTTPS（可参考 `deploy/Caddyfile.example`）。**务必使用 HTTPS**：Agent 默认拒绝连接非 `https://` 的 Gateway（内网测试可在 Agent 配置中设置 `allow_insecure_gateway`）。

### 第 2 步：部署 Agent（每台 OpenCode 设备）

在每台运行 OpenCode 的设备上执行，填入上一步的 Gateway 地址和 **enroll_token**：

```bash
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/install.sh | bash -s -- agent
```

按提示填写：

- **Gateway public URL**：Gateway 的公网地址，例如 `https://oc.example.com`。
- **enroll_token**：第 1 步保存下来的值。
- **Local OpenCode URL**：本机 OpenCode 地址，默认 `http://127.0.0.1:4096`，按实际端口修改。
- **OpenCode 用户名 / 密码**：本机 OpenCode 若启用了认证则填写，否则留空。

### 第 3 步：浏览器访问

打开 Gateway 的公网地址，输入第 1 步设置的登录用户名/密码即可。每台已注册的设备会作为 OpenCode 原生 Server 出现在界面里，可分别访问其项目、会话与终端。

## 非交互安装

通过环境变量预填，适合脚本化部署：

```bash
# Gateway（登录账号必填；enroll_token 留空自动生成并打印，请保存）
MESH_USERNAME='你的登录名' \
MESH_PASSWORD='你的登录密码' \
bash -c 'curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/install.sh | bash -s -- gateway'

# Agent（把 <enroll_token> 换成 Gateway 打印出来的值）
MESH_GATEWAY_URL=https://oc.example.com \
MESH_ENROLL_TOKEN='<enroll_token>' \
OPENCODE_URL=http://127.0.0.1:4096 \
OPENCODE_USERNAME=opencode \
OPENCODE_PASSWORD='本机 OpenCode 密码' \
bash -c 'curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/install.sh | bash -s -- agent'
```

## 卸载

```bash
# 只卸载 Agent
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/uninstall.sh | bash -s -- agent

# 只卸载 Gateway
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/uninstall.sh | bash -s -- gateway
```

卸载会停止并删除 systemd 服务与安装目录；Agent 卸载时还会通知 Gateway 注销设备。若想保留设备身份（`data/` 目录），可在提示时选择 `y`，或用 `MESH_KEEP_DATA=y` 跳过交互。

## 安装位置与平台支持

- **root** 安装：`/opt/opencode-mesh` + 系统级 systemd 服务。
- **普通用户** 安装：`~/.local/share/opencode-mesh` + 用户级 systemd 服务（自动启用 linger）。

已实测：Linux（含 WSL、ARM64 / RK3588）。Windows 原生（非 WSL）与 iOS 暂不支持。

## 配置参考

- `gateway.json`：`auth.username` / `auth.password`（浏览器登录）、`enroll_token`（加入密钥）、`default_device`。
- `agent.json`：`gateway_url`、`enroll_token`、`opencode_url`、可选的 `opencode_basic_auth`。

`enroll_token` 属于 Gateway，同一 Gateway 上的所有 Agent 共用同一个值。`device_id` 与 `agent_token` 由系统自动生成/签发，无需手工配置。
