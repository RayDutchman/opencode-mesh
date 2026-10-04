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
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/install.sh | \
  MESH_VERSION=v0.3.3 bash -s -- agent
```

tag 是不可变快照，可能不包含当前 `main` 的新功能；统一服务托管功能晚于 `v0.3.3` 标签。已有安装不通过重跑安装器升级，使用下文 `upgrade.sh`。

### 非交互安装

自动化可预先导出环境变量，再执行安装器：

```bash
# Gateway：另需设置 MESH_USERNAME、MESH_PASSWORD；MESH_ENROLL_TOKEN 可选。
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/install.sh | bash -s -- gateway

# Agent：需设置 MESH_GATEWAY_URL、MESH_ENROLL_TOKEN、OPENCODE_URL。
# 上游启用认证时，再设置 OPENCODE_USERNAME、OPENCODE_PASSWORD。
curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/install.sh | bash -s -- agent
```

变量须由调用环境导出。`MESH_DEVICE_NAME` 设置注册显示名，`MESH_LISTEN_PORT` 设置 Gateway 端口，`MESH_PUBLIC_URL` 用于安装提示中的公网地址。安装器不会把加入密钥拼进输出的可执行命令。

## 配置与日志

字段示例：[agents.example.json](../config/agents.example.json)、[gateway.example.json](../config/gateway.example.json)。

- `agents.json` 顶层字段是公共默认值，`agents.<实例名>` 内同名字段覆盖默认值。每个实例可独立设置 `gateway_url`、`enroll_token`、`opencode_url` 和 `opencode_basic_auth`。
- `enroll_token` 属于 Gateway，用于加入；`device_id` 和 `agent_token` 自动生成、保存，不手工填写。
- `device_name` 只影响显示名，不是身份。默认实例状态为 `data/agent-state.json`，具名实例为 `data/agent-state-<name>.json`；统一配置不接受 `state_file`。
- `listen_host`、`listen_port` 仅属于 Gateway；Agent 连接 `opencode_url`，不监听该端口。旧 `agent.json` 入口仍兼容。
- `p2p_loopback_candidate` 默认 true，可让同机或宿主机浏览器尝试通过 loopback 建立 P2P。

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

独立具名实例将服务名换为 `opencode-mesh-agent@second.service`。只有修改 unit 文件时才先执行相同范围的 `daemon-reload`，再 restart。

Gateway 可设 `access_log_status_min: 400` 只保留 4xx/5xx 访问日志；`access_log: false` 关闭整个访问日志通道。二者均不关闭启动、异常和 Mesh 代理日志。`log_level` 接受 `critical/error/warning/info/debug/trace`，默认及非法值回退均为 `info`。

## 多实例与统一服务

以下本地脚本命令均在安装目录执行。默认模式下，每个实例一个独立服务：

```bash
bash scripts/install.sh agent second
systemctl --user status opencode-mesh-agent@second.service
```

省略实例名表示 `default`；不要显式传 `agent default`。已有实例不能重复安装或被覆盖，修改配置后重启即可。新增实例不会升级共享代码。

### 新安装：一个服务管理全部实例

```bash
MESH_ALL_INSTANCES=1 bash scripts/install.sh agent
MESH_ALL_INSTANCES=1 bash scripts/install.sh agent second
```

从 GitHub 首次安装也可在管道右侧使用 `MESH_ALL_INSTANCES=1 bash -s -- agent`。统一服务名为 `opencode-mesh-agent.service`，入口使用 `--all-instances`，逐实例创建独立子进程。单个子进程异常退出按自身退避重启；启动时配置表校验失败则整个服务无法启动。

日常新增实例可直接编辑 `config/agents.json`，然后重启：

```bash
systemctl --user restart opencode-mesh-agent.service
```

**没有热加载**，重启会重建全部实例。独立服务模式则仅重启修改的实例。`MESH_INSTALL_ONLY=1` 只写配置和 unit，不启用、不启动；目标服务已运行时会拒绝用此模式添加配置。

### 从旧服务迁移

统一服务与旧独立服务不能同时管理同一身份。**不要用卸载脚本迁移**，它会注销设备并删除配置项。

1. 先升级代码，确保入口支持 `--all-instances`。备份配置、整个 `data/`、默认与所有具名 Agent unit，记录 enabled/active 状态。
2. 若仍使用 `agent.json`，将其有效配置放入 `agents.json` 的 `agents.default`，不带 `state_file`；确认原身份文件与新派生路径一致，不生成新身份。
3. 停止全部旧 Agent。disable 旧 `@` 单元，将其文件移出 systemd 加载目录至备份位置；仅 disable 不移走仍会触发共存检查。
4. 保持工作目录、实例名与身份路径，把默认 unit 的入口改为 `--mode agent --config <绝对路径>/config/agents.json --all-instances`；执行 `daemon-reload`，启用并启动默认 unit。
5. 核对服务状态、子实例集合、设备身份和 Gateway 在线状态。回滚时先停止统一服务，再恢复配置与全部旧 unit，reload 后恢复各自原先的 enabled/active 状态。

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

升级后同时检查服务 active、设备列表在线健康及业务请求。仅 `active` 不代表 Agent 已连接 Gateway。涉及协议演进时先升级 Agent，再升级 Gateway；已打开的网页需刷新以加载新适配器。

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
