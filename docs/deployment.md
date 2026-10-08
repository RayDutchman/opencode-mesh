# 部署与运维

首次部署请先看 [README 快速开始](../README.md#快速开始)。本文用于已有安装的配置、多实例管理、升级和故障排查。

## 安装位置与服务

| 安装身份 | 默认安装目录 | 服务范围 |
|---|---|---|
| 普通用户 | `~/.local/share/opencode-mesh` | `systemctl --user` |
| root | `/opt/opencode-mesh` | `sudo systemctl`（root 可省略 sudo） |

安装时可用 `MESH_INSTALL_DIR` 指定目录。普通用户安装会尝试启用 linger，让服务在退出登录后继续运行；启用失败时应检查 `loginctl` 权限。Gateway 和 Agent 可以同机部署，但 Gateway 不自动充当 OpenCode 设备：仍须另装 Agent 指向该机 OpenCode。

配置和身份位于安装目录内：

- `config/gateway.json`：Gateway 的登录账号、加入密钥、监听端口等。
- `config/agents.json`：各 Agent 实例的上游地址、显示名及可选认证。
- `data/`：自动生成的设备身份及令牌。**备份时同时保留配置和 data，不要靠删除身份文件排障。**

### 固定版本安装

安装器默认获取 `main`。需固定发布版本时，在首次安装中指定 tag，例如：

```bash
# 替换为所需的 Git tag 或 commit，脚本与源码使用同一版本
export MESH_VERSION='replace-with-tag-or-commit'
curl -fsSL "https://raw.githubusercontent.com/RayDutchman/opencode-mesh/${MESH_VERSION}/scripts/install.sh" | bash -s -- agent
```

tag 是不可变快照，可能不包含当前 `main` 的新功能；`v0.3.3` 仍使用旧的独立服务模式。本文描述当前主分支的统一服务模式，升级旧版本前先看[迁移步骤](#从旧服务迁移)。已有安装不通过重跑安装器升级，使用下文 `upgrade.sh`。

若首次安装因缺少 `python3-venv` 中断，先安装与 Python 版本匹配的 venv 包，再重跑原安装命令。安装器会检查并修复残留虚拟环境的 pip 和项目依赖，而不是只看目录是否存在。Gateway 已有配置及同目录服务单元时，重跑保留原账号、加入密钥和 unit，仅修复运行环境并重启；这不升级已有源码。单元属于其他安装目录或配置缺失时拒绝覆盖。

### 非交互安装

`export` 的意思是：在当前终端设置一个变量，让随后启动的安装脚本也能读取它。下面是两台机器各自执行的完整示例，先替换占位值：

```bash
# 在公网服务器执行
export MESH_USERNAME='your-login-name'
export MESH_PASSWORD='replace-with-your-login-password'
export MESH_LISTEN_PORT='18081'
export MESH_ENROLL_TOKEN='replace-with-your-enrollment-token'
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/install.sh | bash -s -- gateway
```

```bash
# 在 OpenCode 设备执行
export MESH_GATEWAY_URL='https://mesh.example.com'
export MESH_ENROLL_TOKEN='replace-with-the-same-enrollment-token'
export OPENCODE_URL='http://127.0.0.1:4096'
# 上游启用认证时填写；未启用时留空
export OPENCODE_USERNAME=''
export OPENCODE_PASSWORD=''
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/install.sh | bash -s -- agent
```

有控制终端时，脚本仍会交互提问，以上变量作为预填值；无人值守、无控制终端时直接使用这些值。`MESH_DEVICE_NAME` 设置注册显示名，`MESH_PUBLIC_URL` 用于安装提示中的公网地址。Gateway 不提供 `MESH_ENROLL_TOKEN` 时自动生成并打印；示例自选了 `18081`，反向代理的上游端口也应改为 `18081`。

## 配置与日志

字段示例：[agents.example.json](../config/agents.example.json)、[gateway.example.json](../config/gateway.example.json)。

- `agents.json` 顶层字段是公共默认值，`agents.<实例名>` 内同名字段覆盖默认值。每个实例可独立设置 `gateway_url`、`enroll_token`、`opencode_url` 和 `opencode_basic_auth`。
- `enroll_token` 属于 Gateway，用于加入；`device_id` 和 `agent_token` 自动生成、保存，不手工填写。
- `device_name` 只影响显示名，不是身份。默认实例状态为 `data/agent-state.json`，具名实例为 `data/agent-state-<name>.json`；统一配置不接受 `state_file`。
- `listen_host`、`listen_port` 仅属于 Gateway；Agent 连接 `opencode_url`，不监听该端口。旧 `agent.json` 须按下文迁移为 `agents.json`。
- `p2p_loopback_candidate` 默认 true，可让同机或宿主机浏览器尝试通过 loopback 建立 P2P。
- `device_offline_ttl_seconds` 默认 `86400`（1 天）：Gateway 每小时清理一次“离线且最近活动超过该时长”的设备注册，包含只注册过、从未连上的条目；在线设备永不清理，`0` 或负值关闭清理。清理只删除注册与令牌，设备再次上线会用同一 `device_id` 重新注册。
- `p2p_enabled` 默认 true，是 Gateway 侧的 P2P 总开关。设为 false 后下发的 manifest 标记 `p2p.disabled`：浏览器不再协商数据通道，新请求走 Relay，且不会因为“关闭”而进入重连循环；`/_mesh/p2p/offer` 同时被拒绝。用于排查“仅 P2P 出问题”的故障。**改动需重启 Gateway，并刷新页面**：已建立的旧通道在刷新前仍然有效；仍加载旧版适配器的页面（无从识别 `disabled`）会按“设备不可用”退避重试，刷新后即停止。

普通配置修改后重启对应服务，不需要 `daemon-reload`：

```bash
# 普通用户安装的默认 Agent
systemctl --user restart opencode-mesh-agent.service
systemctl --user status opencode-mesh-agent.service
journalctl --user -u opencode-mesh-agent.service -f

# root 安装的 Gateway
sudo systemctl restart opencode-mesh-gateway.service
sudo systemctl status opencode-mesh-gateway.service
sudo journalctl -u opencode-mesh-gateway.service -f
```

所有实例都由 `opencode-mesh-agent.service` 管理，修改任一实例后重启这个服务。只有修改 unit 文件时才先执行相同范围的 `daemon-reload`，再 restart。

Gateway 可设 `access_log_status_min: 400` 只保留 4xx/5xx 访问日志；`access_log: false` 关闭整个访问日志通道。二者均不关闭启动、异常和 Mesh 代理日志。`log_level` 接受 `critical/error/warning/info/debug/trace`，默认及非法值回退均为 `info`。

## 多实例与统一服务

**一个安装目录只有一个 Agent 服务**：`opencode-mesh-agent.service` 管理 `agents.json` 中的全部实例，不需要额外的模式开关。以下命令在安装目录执行，向配置中添加具名实例：

```bash
bash scripts/install.sh agent second
systemctl --user status opencode-mesh-agent.service
```

省略实例名表示 `default`；不要显式传 `agent default`。已有实例不能重复安装或被覆盖，修改配置后重启即可。新增实例不会升级共享代码。

### 实例配置与重启

```bash
.venv/bin/python -m src.main --mode agent --config config/agents.json
```

上面是统一服务的前台运行入口，仅用于手动运行；已由 systemd 托管时不要再启动第二份。它逐实例创建独立子进程，单个子进程异常退出按自身退避重启；启动时配置表校验失败则整个服务无法启动。

[配置示例](../config/agents.example.json) 同时展示两种写法：`default` 继承顶层 `gateway_url` / `enroll_token`，`second` 在实例内填写自己的地址和密钥，覆盖公共值。其他实例仍使用顶层默认值；`opencode_url` 等字段同样允许按实例覆盖。

日常新增实例可直接编辑 `config/agents.json`，然后重启：

```bash
systemctl --user restart opencode-mesh-agent.service
```

**没有热加载**，重启会重建全部实例。`MESH_INSTALL_ONLY=1` 只写配置和 unit，不启用、不启动；目标服务已运行时会拒绝用此模式添加配置。

### 从旧服务迁移

当前版本移除了公开的 `--instance`、`--all-instances` 和 `MESH_ALL_INSTANCES` 选项。已有 unit 若带旧参数，必须在升级时修改；`upgrade.sh` 不会重写 unit。统一服务与旧独立服务不能同时管理同一身份。**不要用卸载脚本迁移**，它会注销设备并删除配置项。

1. 备份配置、整个 `data/`、默认与所有具名 Agent unit，记录旧运行提交和 enabled/active 状态。
2. **先停止全部旧 Agent，再运行 `upgrade.sh` 升级代码**；脚本会保持原本停止的服务停止，避免用旧参数启动新入口失败并触发回滚。Gateway 若与 Agent 共目录，升级仍按其原运行状态处理。
3. 若仍使用 `agent.json`，将有效配置放入 `agents.json` 的 `agents.default`，不带 `state_file`；确认原身份文件与新派生路径一致。已使用顶层默认值/实例覆盖的 `agents.json` 不需要改写。
4. disable 旧 `@` 单元，将其文件移出 systemd 加载目录至备份处。保持工作目录、实例名与身份路径，将默认 unit 的入口设为 `--mode agent --config <绝对路径>/config/agents.json`，删除旧的 `--all-instances` 或 `--instance NAME` 参数；执行 `daemon-reload`，按原 enabled/active 状态恢复统一服务。
5. 核对服务状态、子实例集合、设备身份和 Gateway 在线状态。回滚时先停止统一服务，恢复旧代码版本、配置与全部旧 unit，reload 后恢复各自原先的 enabled/active 状态。

用户级 unit 通常位于 `~/.config/systemd/user`，系统级位于 `/etc/systemd/system`。Mesh Agent 与 OpenCode 后台是独立服务，迁移不需要改动 OpenCode 的端口或进程。

## 升级与回滚

在管理端准备最新源码仓库（需要 Git；远程部署需要 SSH 访问权限）：

```bash
git clone https://github.com/RayDutchman/opencode-mesh.git
cd opencode-mesh

# 目标目录和 scope 必须与实际安装一致
bash scripts/upgrade.sh root@vps.example.com /opt/opencode-mesh gateway system HEAD
bash scripts/upgrade.sh user@device.example.com /home/user/.local/share/opencode-mesh agent user HEAD
```

已有源码仓库先更新到期望提交。`HEAD` 可换成 tag 或完整 commit；回滚同样指定旧 ref。脚本只投递该 ref **已提交**的 `src/`、`scripts/`、`pyproject.toml`，校验 SHA-256，不覆盖配置和身份，也不重写 unit。

本机升级可运行 `bash scripts/upgrade.sh` 自动发现安装，或显式指定：

```bash
bash scripts/upgrade.sh local /home/user/.local/share/opencode-mesh agent user HEAD
```

安装目录本身若是 Git 工作区，必须 clean 且 HEAD 等于部署 ref。升级会按 scope 与实际目录收集相关服务，只恢复升级前正在运行的服务；失败时尝试回滚源码、依赖和运行状态。依赖环境并非完整快照。成功后 `.mesh-revision` 记录部署 commit；失败恢复资料按脚本提示保留。

升级后同时检查服务 active、设备列表在线健康及业务请求。仅 `active` 不代表 Agent 已连接 Gateway。旧服务首次升级到无模式开关版本时，必须先完成上文迁移步骤；之后可照常升级。涉及协议演进时先升级 Agent，再升级 Gateway；已打开的网页需刷新以加载新适配器。

## 卸载

在安装目录按角色执行：

```bash
bash scripts/uninstall.sh agent           # 默认 Agent 实例
bash scripts/uninstall.sh agent second    # 具名 Agent 实例
bash scripts/uninstall.sh gateway         # Gateway 服务
bash scripts/uninstall.sh all             # 全部服务与安装内容
```

单独卸载角色会保留共享安装目录；Gateway 配置和凭据也保留。Agent 卸载会尝试向 Gateway 注销设备，移除对应配置，保留身份。统一服务删除一个实例时先停服务再注销、改配置，按原运行状态恢复；最后一个实例删除后才移除共享 unit。

只有 `all` 进入整目录清理，提示时可选择保留 data；`MESH_KEEP_DATA=y` 可预设该选项。卸载不操作 OpenCode 自身。

## 连接排查

1. **设备离线**：检查 Agent 服务、Gateway HTTPS 地址及加入密钥；确认控制连接能到达 Gateway。
2. **设备在线但 OpenCode 不可用**：核对 `opencode_url` 的实际端口和上游账号；Agent 在线不等于 OpenCode 健康。
3. **只有 Relay**：这通常是 WebRTC/NAT 可达性问题，不等于安装失败；先确认 Relay 下业务正常。
4. **手机安装入口无反应**：Android Chrome 铸造 WebAPK 需要手机能访问 Google 的相关服务；普通浏览器访问不受这个安装步骤限制。

已有设备健康状态由 Agent 上报。旧 Agent 未报告健康时显示未知，可手动选择后由浏览器探测 V2。更深入的故障边界与实测记录见[维护说明](maintenance.md)，传输验收见[协议文档](protocol.md#验收矩阵)。
