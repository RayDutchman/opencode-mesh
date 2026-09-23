"""Shared-source upgrade (upgrade.sh --apply) multi-instance behavior tests.

Runs the script for real (behavior-based assertions, not output string checks),
using a temp install dir + mock systemctl/python:
- list-unit-files + list-units jointly discover the Agent default unit/named
  instances and the Gateway sharing the same scope and WorkingDirectory
  (shared source affects the Gateway too);
- running state is recorded before the upgrade; only units running before the
  upgrade are stopped/restarted, originally inactive ones stay;
- units in other install dirs are always excluded; template units (@.service
  with no instance) are never touched;
- install failure rolls back and restores all originally active units
  (including safe handling of partial stop failure);
- refusing when the target role has no installed services avoids silent success;
- default single instance (including --user scope) is compatible.

Deploy self-replaces scripts/ inside the install dir; the script under test is
copied outside the temp dir before running so all scenarios use the same
original version, unpolluted by --apply's self-replacement.
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
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "upgrade.sh"
REVISION = "rev-new"

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("bash", "tar", "sha256sum", "realpath")),
    reason="需要 bash/tar/sha256sum/realpath 才能真实执行 upgrade.sh",
)

# Mock for the install dir's .venv/bin/python: prints a version line during the verify stage,
# pip install can be made to fail (the "install failure rollback" scenario).
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

# systemctl mock: keeps a unit table per scope (system/user), logs every call's behavior,
# supports injecting a single unit's stop failure (MOCK_STOP_FAIL); state persists between calls.
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
    """The script under test is copied outside the temp dir: the install dir's scripts/ gets self-replaced during deploy, the copy stays pristine and is shared by all scenarios."""
    scratch = Path(tempfile.mkdtemp(prefix="ocm-deploy-script-"))
    target = scratch / "upgrade.sh"
    shutil.copy(DEPLOY_SCRIPT, target)
    yield target
    shutil.rmtree(scratch, ignore_errors=True)


@pytest.fixture()
def mock_env(tmp_path):
    """mock systemctl (prepended to PATH) + state file + behavior log."""
    bin_dir = tmp_path / "mock-bin"
    bin_dir.mkdir()
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(MOCK_SYSTEMCTL, encoding="utf-8")
    systemctl.chmod(0o755)
    state_path = tmp_path / "systemctl-state.json"
    log_path = tmp_path / "systemctl.log"
    return bin_dir, state_path, log_path


def make_install_root(base: Path) -> Path:
    """Build an old-version install dir: src/scripts/pyproject.toml/.venv/bin/python +
old revision and old source markers."""
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
    """Build a new-version release archive: new source marker + new-version pyproject + self-replacement marker for scripts."""
    stage = base / "release"
    (stage / "src").mkdir(parents=True)
    (stage / "src" / "MARKER").write_text("NEW", encoding="utf-8")
    (stage / "scripts").mkdir()
    # upgrade self-replaces scripts/; the archive's script verifies the self-replacement behavior.
    (stage / "scripts" / "upgrade.sh").write_text("# archive-script-v2\n", encoding="utf-8")
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
    """Behavior log -> [(verb, unit), ...], one line per unit for stop/restart etc."""
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
    """Two active + one inactive + active unit in another dir: only stop/restart the same-dir units running before upgrade (Gateway included, shared source affects it too); inactive, other-dir and template units are left alone."""
    bin_dir, state_path, log_path = mock_env
    root = make_install_root(tmp_path)
    write_state(state_path, "system", {
        "opencode-mesh-agent.service": {"state_file": "enabled", "active": "active", "wd": str(root)},
        "opencode-mesh-agent@windows.service": {"state_file": "disabled", "active": "active", "wd": str(root)},
        "opencode-mesh-agent@offline.service": {"state_file": "disabled", "active": "inactive", "wd": str(root)},
        "opencode-mesh-agent@wrongdir.service": {"state_file": "disabled", "active": "active", "wd": "/elsewhere/install"},
        "opencode-mesh-gateway.service": {"state_file": "enabled", "active": "active", "wd": str(root)},
        # template unit has no instance: not discovered, never touched
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
    # inactive and other-dir units were never stopped/restarted; other dirs were not even probed for liveness
    assert "opencode-mesh-agent@offline.service" not in subjects(ops, "stop") | subjects(ops, "restart")
    assert "opencode-mesh-agent@wrongdir.service" not in subjects(ops, "stop") | subjects(ops, "restart") | subjects(ops, "is-active")
    # template fully excluded (not even in show/is-active)
    assert "opencode-mesh-agent@.service" not in subjects(ops, "show") | subjects(ops, "is-active")
    # the pre-upgrade running state did record the inactive unit (keeps recording semantics, but never restarts it)
    assert "opencode-mesh-agent@offline.service" in subjects(ops, "is-active")

    # new code and new revision land on disk, scripts/ self-replaced with the archive version;
    # rollback backup keeps old source and old revision (old identity files untouched)
    assert (root / "src" / "MARKER").read_text(encoding="utf-8") == "NEW"
    assert (root / ".mesh-revision").read_text(encoding="utf-8") == f"{REVISION}\n"
    assert (root / "scripts" / "upgrade.sh").read_text(encoding="utf-8") == "# archive-script-v2\n"
    assert not (root / ".mesh-backups").exists()
    assert not list(root.glob('.mesh-stage.*'))

    # final state: both agent instances and the gateway are running (restarted), inactive stays inactive,
    # other-dir units stay active and were never touched
    state = read_state(state_path, "system")
    assert state["opencode-mesh-agent.service"]["active"] == "active"
    assert state["opencode-mesh-agent@windows.service"]["active"] == "active"
    assert state["opencode-mesh-gateway.service"]["active"] == "active"
    assert state["opencode-mesh-agent@offline.service"]["active"] == "inactive"
    assert state["opencode-mesh-agent@wrongdir.service"]["active"] == "active"

    # the archive verify stage really ran (mock python records the -c call)
    py_log = Path(log_path.parent / "mock-python.log")
    assert py_log.exists() and any(line.startswith("-c ") for line in py_log.read_text().splitlines())


def test_apply_pip_failure_rolls_back_source_and_restores_all_originally_active(
    tmp_path, mock_env, deploy_script,
):
    """Install (pip) failure: rollback restores old source and old revision and restarts all originally active units."""
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

    # behavior: source rolled back to old content, revision keeps the old value
    assert (root / "src" / "MARKER").read_text(encoding="utf-8") == "OLD"
    assert (root / ".mesh-revision").read_text(encoding="utf-8") == "rev-old\n"

    # the two originally active same-dir units: stopped, and restarted after rollback; inactive/other-dir untouched
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

    # both the install step and the rollback tried pip (behavior evidence: deploy reached install, rollback reinstalled)
    py_log = Path(log_path.parent / "mock-python.log")
    pip_calls = [line for line in py_log.read_text(encoding="utf-8").splitlines() if "pip install" in line]
    assert len(pip_calls) >= 2
    recovery = list(root.glob('.mesh-stage.*/rollback.tar.gz'))
    assert len(recovery) == 1
    assert 'Recovery incomplete' in proc.stderr


def test_apply_partial_stop_failure_restores_every_originally_active(tmp_path, mock_env, deploy_script):
    """Partial stop failure safe handling: units stopped successfully stay stopped, the still-running failed unit and all originally active units are restarted as a whole on rollback, restoring running state without starting inactive ones."""
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
    # in the batch stop, agent.service was stopped, windows' stop failed
    assert subjects(ops, "stop") == {"opencode-mesh-agent.service"}
    assert subjects(ops, "stop-fail") == {"opencode-mesh-agent@windows.service"}
    # rollback restarts all originally active units together (including the one whose stop failed and is still running)
    assert subjects(ops, "restart") == {"opencode-mesh-agent.service", "opencode-mesh-agent@windows.service"}
    assert "opencode-mesh-agent@offline.service" not in subjects(ops, "restart")

    # source rolled back, running state fully restored
    assert (root / "src" / "MARKER").read_text(encoding="utf-8") == "OLD"
    state = read_state(state_path, "system")
    assert state["opencode-mesh-agent.service"]["active"] == "active"
    assert state["opencode-mesh-agent@windows.service"]["active"] == "active"
    assert state["opencode-mesh-agent@offline.service"]["active"] == "inactive"


def test_apply_default_single_instance_user_scope(tmp_path, mock_env, deploy_script):
    """Default single-instance compatibility: only opencode-mesh-agent.service, user scope;
if --user is not forwarded to systemctl, the mock finds no unit in the empty system table and refuses."""
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


def test_same_revision_does_not_restart(tmp_path, mock_env, deploy_script):
    bin_dir, state_path, log_path = mock_env
    root = make_install_root(tmp_path)
    (root / '.mesh-revision').write_text(REVISION + '\n')
    write_state(state_path, 'user', {'opencode-mesh-agent.service': {
        'state_file': 'enabled', 'active': 'active', 'wd': str(root)}})
    archive, digest = make_release_archive(tmp_path)
    result = run_apply(deploy_script, root, archive, digest, 'agent', 'user', bin_dir, state_path, log_path)
    assert result.returncode == 0
    assert not subjects(read_ops(log_path), 'restart')
    assert (root / 'src/MARKER').read_text() == 'OLD'


@pytest.mark.parametrize("role,units", [
    # target role=gateway but the dir only has Agent services: refuse to avoid silent success
    ("gateway", {"opencode-mesh-agent.service": {"state_file": "enabled", "active": "active", "wd": None}}),
    # target role=agent but only template units (no instance): also refused
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

    # no service operation, no source change, not even a backup dir is created
    ops = read_ops(log_path)
    assert not [v for v, _ in ops if v in ("stop", "restart", "stop-fail", "is-active")]
    assert (root / "src" / "MARKER").read_text(encoding="utf-8") == "OLD"
    assert (root / ".mesh-revision").read_text(encoding="utf-8") == "rev-old\n"
    assert not (root / ".mesh-backups").exists()
