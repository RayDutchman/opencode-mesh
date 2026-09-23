# OpenCode Mesh

把运行在多台设备上的 [OpenCode](https://opencode.ai) 通过一个公网 Gateway 暴露给浏览器：用 OpenCode 原生 Web UI 访问任意一台设备，数据优先走 WebRTC 直连，打不通时自动回退到 Gateway 中继。

## 已确认的版本基线

- **V1 稳定基线**：`852668b01a51d38b3f38c827f3ca2e64fe614080`。
- 提交时间：北京时间 **2026-09-22 17:19:16（UTC+8）**；标题：`chore: ignore deployment metadata`。
- 该提交为历史人工验收确认的 V1 基线；`v0.1.0` 标签更早，不能代替这个精确基线。它不代表当前版本对 V1 的兼容承诺。
- 当前维护方向为 V2 原生多 Server、最小传输适配、P2P 优先与 Relay 兜底。

## 架构

```
浏览器 ───── WebRTC 直连 ───────────────────> Agent ──> OpenCode V2
  └── HTTPS/WSS ──> Gateway ── WebSocket 中继 ──┘
```

Gateway 提供页面、设备发现和 WebRTC 信令。直连建立后，消息、事件流和终端数据直接到 Agent；直连不可用时走 Relay。手机只需浏览器，无需安装 VPN 客户端。

- **Gateway**：部署在有公网地址的服务器上，负责浏览器 HTTP Basic Auth、设备注册与路由、以及 Relay 中继。
- **Agent**：部署在每台运行 OpenCode 的设备上，主动连接 Gateway，并代理本机 OpenCode。它只对外连接，不监听任何端口。

更完整的工作原理、组件边界、数据流和文件职责见 [`docs/architecture.md`](docs/architecture.md)。

## 快速开始

### 版本安装

仓库使用 SemVer 版本号，发布 tag 使用 `vX.Y.Z` 格式。安装脚本默认从 `main` 分支获取最新开发版本；生产环境可以固定到发布 tag：

```bash
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/install.sh | \
  MESH_VERSION=v0.2.1 bash -s -- agent
```

安装完成后会打印实际运行版本。升级时修改 `MESH_VERSION` 后重新执行安装；需要回滚时指定较早的 tag。版本号的唯一来源是 `src/__init__.py`，发布前需同步创建对应的 Git tag。

### 第 1 步：部署 Gateway（公网服务器）

在公网服务器上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/install.sh | bash -s -- gateway
```

按提示填写：

- **登录用户名 / 密码**：之后浏览器访问 Gateway 时使用，可自定义。
- **enroll_token**：**留空即自动生成并打印**。

> ⚠️ **enroll_token 是 Gateway 的“加入密钥”，请把它保存下来**。下一步在每台设备上安装 Agent 时都要填这个值。它相当于整个 Mesh 的准入凭据，不要外泄。

安装完成后脚本会打印 Agent 安装步骤和 Gateway 地址。脚本不会把 enroll token 拼进可执行命令；请在 Agent 安装提示中粘贴保存的 token。

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

打开 Gateway 的公网地址，输入第 1 步设置的登录用户名/密码即可。发现的在线 **OpenCode V2** 设备会补充到原生 Server 列表，可分别访问其项目、会话与终端。Server 改名、外部 Server 和会话管理交给 OpenCode；Mesh 不再覆盖已有名称或整张列表。

当前开发线以 **V2.0.6** 为验证版本，不再维护 V1 前端兼容。浏览器页面使用原生 `/server/<encoded-server>/...` 路由；`/_mesh/device/<id>` 是 Server 的请求地址，不是 V2 页面入口。状态栏显示当前设备及 P2P/Relay；后台访问其他 Server 时会使用该 Server 的明确地址。

Gateway 域名只作为访问入口，不再额外显示为业务 Server。Mesh 在原生 UI 初始化前发现设备，集中适配已验证的 V2 入口模块；旧域名别名定向迁移到明确设备，设备发现不再触发迟到刷新。OpenCode 升级后入口契约需要复验。VPS 若也运行 OpenCode，应另启一个独立 Agent 指向本机服务，与其他设备一样注册和访问，且使用独立的身份状态文件。

Mesh 适配 `fetch` 和 WebSocket 传输，并在 V2 SDK 通过 `new URL('/api/...', serverUrl)` 构造请求时保留 Mesh Server 基址，避免绝对路径丢掉设备前缀。其他 URL 沿用原生解析；不替换 XMLHttpRequest/EventSource，也不改写 JSON 请求体。P2P 已发出的请求若中断，结果可能未知，不会自动换到 Relay 重发 mutation；后续请求可走 Relay。

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

## 更新已有部署（推荐）

统一使用 Git tag 或 commit 部署，无需逐个同步 Python 文件：

```bash
bash scripts/deploy-release.sh root@your-vps /root/opencode-mesh gateway system v0.2.1
bash scripts/deploy-release.sh user@device /home/user/.local/share/opencode-mesh agent user v0.2.1
# 本机以 Git 工作区运行的 Agent：工作区须干净，且 HEAD 与部署 ref 一致
bash scripts/deploy-release.sh local "$PWD" agent user HEAD
```

脚本将指定 Git ref 的 `src/`、`scripts/`、`pyproject.toml` 打包，通过 SHA-256 校验后更新，安装依赖并重启服务；不复制本地配置、密钥或 data。目标机沿用原有 systemd 单元、配置和虚拟环境，需已完成首次安装。

安装目录的 `.mesh-revision` 记录完整 commit；`.mesh-backups/` 保留更新前源码。安装或服务启动失败时自动恢复旧源码并尝试重新安装、启动旧版（共享虚拟环境的依赖不是完整快照）。`active` 检查不代表 Agent 已连通 Gateway，联网状态仍需从设备列表确认。

只打包**已提交的代码**。正式发布先递增 `src.__version__`、更新 CHANGELOG、测试并创建新的 `vX.Y.Z` tag，再部署该 tag；不要移动旧发布 tag。回滚可指定旧 tag，开发工作区需先切换到相应提交。

## 卸载

```bash
# 只卸载 Agent
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/uninstall.sh | bash -s -- agent

# 只卸载 Gateway
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/uninstall.sh | bash -s -- gateway

# 同时卸载 Agent 和 Gateway
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/uninstall.sh | bash -s -- all
```

卸载必须显式指定 `agent`、`gateway` 或 `all`。卸载会停止并删除对应 systemd 服务与安装文件；Agent 卸载时还会通知 Gateway 注销设备。若想保留设备身份（`data/` 目录），可在提示时选择 `y`，或用 `MESH_KEEP_DATA=y` 跳过交互。

## 安装位置与平台支持

- **root** 安装：`/opt/opencode-mesh` + 系统级 systemd 服务。
- **普通用户** 安装：`~/.local/share/opencode-mesh` + 用户级 systemd 服务（自动启用 linger）。

已实测：Linux（含 WSL、ARM64 / RK3588）。Windows 原生（非 WSL）与 iOS 暂不支持。

## 配置参考

- `agents.json`：同机所有 Agent 的统一人工配置，参见 [`config/agents.example.json`](config/agents.example.json)。顶层配置 Gateway 与加入密钥，`agents` 中按实例名填写各自上游、显示名和可选认证；实例字段覆盖同名公共字段。
- `gateway.json`：`auth.username` / `auth.password`（浏览器登录）、`enroll_token`（加入密钥）、`default_device`。
- `agent.json`：`gateway_url`、`enroll_token`、`opencode_url`、可选的 `device_name`（注册显示名，省略时使用主机名）、`opencode_basic_auth`、`p2p_loopback_candidate`（默认 true，让同机/宿主机浏览器通过 `127.0.0.1` 建立 P2P 直连）。

`enroll_token` 属于 Gateway，同一 Gateway 上的所有 Agent 共用同一个值。`device_id` 与 `agent_token` 由系统自动生成/签发，无需手工配置。

同机新增实例使用 `bash scripts/install.sh agent second`（或 `MESH_INSTANCE=second`）；设置 `MESH_INSTALL_ONLY=1` 时只写配置与单元，不启用、不启动。`MESH_DEVICE_NAME` 指定显示名。已有实例拒绝重复安装；更改配置后重启该实例即可，升级共享代码使用 `deploy-release.sh`。

加入密钥通过已导出的 `MESH_ENROLL_TOKEN` 提供。服务名称为 `opencode-mesh-agent@second.service`，默认实例仍为 `opencode-mesh-agent.service`。远端 `deploy-agent.sh` 使用 `MESH_INSTANCE=second` 与相同的仅安装开关。`uninstall.sh agent second` 只移除对应服务和配置项，保留身份与共享目录；`all` 才进入整目录卸载流程。共享升级按同一 systemd scope、实际工作目录收集关联服务，仅恢复升级前运行的集合。

统一配置通过 `--instance` 选择实例，例如：

```bash
.venv/bin/python -m src.main --mode agent --config config/agents.json --instance second
```

此命令会实际启动并注册 Agent。程序不会监听 OpenCode 的上游端口，而是连接 `opencode_url`。统一配置不接受 `state_file`：默认实例身份自动保存于安装目录的 `data/agent-state.json`，其他实例为 `data/agent-state-<name>.json`。这些文件是程序内部身份存储，不需要手工填写，也不写回人工配置；备份时应连同配置保存。

旧单实例配置仍可使用原命令运行。迁移到统一配置前先预检，再明确写入：

```bash
.venv/bin/python scripts/migrate-agent-config.py config/agent.local.json config/agents.json --working-directory "$PWD"
.venv/bin/python scripts/migrate-agent-config.py config/agent.local.json config/agents.json --working-directory "$PWD" --apply
```

将源路径替换为实际旧配置路径；工作目录必须是旧服务的 `WorkingDirectory`。迁移保留原文件和设备身份，拒绝覆盖已有目标或冲突身份，不自动切换、启用或重启服务。`config/agents.json` 已被 Git 忽略；示例文件不包含真实凭据。

## 更多文档

- [`AGENTS.md`](AGENTS.md)：AI 与维护者的工作入口、约束和交接要求。
- [`docs/maintenance.md`](docs/maintenance.md)：事实来源、开发验证、已知限制及持续维护方法。
- [`docs/architecture.md`](docs/architecture.md)：原理、架构、数据流和文件结构说明。
- [`docs/protocol.md`](docs/protocol.md)：控制消息与 P2P 分片协议规格。
- [`docs/opencode-web-capability-matrix.md`](docs/opencode-web-capability-matrix.md)：历史能力矩阵，不代表当前 V2 的完整验收范围。
- [`docs/opencode-web-route-catalog.json`](docs/opencode-web-route-catalog.json)：历史路由目录。
- `docs/superpowers/`：按日期保留的历史设计与执行证据；当前工作以维护入口和现行架构、协议为准。
