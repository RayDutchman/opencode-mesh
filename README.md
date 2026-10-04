# OpenCode Mesh

**一个网址，访问多台设备上的 OpenCode。**

在手机或电脑浏览器里，使用 [OpenCode](https://opencode.ai) 原生 Web 界面，访问家中工作站、开发机和服务器上的项目、会话与终端。设备无需开放公网服务端口，也不需要在访问端安装 VPN。

**原生多 Server · WebRTC 直连优先 · Relay 中继兜底 · 浏览器 / PWA**

[快速开始](#快速开始) · [基本架构](#基本架构) · [部署与运维](docs/deployment.md) · [已知限制](#支持范围与已知限制)

## 它解决什么问题

你已经在几台机器上运行 OpenCode，但不想为每台机器分别配置公网入口。Mesh 用一个公网 Gateway 汇集设备入口，每台设备只需运行一个轻量 Agent。

- **出门继续工作**：从手机访问留在家中或办公室的 OpenCode。
- **多机统一入口**：在同一个网页中选择设备，使用它自己的项目、会话和终端。
- **减少中继流量**：网络允许时，当前设备的业务数据通过 WebRTC 直达 Agent；无法直连时使用 Gateway 中继。
- **同机多实例**：一台机器上的多套 OpenCode 可以分别接入，由一个服务统一管理全部 Agent 实例。

Mesh 提供访问和传输能力；OpenCode 仍负责运行任务、管理项目与会话。它不会把不同机器的文件或会话同步到一起。

## 基本架构

```text
+---------+    HTTPS/WSS    +---------+
| Browser | <------------> | Gateway |
+----+----+                +----+----+
     |                         |
     | WebRTC (P2P)            | WSS (Relay)
     |                         |
     +----------+--------------+
                |
                v
       +-------------------+
       | Agent -> OpenCode |
       +-------------------+
```

每台设备各有 Agent → OpenCode 这一组；Agent 主动向 Gateway 建立 WSS 连接。页面、认证和信令经 Gateway，业务数据优先直连，无法直连则中继。

| 组件 | 放在哪里 | 做什么 |
|---|---|---|
| **Gateway** | 有 HTTPS 入口的公网服务器 | 登录认证、设备发现、连接协商、Relay 中继 |
| **Agent** | 能访问 OpenCode 的 Linux / WSL 主机 | 主动连接 Gateway，代理指定 OpenCode 实例 |
| **浏览器** | 手机或电脑 | 使用原生 OpenCode Web UI，选择设备和传输路径 |

页面和静态资源仍经 Gateway 获取；P2P 指业务传输直连，并不代表所有流量都绕过 Gateway。更多细节见[架构说明](docs/architecture.md)。

## 快速开始

### 准备条件

- **一台公网服务器**：Linux、systemd、域名及 HTTPS 反向代理。下面给出 Caddy 配置示例。
- **至少一台 OpenCode 设备**：已运行 **OpenCode V2**，知道它的访问地址和认证信息；Mesh 不代装或启动 OpenCode。
- Gateway 和 Agent 主机需要 **Python 3.11+**（含 `venv`）、`curl`、`tar`、`flock`。WSL 需启用 systemd。

以下命令安装 `main` 分支的当前代码，交互输入配置并自动创建、启动 systemd 服务。普通用户安装为用户级服务，root 安装为系统级服务；已有安装请使用[升级流程](docs/deployment.md#升级与回滚)，不要重复安装。

### 1. 公网服务器：安装 Gateway

```bash
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/install.sh | bash -s -- gateway
```

安装脚本是**交互式的**：按提示填写浏览器登录用户名、密码和监听端口。端口可自行输入，也可直接回车使用默认 `18080`。`enroll_token` 留空会自动生成，**保存它，供设备加入时使用**。

Gateway 监听本机 `127.0.0.1` 的所选端口，接着配置 HTTPS 入口。若已安装并运行 [Caddy](https://caddyserver.com/docs/install)，将以下站点块加入它的 Caddyfile，把域名换成自己的；**若安装时选了其他端口，下面的 `18080` 也要对应修改**：

```caddyfile
mesh.example.com {
    reverse_proxy 127.0.0.1:18080 {
        flush_interval -1
    }
}
```

域名需指向服务器，并允许公网访问 80/443 端口。使用标准 systemd 安装的 Caddy 时，保存配置后执行：

```bash
sudo systemctl reload caddy
```

Caddy 提供 HTTPS 并代理 WebSocket 和流式响应。使用现有 Nginx 等入口也可以，只需将这些流量转发到 Gateway；无需公开 `18080` 端口。

### 2. OpenCode 设备：安装 Agent

在每台设备上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/install.sh | bash -s -- agent
```

| 安装提示 | 填什么 |
|---|---|
| Gateway public URL | 上一步的 HTTPS 地址，如 `https://mesh.example.com` |
| enroll_token | Gateway 生成的加入密钥 |
| Local OpenCode URL | 设备实际运行的 OpenCode 地址；默认提示为 `http://127.0.0.1:4096`，请核对端口 |
| OpenCode username / password | OpenCode 自身的认证信息；未启用认证则留空 |

Gateway 登录密码、加入密钥和 OpenCode 密码用途不同，请分别填写。Agent 主动向外连接，无需为它配置公网端口转发。

### 3. 打开浏览器，选择设备

访问 `https://mesh.example.com`，使用第 1 步的账号登录。设备上线后，打开其项目或会话；顶部 Mesh 横条会显示当前设备、健康状态及 **P2P / Relay**。

**验证跑通**：设备显示健康 → 能查看该设备的项目和会话 → 能打开终端。显示 Relay 也可以正常使用，不必等到 P2P 才开始工作。

手机可直接使用浏览器，也可通过支持的浏览器菜单安装为 PWA。点击顶部 **OpenCode Mesh** 标题可重新加载页面。

## 支持范围与已知限制

- **服务端**：Linux（包括启用 systemd 的 WSL），已有 ARM64 / RK3588 部署验证。Windows 原生服务安装暂不支持；这不限制从 Windows 浏览器访问。
- **OpenCode**：当前维护 V2 适配。上游前端入口变化可能需要适配更新，不承诺任意未来版本开箱即用。
- **传输**：P2P 可达性受浏览器和网络影响；结果未知的已发写请求不会自动改走 Relay 重发。
- **设备切换**：目前建议先回设备主页再切换；从会话页直接切换仍有返回旧会话的已知问题。
- **手机访问**：保留浏览器 / PWA，不提供独立 Android APK；PWA 不支持离线运行 OpenCode 任务。

完整的已知问题及验证记录见[维护说明](docs/maintenance.md)。

## 下一步

| 想做什么 | 看这里 |
|---|---|
| 更新、回滚、查看日志、卸载 | [部署与运维](docs/deployment.md) |
| 同机接入多套 OpenCode、用一个服务管理实例 | [多实例与统一服务](docs/deployment.md#多实例与统一服务) |
| 查看配置字段 | [Agent 示例](config/agents.example.json) · [Gateway 示例](config/gateway.example.json) |
| 理解组件、设备身份与故障恢复 | [架构说明](docs/architecture.md) |
| 理解 HTTP / SSE / WebSocket 转发与验收要求 | [传输协议](docs/protocol.md) |
| 参与开发 | [维护与验证](docs/maintenance.md) · [贡献约定](AGENTS.md) · [更新记录](CHANGELOG.md) |
