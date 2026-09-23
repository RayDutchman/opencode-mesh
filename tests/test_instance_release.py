"""共享源码升级（deploy-release.sh --apply）的多实例行为测试。

真实执行脚本（基于行为断言，不是检查输出字符串），用临时安装目录 +
mock systemctl/python 验证：
- list-unit-files + list-units 联合发现同 scope、同 WorkingDirectory 的
  Agent 默认单元/具名实例及 Gateway（共享源码同样影响它）；
- 升级前记录运行状态，只 stop/restart 升级前运行的单元，原 inactive 保持；
- 其它安装目录的单元一律排除，模板单元（@.service 无实例）不操作；
- 安装失败回滚恢复全部原 active（含部分 stop 失败的安全处理）；
- 目标角色没有任何已安装服务时拒绝，避免静默成功；
- 默认单实例（含 --user scope）兼容。

脚本部署会自替换安装目录内的 scripts/；被测脚本在执行前复制到临时目录之外，
保证多场景用同一份原版本，且不被 --apply 的自替换污染。
"""

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy-release.sh"
REVISION = "rev-new"

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("bash", "tar", "sha256sum", "realpath")),
    reason="需要 bash/tar/sha256sum/realpath 才能真实执行 deploy-release.sh",
)

# 安装目录 .venv/bin/python 的 mock：验证阶段打印版本行，
# pip install 可注入失败（对应“安装失败回滚”场景）。
MOCK_PYTHON = """#!/usr/bin/env bash
if [[ -n "${MOCK_PYTHON_LOG:-}" ]]; then
  printf '%s\\n' "$*" >> "$MOCK_PYTHON_LOG"
fi
if [[ "${1:-}" == "-c" ]]; then
  printf 'Deploying Mesh 0.0.0-mock\\n'
  exit 0
fi
if [[ "${MOCK_PIP_FAIL:-0}" == "1" ]]; then
  printf 'mock: pip install failed\\n' >&2
  exit 1
fi
exit 0
"""

# systemctl 的 mock：按 scope（system/user）维护单元表，记录每次调用的行为，
# 支持注入单个单元的 stop 失败（MOCK_STOP_FAIL），状态在调用间持久化。
MOCK_SYSTEMCTL = r'''#!/usr/bin/env python3
import json
import os
import sys

state_path = os.environ["MOCK_SYSTEMCTL_STATE"]
log_path = os.environ.get("MOCK_SYSTEMCTL_LOG")
scope = "user" if "--user" in sys.argv else "system"
with open(state_path, encoding="utf-8") as fh:
    state = json.load(fh)
units = state.setdefault(scope, {})


def log(line):
    if log_path:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def save():
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, sort_keys=True, indent=2)


args = [a for a in sys.argv[1:] if not a.startswith("-")]
if not args:
    sys.exit(2)
verb = args[0]
rest = args[1:]

if verb == "list-unit-files":
    for name in sorted(units):
        log(f"list-unit-files {name}")
        print(f"{name}\t{units[name].get('state_file', 'disabled')}")
    sys.exit(0)

if verb == "list-units":
    for name in sorted(units):
        active = units[name].get("active", "inactive")
        log(f"list-units {name}")
        print(f"{name}\tloaded\t{active}\t{'running' if active == 'active' else 'dead'}")
    sys.exit(0)

if verb == "show":
    name = rest[0]
    log(f"show {name}")
    if name in units and "wd" in units[name]:
        print(units[name]["wd"])
    sys.exit(0)

if verb == "stop":
    fail = {n for n in os.environ.get("MOCK_STOP_FAIL", "").split(",") if n}
    for name in rest:
        if name in fail:
            log(f"stop-fail {name}")
            print(f"mock: failed to stop {name}", file=sys.stderr)
            save()
            sys.exit(1)
        if name in units:
            units[name]["active"] = "inactive"
        log(f"stop {name}")
    save()
    sys.exit(0)

if verb == "restart":
    for name in rest:
        if name in units:
            units[name]["active"] = "active"
        log(f"restart {name}")
        marker = state_path + ".restart-failed"
        if os.environ.get("MOCK_RESTART_FAIL_ONCE") and not os.path.exists(marker):
            open(marker, "w").close()
            save()
            sys.exit(1)
    save()
    sys.exit(0)

if verb == "is-active":
    quiet = "--quiet" in sys.argv
    name = rest[0]
    log(f"is-active {name}")
    active = name in units and units[name].get("active") == "active"
    if not quiet:
        print("active" if active else "inactive")
    sys.exit(0 if active else 1)

log(f"unknown {verb}")
sys.exit(1)
'''


@pytest.fixture(scope="session")
def deploy_script():
    """被测脚本复制到临时目录之外：各场景安装目录内的 scripts/ 会被部署自替换，
    执行副本保持原样，多场景共用同一版本。"""
    scratch = Path(tempfile.mkdtemp(prefix="ocm-deploy-script-"))
    target = scratch / "deploy-release.sh"
    shutil.copy(DEPLOY_SCRIPT, target)
    yield target
    shutil.rmtree(scratch, ignore_errors=True)


@pytest.fixture()
def mock_env(tmp_path):
    """mock systemctl（PATH 前置）+ 状态文件 + 行为日志。"""
    bin_dir = tmp_path / "mock-bin"
    bin_dir.mkdir()
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(MOCK_SYSTEMCTL, encoding="utf-8")
    systemctl.chmod(0o755)
    state_path = tmp_path / "systemctl-state.json"
    log_path = tmp_path / "systemctl.log"
    return bin_dir, state_path, log_path


def make_install_root(base: Path) -> Path:
    """构造旧版本安装目录：src/scripts/pyproject.toml/.venv/bin/python +
    旧 revision 与旧源码标识。"""
    root = base / "install"
    (root / "src").mkdir(parents=True)
    (root / "src" / "MARKER").write_text("OLD", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "opencode-mesh"\n', encoding="utf-8")
    (root / ".mesh-revision").write_text("rev-old\n", encoding="utf-8")
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python_mock = venv_bin / "python"
    python_mock.write_text(MOCK_PYTHON, encoding="utf-8")
    python_mock.chmod(0o755)
    return root


def make_release_archive(base: Path) -> tuple[Path, str]:
    """构造新版发布归档：新源码标识 + 新版本 pyproject + scripts 自替换标记。"""
    stage = base / "release"
    (stage / "src").mkdir(parents=True)
    (stage / "src" / "MARKER").write_text("NEW", encoding="utf-8")
    (stage / "scripts").mkdir()
    # 部署会自替换 scripts/：归档里的 deploy-release.sh 是“新版本”占位，用于验证自替换行为
    (stage / "scripts" / "deploy-release.sh").write_text("# archive-script-v2\n", encoding="utf-8")
    (stage / "pyproject.toml").write_text(
        '[project]\nname = "opencode-mesh"\nversion = "new"\n', encoding="utf-8"
    )
    archive = base / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(stage / "src", arcname="src")
        tf.add(stage / "scripts", arcname="scripts")
        tf.add(stage / "pyproject.toml", arcname="pyproject.toml")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return archive, digest


def write_state(path: Path, scope: str, units: dict[str, dict]) -> None:
    data = {"system": {}, "user": {}}
    data[scope] = units
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def run_apply(script: Path, root: Path, archive: Path, digest: str, role: str, scope: str,
              mock_bin: Path, state_path: Path, log_path: Path, *,
              env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PATH": f"{mock_bin}{os.pathsep}{os.environ['PATH']}",
        "MOCK_SYSTEMCTL_STATE": str(state_path),
        "MOCK_SYSTEMCTL_LOG": str(log_path),
        "MOCK_PYTHON_LOG": str(log_path.parent / "mock-python.log"),
        "MESH_DEPLOY_HEALTH_SLEEP": "0",
    }
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(script), "--apply", str(archive), digest, str(root), role, scope, REVISION],
        env=env, capture_output=True, text=True, timeout=120,
    )


def read_ops(log_path: Path) -> list[tuple[str, str]]:
    """行为日志 -> [(verb, unit), ...]，stop/restart 等对每个单元一行。"""
    if not log_path.exists():
        return []
    ops = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if parts:
            ops.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return ops


def subjects(ops: list[tuple[str, str]], verb: str) -> set[str]:
    return {unit for v, unit in ops if v == verb}


def read_state(state_path: Path, scope: str) -> dict:
    return json.loads(state_path.read_text(encoding="utf-8"))[scope]


def test_partial_restart_stops_new_processes_before_rollback(tmp_path, mock_env, deploy_script):
    bin_dir, state_path, log_path = mock_env
    root = make_install_root(tmp_path)
    names = ["opencode-mesh-agent.service", "opencode-mesh-agent@second.service"]
    write_state(state_path, "user", {name: {"active": "active", "wd": str(root)} for name in names})
    archive, digest = make_release_archive(tmp_path)
    proc = run_apply(deploy_script, root, archive, digest, "agent", "user", bin_dir,
                     state_path, log_path, env_extra={"MOCK_RESTART_FAIL_ONCE": "1"})
    assert proc.returncode == 1
    ops = read_ops(log_path)
    for name in names:
        assert ops.count(("stop", name)) == 2
    assert all(read_state(state_path, "user")[name]["active"] == "active" for name in names)
    assert (root / "src" / "MARKER").read_text() == "OLD"
    assert (root / ".mesh-revision").read_text() == "rev-old\n"


def test_apply_restarts_running_same_root_agent_instances_and_gateway(
    tmp_path, mock_env, deploy_script,
):
    """两个 active + 一个 inactive + 其它目录 active：只 stop/restart 升级前运行、
    同目录的单元（含 Gateway，共享源码同样受影响）；inactive、其它目录及模板不碰。"""
    bin_dir, state_path, log_path = mock_env
    root = make_install_root(tmp_path)
    write_state(state_path, "system", {
        "opencode-mesh-agent.service": {"state_file": "enabled", "active": "active", "wd": str(root)},
        "opencode-mesh-agent@windows.service": {"state_file": "disabled", "active": "active", "wd": str(root)},
        "opencode-mesh-agent@offline.service": {"state_file": "disabled", "active": "inactive", "wd": str(root)},
        "opencode-mesh-agent@wrongdir.service": {"state_file": "disabled", "active": "active", "wd": "/elsewhere/install"},
        "opencode-mesh-gateway.service": {"state_file": "enabled", "active": "active", "wd": str(root)},
        # 模板单元没有实例：不参与发现，也不操作
        "opencode-mesh-agent@.service": {"state_file": "disabled", "active": "inactive", "wd": str(root)},
    })
    archive, digest = make_release_archive(tmp_path)
    proc = run_apply(deploy_script, root, archive, digest, "agent", "system", bin_dir, state_path, log_path)
    assert proc.returncode == 0, proc.stderr

    expected = {"opencode-mesh-agent.service", "opencode-mesh-agent@windows.service",
                "opencode-mesh-gateway.service"}
    ops = read_ops(log_path)
    assert subjects(ops, "stop") == expected
    assert subjects(ops, "restart") == expected
    # inactive 与其它目录的单元从未被 stop/restart，其它目录也没被探测存活状态
    assert "opencode-mesh-agent@offline.service" not in subjects(ops, "stop") | subjects(ops, "restart")
    assert "opencode-mesh-agent@wrongdir.service" not in subjects(ops, "stop") | subjects(ops, "restart") | subjects(ops, "is-active")
    # 模板被完整排除（连 show/is-active 都不出现）
    assert "opencode-mesh-agent@.service" not in subjects(ops, "show") | subjects(ops, "is-active")
    # 升级前运行状态确实记录了 inactive 单元（保持记录语义，但绝不重启它）
    assert "opencode-mesh-agent@offline.service" in subjects(ops, "is-active")

    # 新代码、新 revision 落盘，scripts/ 被自替换为归档版本；
    # 回滚备份保留旧源码与旧 revision（含旧身份文件不改动）
    assert (root / "src" / "MARKER").read_text(encoding="utf-8") == "NEW"
    assert (root / ".mesh-revision").read_text(encoding="utf-8") == f"{REVISION}\n"
    assert (root / "scripts" / "deploy-release.sh").read_text(encoding="utf-8") == "# archive-script-v2\n"
    backups = list((root / ".mesh-backups").glob("source.*.tar.gz"))
    assert len(backups) == 1
    with tarfile.open(backups[0], "r:gz") as tf:
        assert tf.extractfile("src/MARKER").read().decode() == "OLD"
        assert tf.extractfile(".mesh-revision").read().decode() == "rev-old\n"

    # 最终状态：两个 agent 实例与 gateway 都在运行（已重启），inactive 保持 inactive，
    # 其它目录单元保持 active 且从未被碰
    state = read_state(state_path, "system")
    assert state["opencode-mesh-agent.service"]["active"] == "active"
    assert state["opencode-mesh-agent@windows.service"]["active"] == "active"
    assert state["opencode-mesh-gateway.service"]["active"] == "active"
    assert state["opencode-mesh-agent@offline.service"]["active"] == "inactive"
    assert state["opencode-mesh-agent@wrongdir.service"]["active"] == "active"

    # 归档校验阶段确实执行（mock python 记录 -c 调用）
    py_log = Path(log_path.parent / "mock-python.log")
    assert py_log.exists() and any(line.startswith("-c ") for line in py_log.read_text().splitlines())


def test_apply_pip_failure_rolls_back_source_and_restores_all_originally_active(
    tmp_path, mock_env, deploy_script,
):
    """安装（pip）失败：回滚恢复旧源码与旧 revision，并重启全部原 active。"""
    bin_dir, state_path, log_path = mock_env
    root = make_install_root(tmp_path)
    write_state(state_path, "system", {
        "opencode-mesh-agent.service": {"state_file": "enabled", "active": "active", "wd": str(root)},
        "opencode-mesh-agent@windows.service": {"state_file": "disabled", "active": "active", "wd": str(root)},
        "opencode-mesh-agent@offline.service": {"state_file": "disabled", "active": "inactive", "wd": str(root)},
        "opencode-mesh-agent@wrongdir.service": {"state_file": "disabled", "active": "active", "wd": "/elsewhere/install"},
    })
    archive, digest = make_release_archive(tmp_path)
    proc = run_apply(deploy_script, root, archive, digest, "agent", "system",
                     bin_dir, state_path, log_path, env_extra={"MOCK_PIP_FAIL": "1"})
    assert proc.returncode == 1

    # 行为：源码回滚为旧内容，revision 保留旧值
    assert (root / "src" / "MARKER").read_text(encoding="utf-8") == "OLD"
    assert (root / ".mesh-revision").read_text(encoding="utf-8") == "rev-old\n"

    # 原 active 的两个同目录单元：被 stop 且回滚后被重启恢复；inactive/其它目录不被碰
    ops = read_ops(log_path)
    active_set = {"opencode-mesh-agent.service", "opencode-mesh-agent@windows.service"}
    assert subjects(ops, "stop") == active_set
    assert subjects(ops, "restart") == active_set
    assert "opencode-mesh-agent@offline.service" not in subjects(ops, "stop") | subjects(ops, "restart")
    assert "opencode-mesh-agent@wrongdir.service" not in subjects(ops, "stop") | subjects(ops, "restart")
    state = read_state(state_path, "system")
    assert state["opencode-mesh-agent.service"]["active"] == "active"
    assert state["opencode-mesh-agent@windows.service"]["active"] == "active"
    assert state["opencode-mesh-agent@offline.service"]["active"] == "inactive"

    # 安装步骤与回滚都尝试了 pip（行为证据：部署确实走到安装、回滚确实重装）
    py_log = Path(log_path.parent / "mock-python.log")
    pip_calls = [line for line in py_log.read_text(encoding="utf-8").splitlines() if "pip install" in line]
    assert len(pip_calls) >= 2


def test_apply_partial_stop_failure_restores_every_originally_active(tmp_path, mock_env, deploy_script):
    """部分 stop 失败的安全处理：先停成功的单元保持停止，失败仍在运行的单元，
    回滚按“全部原 active”整体重启，保证运行状态恢复且 inactive 不启动。"""
    bin_dir, state_path, log_path = mock_env
    root = make_install_root(tmp_path)
    write_state(state_path, "system", {
        "opencode-mesh-agent.service": {"state_file": "enabled", "active": "active", "wd": str(root)},
        "opencode-mesh-agent@windows.service": {"state_file": "disabled", "active": "active", "wd": str(root)},
        "opencode-mesh-agent@offline.service": {"state_file": "disabled", "active": "inactive", "wd": str(root)},
    })
    archive, digest = make_release_archive(tmp_path)
    proc = run_apply(deploy_script, root, archive, digest, "agent", "system",
                     bin_dir, state_path, log_path,
                     env_extra={"MOCK_STOP_FAIL": "opencode-mesh-agent@windows.service"})
    assert proc.returncode == 1

    ops = read_ops(log_path)
    # 批量 stop 中 agent.service 已停、windows 的 stop 失败
    assert subjects(ops, "stop") == {"opencode-mesh-agent.service"}
    assert subjects(ops, "stop-fail") == {"opencode-mesh-agent@windows.service"}
    # 回滚把全部原 active 一并重启（含 stop 失败、仍在运行的单元）
    assert subjects(ops, "restart") == {"opencode-mesh-agent.service", "opencode-mesh-agent@windows.service"}
    assert "opencode-mesh-agent@offline.service" not in subjects(ops, "restart")

    # 源码已回滚，运行状态全部恢复
    assert (root / "src" / "MARKER").read_text(encoding="utf-8") == "OLD"
    state = read_state(state_path, "system")
    assert state["opencode-mesh-agent.service"]["active"] == "active"
    assert state["opencode-mesh-agent@windows.service"]["active"] == "active"
    assert state["opencode-mesh-agent@offline.service"]["active"] == "inactive"


def test_apply_default_single_instance_user_scope(tmp_path, mock_env, deploy_script):
    """默认单实例兼容性：仅 opencode-mesh-agent.service，user scope；
    若 --user 未转发给 systemctl，mock 会在空的 system 表上找不到单元而拒绝。"""
    bin_dir, state_path, log_path = mock_env
    root = make_install_root(tmp_path)
    write_state(state_path, "user", {
        "opencode-mesh-agent.service": {"state_file": "enabled", "active": "active", "wd": str(root)},
    })
    archive, digest = make_release_archive(tmp_path)
    proc = run_apply(deploy_script, root, archive, digest, "agent", "user", bin_dir, state_path, log_path)
    assert proc.returncode == 0, proc.stderr

    ops = read_ops(log_path)
    assert subjects(ops, "stop") == {"opencode-mesh-agent.service"}
    assert subjects(ops, "restart") == {"opencode-mesh-agent.service"}
    assert (root / ".mesh-revision").read_text(encoding="utf-8") == f"{REVISION}\n"
    assert (root / "src" / "MARKER").read_text(encoding="utf-8") == "NEW"


@pytest.mark.parametrize("role,units", [
    # 目标 role=gateway，但目录上只有 Agent 服务：拒绝，防止静默成功
    ("gateway", {"opencode-mesh-agent.service": {"state_file": "enabled", "active": "active", "wd": None}}),
    # 目标 role=agent，但只有模板单元（无实例）：同样拒绝
    ("agent", {"opencode-mesh-agent@.service": {"state_file": "disabled", "active": "inactive", "wd": None}}),
])
def test_apply_refuses_when_target_role_has_no_installed_service(
    tmp_path, mock_env, deploy_script, role, units,
):
    bin_dir, state_path, log_path = mock_env
    root = make_install_root(tmp_path)
    for spec in units.values():
        spec["wd"] = str(root)
    write_state(state_path, "system", units)
    archive, digest = make_release_archive(tmp_path)
    proc = run_apply(deploy_script, root, archive, digest, role, "system", bin_dir, state_path, log_path)
    assert proc.returncode == 1
    assert "refusing" in proc.stderr.lower()

    # 没有任何服务操作、任何源码变更，连备份目录都不会创建
    ops = read_ops(log_path)
    assert not [v for v, _ in ops if v in ("stop", "restart", "stop-fail", "is-active")]
    assert (root / "src" / "MARKER").read_text(encoding="utf-8") == "OLD"
    assert (root / ".mesh-revision").read_text(encoding="utf-8") == "rev-old\n"
    assert not (root / ".mesh-backups").exists()
