# Unified Agent Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 一个 service 管理 `agents.json` 全部连接实例，修改配置并重启后生效。

**Architecture:** 父进程枚举实例并监督现有 `--instance` 子进程，保留通信实现和身份派生规则。每个实例独立重启，实例配置覆盖公共配置。统一服务沿用 `opencode-mesh-agent.service`。

**Tech Stack:** Python 标准库、现有 pytest、Bash、systemd；不新增依赖。

**Spec:** 本文“已确认设计”记录用户在 2026-10-04 确认执行的会话设计。

## 已确认设计

- 本机多个 Agent，由一个 service 托管；不实现 VPS 多 Gateway 托管。
- 每实例可独立定义 `opencode_url`、`gateway_url`、`enroll_token`，公共配置继承继续有效。
- CLI 新增 `--all-instances`，仅用于 agent，不能与 `--instance` 同用；原单实例入口保持兼容。
- 只在父进程启动时确定实例集合；增删实例通过重启应用，不实现热加载。
- 子进程独立退避重启，不因一个实例崩溃重启健康实例。使用 1、2、4 秒递增、上限 30 秒的监督退避；连续运行 60 秒后重置。网络重连仍由现有 Agent 负责。
- SIGTERM/SIGINT 停止监督、终止全部子进程并回收；终止超时 5 秒后 kill，最终 wait。停止期间不重新拉起子进程。
- 保持原配置路径、实例名和身份文件，禁止凭据出现在命令行或监督日志。
- 安装统一模式使用显式 `MESH_ALL_INSTANCES=1`，旧安装方式兼容；防止统一 unit 和旧实例 unit 混跑。迁移由运维单独执行，不自动停止或删除现有服务。
- 每实例 Gateway 覆盖应适用于两种安装方式，不能修改已存在公共值从而影响其他实例。已有实例配置仍拒绝覆盖；日常操作是编辑配置再重启，不是重复安装。

## Global Constraints

- 不新增依赖；优先修改现有文件。
- `src/` 与 `tests/` 注释、docstring 使用英语；`scripts/*.sh` 文案与注释纯英语。
- 示例仅使用虚构设备、文档专用域名；不得输出真实认证配置。
- 不自动 commit、push、发布、同步主仓、迁移或重启运行服务。
- 开发 worktree 与本机运行主仓分离，测试不连接生产网关或 OpenCode。

## Review Focus

1. 子进程创建过程中收到停止信号，不能遗漏新创建的进程。
2. 子进程连续崩溃时退避可中断，健康实例继续运行。
3. 配置为空、类型错误、非法实例名以及共享身份路径必须启动前明确处理。
4. 统一服务与旧实例服务不能因安装脚本旁路而同时管理同一身份。
5. 删除一个实例时不能删除共享服务；删除最后一个实例才停用并删除 unit；保留身份。

---

### Task 1: Supervisor 与安装生命周期

**Files:**
- Modify: `src/main.py`
- Modify: `scripts/install.sh`, `scripts/uninstall.sh`
- Test: `tests/test_agent_instances.py`, `tests/test_instance_install.py`
- Test: 新建 `tests/test_agent_supervisor.py`（真实子进程生命周期）
- Verify: `tests/test_instance_release.py`，统一 unit 名应无需修改升级发现逻辑。

**Interfaces:**
- 消费：`resolve_agent_config(path, cfg, instance)`，保持原身份路径推导。
- 提供：`python -m src.main --mode agent --config PATH --all-instances`。
- 提供：`MESH_ALL_INSTANCES=1 bash scripts/install.sh agent [NAME]`，使用统一 unit。

- [ ] **Step 1: 配置和 CLI 回归先行**

在现有测试中补充：

```python
def test_per_instance_gateway_override_preserves_shared_config(tmp_path):
    from src.main import resolve_agent_config
    cfg = {"gateway_url": "https://mesh-a.example.com", "enroll_token": "token-a",
           "agents": {"default": {}, "beta": {
               "gateway_url": "https://mesh-b.example.com", "enroll_token": "token-b"}}}
    original = json.dumps(cfg)
    path = tmp_path / "config" / "agents.json"
    first = resolve_agent_config(path, cfg, "default")
    second = resolve_agent_config(path, cfg, "beta")
    assert first["enroll_token"] == "token-a"
    assert second["gateway_url"] == "https://mesh-b.example.com"
    assert second["enroll_token"] == "token-b"
    assert first["state_file"] != second["state_file"]
    assert json.dumps(cfg) == original
```

覆盖原行为的测试预期直接通过；新 CLI、监督行为和安装模式测试必须先失败，记录失败原因。拒绝 gateway 模式使用 `--all-instances`，拒绝与 `--instance` 同用。

- [ ] **Step 2: 生命周期 RED**

测试侧使用 `sys.executable -u -c` 真实子进程及临时 marker 文件同步；在测试边界替换进程创建命令，生产代码不加测试专用环境变量、日志或开关。断言全部实例启动、失败实例退避重启且健康实例 PID 不变、停止期间不重启、SIGTERM/SIGINT 后子进程无残留、忽略 SIGTERM 的子进程在超时后被回收。全部等待有截止时间，`finally` 清理测试进程。

```bash
/home/chenweibo/opencode-mesh/.venv/bin/python -m pytest -q tests/test_agent_instances.py tests/test_agent_supervisor.py
```

- [ ] **Step 3: 最小监督实现**

在 `src/main.py` 入口附近增加监督 helper。先验证整个实例映射再创建子进程；每实例使用原 CLI 启动，参数通过 argv 列表传递：

```python
[sys.executable, "-m", "src.main", "--mode", "agent",
 "--config", str(Path(path).resolve()), "--instance", name]
```

不传凭据、不使用 shell。空实例集合明确报配置错误；父进程记录实例名、退出码与重试延时。监督层处理创建失败和退出，各自退避；停止信号统一驱动有界回收。保留单实例 `Agent(cfg).run()` 与既有网络重连实现。

- [ ] **Step 4: 安装/卸载 RED 与实现**

复用 `test_instance_install.py` 的 `make_fakebin`、`base_env`、`agent_env`、`seed_unit`、`run_script`，新增统一安装、仅安装模式、不同 Gateway、混合旧 unit 拒绝、删除中间/最后一个实例行为测试。

```python
assert "--all-instances" in unit.read_text()
assert "--instance " not in unit.read_text()
assert config["agents"]["beta"]["gateway_url"] == "https://mesh-b.example.com"
assert config["gateway_url"] == "https://mesh-a.example.com"
```

安装首次写统一 unit；后续新增实例复用已验证归属与模式的统一 unit。已有实例条目不覆盖。`MESH_INSTALL_ONLY=1` 不启动/重启服务；若服务已在运行，拒绝会被监督进程读入的新配置变更，保持“仅安装无注册副作用”契约。

卸载识别统一 unit 的入口参数并先核 WorkingDirectory。删除一个实例时先停止统一服务以结束该连接，再按原路径注销、原子更新配置；仍有实例则恢复之前的运行状态，最后一个才 disable/remove unit。原本停止的服务保持停止。保留所有身份文件，旧独立 unit 分支继续兼容。

```bash
/home/chenweibo/opencode-mesh/.venv/bin/python -m pytest -q tests/test_instance_install.py tests/test_instance_release.py
bash -n scripts/install.sh scripts/uninstall.sh scripts/upgrade.sh
```

- [ ] **Step 5: 针对实现进行独立审查**

审查上述五项 Review Focus 和脚本原子写入、归属验证、退出清理。修复后复跑覆盖用例，不自动提交。

### Task 2: 当前文档与全量验证

**Files:** `README.md`, `docs/architecture.md`, `docs/maintenance.md`；如存在 `config/agents.example.json` 则同步示例。

- [ ] 写明新统一模式的安装、配置与重启命令；原单实例模式作为兼容入口保留。
- [ ] 示例展示两个实例各自的 Gateway/加入密钥，解释公共值仅作默认。
- [ ] 迁移步骤要求备份原 unit 与配置、停止旧实例、沿用原 config/identity 路径、更换统一入口、验证子进程和设备在线；保留可回滚旧 unit，不要求删除身份或注册表。
- [ ] 不将停止 Mesh 转发进程等同于停止 OpenCode 后台；仅通过被停 Mesh 链路访问的客户端会断连。
- [ ] 执行全量与差异检查，记录真实结果和未验收项：

```bash
/home/chenweibo/opencode-mesh/.venv/bin/python -m pytest -q -p no:cacheprovider -W error::DeprecationWarning -rs --tb=short
git diff --check
```

- [ ] 最终报告区分代码完成、隔离验证、本机迁移三个状态；真实服务迁移须另行确认操作范围。

## 执行记录

- 用户后续调整：不强制执行本计划中的 TDD 红绿步骤及多轮审查流程；直接实现与修正、一次 review、必要测试验证即可。已有有效测试保留，只按实际改动适配，不为流程补齐覆盖或重复运行。功能契约不变。

- 2026-10-04：用户已确认上述功能范围并要求执行；在现有 worktree 实施，沿用已有子会话，不自动提交或迁移服务。
- 基线验证：`432 passed / 1 skipped`（缺少 API-35 `android.jar`）；已有隔离 worktree `feat/mesh-pwa`，基线提交 `efba4a5`。
- Task 1、Task 2 已完成：实现、独立审查后修正和当前文档更新完毕。用户中途简化工作流，未继续执行强制 TDD/多轮审查流程。
- 最终全量验证：487 passed / 1 skipped（缺 API-35 `android.jar`），32.54s；脚本语法及 diff 检查通过。未提交、未推送、未部署、未迁移运行服务。
