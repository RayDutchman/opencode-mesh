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
  MESH_VERSION=v0.3.3 bash -s -- agent
```

安装完成后会打印实际运行版本。已有安装请使用下文的 `upgrade.sh` 指定目标 tag 或 commit 升级；需要回滚时指定较早的 tag。版本号的唯一来源是 `src/__init__.py`，发布前需同步创建对应的 Git tag。

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

Gateway 默认只监听 `127.0.0.1:18080`，请在 Gateway 所在机器配置 HTTPS 反向代理，将公网入口转发到该地址，并支持 WebSocket 和流式响应。可使用现有 Caddy、Nginx 等入口，无需另装一套。**务必使用 HTTPS**：Agent 默认拒绝连接非 `https://` 的 Gateway（内网测试可在 Agent 配置中设置 `allow_insecure_gateway`）。该反向代理提供公网入口，与同机浏览器的 WebRTC/ICE 直连是不同层。

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
bash scripts/upgrade.sh root@your-vps /root/opencode-mesh gateway system v0.3.3
bash scripts/upgrade.sh user@device /home/user/.local/share/opencode-mesh agent user v0.3.3
# 本机以 Git 工作区运行的 Agent：工作区须干净，且 HEAD 与部署 ref 一致
bash scripts/upgrade.sh local "$PWD" agent user HEAD
```

脚本将指定 Git ref 的 `src/`、`scripts/`、`pyproject.toml` 打包，通过 SHA-256 校验后更新，安装依赖并重启服务；不复制本地配置、密钥或 data。目标机沿用原有 systemd 单元、配置和虚拟环境，需已完成首次安装。

直接运行 `bash ~/opencode-mesh/scripts/upgrade.sh` 会自动发现本机已安装的 systemd 服务与真实安装目录，默认升级到脚本所在源码仓库的 HEAD；只有多个安装目标时才需要选择。确认前显示版本、短提交号和关联服务；输入编号错误可重新选择，已安装同一提交则不重启。请先自行更新源码仓库，脚本不会自动执行 git pull。远程升级保留上面的显式位置参数用法。

安装目录的 `.mesh-revision` 记录完整 commit。旧源码仅临时保存在本次 `.mesh-stage.*` 工作目录，成功或完整回滚后自动清理；恢复失败才保留并打印位置。历史版本产生的 `.mesh-backups/` 不会被本次升级自动删除。安装或服务启动失败时尝试恢复旧源码、依赖和原运行服务（共享虚拟环境的依赖不是完整快照）。`active` 检查不代表 Agent 已连通 Gateway，联网状态仍需从设备列表确认。

只打包**已提交的代码**。正式发布先递增 `src.__version__`、更新 CHANGELOG、测试并创建新的 `vX.Y.Z` tag，再部署该 tag；不要移动旧发布 tag。回滚可指定旧 tag，开发工作区需先切换到相应提交。

## 一个服务托管全部实例（可选，opt-in）

默认行为不变：每个实例一个 systemd 单元。想改成「一个 `opencode-mesh-agent.service` 托管 `config/agents.json` 里的全部实例」，用显式环境变量开启：

```bash
# 省略实例名 = 配置 default 实例（`agent default` 会被拒绝）
MESH_ALL_INSTANCES=1 bash scripts/install.sh agent
MESH_ALL_INSTANCES=1 bash scripts/install.sh agent second
```

该模式下的实际形态：

- `ExecStart` 是 `--all-instances`，父进程枚举实例集合，每个实例仍是独立的 `--instance` 子进程；单个实例崩溃只按自己的退避重启，不影响其他实例。
- **没有热加载。** 实例集合在父进程启动时确定。改完 `config/agents.json` 必须重启整个服务才生效：

```bash
systemctl --user restart opencode-mesh-agent.service
```

- 新增实例的日常做法是**手工编辑 `config/agents.json` 加一条 `agents` 条目，然后重启**，不必重跑 `install.sh`。已有实例条目重跑安装会被拒绝（不允许覆盖），统一模式下重复安装只用于补全新实例。
- 每个实例可单独覆盖 `gateway_url` / `enroll_token` / `opencode_url`；只有与公共值不同的实例才在自己的条目里写覆盖，公共值仍是其余实例的默认。见 [`config/agents.example.json`](config/agents.example.json)。
- `MESH_INSTALL_ONLY=1` 只写配置与单元、不启用不启动；若目标单元当前正在运行会被拒绝，因为改配置不会热加载，注册要到下次重启才发生。

### 与旧安装方式的共存与迁移

统一 unit 与旧的每实例 unit 不能同时管理同一身份，两个方向都有守卫。**迁移不会自动发生，也不该用 `uninstall.sh` 做**——卸载会注销设备、改写配置、删掉最后一个实例的单元，正是要保住的东西。

前置条件：**安装代码里必须已经带 `--all-instances` 入口**（先用 `upgrade.sh` 升级共享代码），否则新单元起不来。

手工迁移步骤：

以下以用户级服务为例；系统级使用 `sudo systemctl` 和 `/etc/systemd/system`。记录各单元原先的 enabled/active 状态，以便回滚时恢复。

1. 备份 `config/agents.json`、整个 `data/`（设备身份），以及默认 `opencode-mesh-agent.service` 和每个旧 `opencode-mesh-agent@*.service` 单元文件。
2. 停止默认 Agent 单元；对每个旧 `@` 单元执行 `systemctl --user stop` 和 `systemctl --user disable`，确认全部停止后再更换入口。
3. 把旧 `@` 单元文件移出 systemd 加载目录（`~/.config/systemd/user`）到备份处，**保留文件本体**；只 disable 不移走的话 installer 仍会判定共存而拒绝。
4. 沿用原 `config/agents.json` 与 `data/agent-state*.json`，把默认实例单元 `ExecStart` 里的 `--instance default` 手工改成 `--all-instances`，`systemctl --user daemon-reload` 后 restart。身份由实例名派生，改这一处不会换设备身份。
5. 验证共享服务 active、子进程实例集合与配置一致、各设备在 Gateway 在线。若需回滚，先停止统一服务，再恢复默认单元和旧 `@` 单元备份，执行 `daemon-reload`，按记录恢复各单元原先的 enabled/active 状态；不能在统一服务仍运行时启动旧实例。

注意区分：**Mesh Agent 与本机 OpenCode 后台是两件独立的事**。Mesh Agent 连 Gateway，不托管也不重启 OpenCode；迁移只影响 Mesh 侧的服务形态，不应动 OpenCode 的 unit、端口或进程。

旧安装方式（每实例一个单元）继续可用，两种模式的配置字段含义一致。

## 卸载

```bash
# 只卸载 Agent
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/uninstall.sh | bash -s -- agent

# 只卸载 Gateway
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/uninstall.sh | bash -s -- gateway

# 同时卸载 Agent 和 Gateway
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/uninstall.sh | bash -s -- all
```

卸载必须显式指定 `agent`、`gateway` 或 `all`。单独卸载 Agent 或 Gateway 只停止并删除对应 systemd 服务，保留共享安装目录；单独卸载 Gateway 也会保留其配置和凭据，避免影响同目录的其他实例。`all` 才进入整目录卸载流程。Agent 卸载时还会通知 Gateway 注销设备。若想在 `all` 时保留设备身份（`data/` 目录），可在提示时选择 `y`，或用 `MESH_KEEP_DATA=y` 跳过交互。

## 安装位置与平台支持

- **root** 安装：`/opt/opencode-mesh` + 系统级 systemd 服务。
- **普通用户** 安装：`~/.local/share/opencode-mesh` + 用户级 systemd 服务（自动启用 linger）。

已实测：Linux（含 WSL、ARM64 / RK3588）。Windows 原生（非 WSL）与 iOS 暂不支持。

## 常用运维命令

普通配置文件修改后，只需重启对应实例；不需要 `daemon-reload`。例如，用户级默认 Agent：

```bash
systemctl --user restart opencode-mesh-agent.service
systemctl --user status opencode-mesh-agent.service
journalctl --user -u opencode-mesh-agent.service -f
```

具名用户级 Agent（示例实例名 `workstation`）使用模板单元：

```bash
systemctl --user restart opencode-mesh-agent@workstation.service
systemctl --user status opencode-mesh-agent@workstation.service
journalctl --user -u opencode-mesh-agent@workstation.service -f
```

系统级 Gateway：

```bash
sudo systemctl restart opencode-mesh-gateway.service
sudo systemctl status opencode-mesh-gateway.service
sudo journalctl -u opencode-mesh-gateway.service -f
```

`config/agents.json` 是共享配置：若只修改一个实例（例如 `agents.default.device_name`），只重启该实例，不必重启同机其他 Agent；统一 unit 下则重启该 unit 即可，它会逐实例重建子进程。`device_name` 仅是注册显示名；重启会重新注册其显示信息，但不会改写程序管理的 `data/agent-state*.json` 身份、`device_id` 或 `agent_token`。只有修改 systemd unit 文件时才先执行对应 scope 的 `daemon-reload`，再重启服务：用户级用 `systemctl --user daemon-reload`，系统级用 `sudo systemctl daemon-reload`。

上游健康状态由 Agent 报告，需要升级 Agent 才能使用；旧 Mesh 未报告健康时显示灰色未知，但仍可手动尝试，浏览器会实际验证目标 `/api/info` 是否为 OpenCode V2 后才进入。健康设备正常连接；Agent 离线或明确报告 OpenCode 不可达、认证失败、异常时不可选。浏览器验收分两步：保留 Agent、停止并恢复 OpenCode，观察上游不可用与恢复；再停止并恢复对应 Mesh Agent，观察 Agent 离线与恢复。每步恢复正常后再进行下一步，设备身份无需重新创建。

## 配置参考

- `agents.json`：同机所有 Agent 的统一人工配置，参见 [`config/agents.example.json`](config/agents.example.json)。顶层配置 Gateway 与加入密钥，`agents` 中按实例名填写各自上游、显示名和可选认证；实例字段覆盖同名公共字段。
- `gateway.json`：`auth.username` / `auth.password`（浏览器登录）、`enroll_token`（加入密钥）、`default_device`，以及可选日志项 `log_level`（默认 `info`）、`access_log`（默认 `true`）、`access_log_status_min`（默认未设置）。

日志项说明：`access_log` 设为 `false` 会关闭全部访问日志，包含错误响应；只想压掉成功请求的噪音时，设 `access_log_status_min` 为 `400`，错误与 5xx 仍会记录，4xx 以下的请求不记录。浏览器设备状态刷新、连接探测和离线页轮询都是 200 响应，是长期运行网关上访问日志的主要来源。启动、关闭和未捕获异常日志（`Exception in ASGI application`）以及 Mesh 自身的代理与 P2P 日志始终保留。修改后重启对应服务生效。`log_level` 只接受 `critical`、`error`、`warning`、`info`、`debug`、`trace`，填写其他值会回退为 `info`。若日志量仍然偏大，可同时限制 journald 保留上限，例如在 `[Journal]` 中设置 `SystemMaxUse=200M`。
- `agent.json`：`gateway_url`、`enroll_token`、`opencode_url`、可选的 `device_name`（注册显示名，省略时使用主机名）、`opencode_basic_auth`、`p2p_loopback_candidate`（默认 true，让同机/宿主机浏览器通过 `127.0.0.1` 建立 P2P 直连）。

`enroll_token` 属于 Gateway，同一 Gateway 上的所有 Agent 共用同一个值。`device_id` 与 `agent_token` 由系统自动生成/签发，无需手工配置。

同机新增实例使用 `bash scripts/install.sh agent second`（或 `MESH_INSTANCE=second`）；设置 `MESH_INSTALL_ONLY=1` 时只写配置与单元，不启用、不启动。`MESH_DEVICE_NAME` 指定显示名。已有实例拒绝重复安装；更改配置后重启该实例即可，升级共享代码使用 `upgrade.sh`。以上是默认的每实例一个单元方式；统一 unit 下新增实例见[「一个服务托管全部实例」](#一个服务托管全部实例可选opt-in)。

加入密钥通过已导出的 `MESH_ENROLL_TOKEN` 提供。服务名称为 `opencode-mesh-agent@second.service`，默认实例仍为 `opencode-mesh-agent.service`。远端首次安装时在目标机器运行 `install.sh`，不另设远程安装脚本。`uninstall.sh agent second` 只移除对应服务和配置项，保留身份与共享目录；统一 unit 下删除一个实例同样是「停 unit → 注销设备 → 改配置 → 恢复原运行态」，共享 unit 保留，删掉最后一个实例才停用并删除单元；`all` 才进入整目录卸载流程。共享升级按同一 systemd scope、实际工作目录收集关联服务，仅恢复升级前运行的集合。

统一配置通过 `--instance` 选择单个实例，或用 `--all-instances` 托管全部（两者互斥，`--all-instances` 仅用于 agent 模式），例如：

```bash
.venv/bin/python -m src.main --mode agent --config config/agents.json --instance second
```

此命令会实际启动并注册 Agent。程序不会监听 OpenCode 的上游端口，而是连接 `opencode_url`。统一配置不接受 `state_file`：默认实例身份自动保存于安装目录的 `data/agent-state.json`，其他实例为 `data/agent-state-<name>.json`。这些文件是程序内部身份存储，不需要手工填写，也不写回人工配置；备份时应连同配置保存。

旧单实例配置仍可使用原命令运行。新增实例前如需调整为统一配置，应备份配置、保留原设备身份，并核对服务工作目录与内部状态路径；安装脚本不会自动覆盖旧配置。改用统一 unit 托管的步骤见上文迁移小节。`config/agents.json` 已被 Git 忽略；示例文件不包含真实凭据。Agent 不使用 `listen_host`、`listen_port`，这两个字段仅属于 Gateway。

`scripts/` 仅保留安装 `install.sh`、卸载 `uninstall.sh`、升级 `upgrade.sh` 三个入口；认证检查统一随 pytest 执行。有控制终端时直接 `bash scripts/upgrade.sh` 自动发现本机安装并确认，`bash scripts/uninstall.sh` 按提示选择卸载角色与实例；自动化仍可使用位置参数。安装具名实例使用 `bash scripts/install.sh agent NAME`，连接参数通过终端提示填写。

## 更多文档

- [`AGENTS.md`](AGENTS.md)：AI 与维护者的工作入口、约束和交接要求。
- [`docs/maintenance.md`](docs/maintenance.md)：事实来源、开发验证、已知限制及持续维护方法。
- [`docs/architecture.md`](docs/architecture.md)：原理、架构、数据流和文件结构说明。
- [`docs/protocol.md`](docs/protocol.md)：控制消息与 P2P 分片协议规格。
- [`docs/opencode-web-capability-matrix.md`](docs/opencode-web-capability-matrix.md)：历史能力矩阵，不代表当前 V2 的完整验收范围。
- [`docs/opencode-web-route-catalog.json`](docs/opencode-web-route-catalog.json)：历史路由目录。
- `docs/superpowers/`：按日期保留的历史设计与执行证据；当前工作以维护入口和现行架构、协议为准。
