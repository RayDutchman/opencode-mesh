"""install.sh / uninstall.sh multi-instance behavior tests.

Contract (docs/superpowers/specs/2026-09-23-multi-agent-design.md, confirmed with the user):
- All agents share the manual config/agents.json: top-level common fields + per-instance agents mapping.
- Config does not write state_file; identity is derived by the program from the install root and instance name (default agent-state.json, named agent-state-<name>.json).
- Every new instance uses opencode-mesh-agent.service; legacy units are only recognized for cleanup.
- Install only merges the given instance keys, keeping other instances and unknown top-level fields; existing shared code is not overwritten; MESH_INSTALL_ONLY=1 does not enable/start.
- Legacy single-agent configs (agent.json / agent.local.json / agent-*.json) are explicitly rejected, never silently overwritten.
- Single-instance uninstall removes only that instance's config keys and standalone unit, keeping shared directories, other instances and auto identity state.
- Everything is verified through temp directories and mock commands, no real deployment.

Run: .venv/bin/python -m pytest tests/test_instance_install.py -q
"""

import json
import os
import fcntl
import hashlib
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
REAL_PY = sys.executable

FAKE_ID = """#!/usr/bin/env bash
if [ "${1:-}" = "-u" ]; then echo 1000; exit 0; fi
if [ "${1:-}" = "-un" ]; then echo testuser; exit 0; fi
exec /usr/bin/id "$@"
"""

# systemctl and curl share one ordered event log so a test can assert that a
# service was stopped before its device was deregistered. FAKE_ACTIVE_UNITS lists
# the units reported active (default: none, because MESH_INSTALL_ONLY never starts
# anything); FAKE_STOP_FAIL_UNITS makes `stop` fail for those units.
FAKE_SYSTEMCTL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_SYSTEMCTL_LOG:?}"
[[ -z "${FAKE_EVENTS:-}" ]] || printf 'systemctl %s\\n' "$*" >> "$FAKE_EVENTS"
in_list() {
  local needle="$1" item found=0
  shift
  for item in "$@"; do
    [[ "$item" == "$needle" ]] && found=1
  done
  [[ "$found" == 1 ]]
}
# The scripts call `systemctl --user <verb>` through an unquoted SC_CMD, so the verb is
# not necessarily the first argument.
verb=""
for arg in "$@"; do
  case "$arg" in is-active|stop|disable|restart|start|enable) verb="$arg"; break ;; esac
done
if [[ "$verb" == "is-active" ]]; then
  IFS=',' read -r -a _list <<< "${FAKE_ACTIVE_UNITS:-}"
  if in_list "${*: -1}" "${_list[@]}"; then printf 'active\\n'; exit 0; fi
  printf 'inactive\\n'; exit 3
fi
if [[ "$verb" == "stop" ]]; then
  IFS=',' read -r -a _list <<< "${FAKE_STOP_FAIL_UNITS:-}"
  if in_list "${*: -1}" "${_list[@]}"; then printf 'Job failed\\n' >&2; exit 1; fi
fi
exit 0
"""

FAKE_LOGINCTL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_LOGINCTL_LOG:?}"
exit 0
"""

# The real Gateway refuses to deregister a device whose Agent websocket is still
# open (409 in src/main.py). Reproduce it: an agent counts as connected while its
# unit reports active and no stop has been issued yet.
FAKE_CURL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_CURL_LOG:?}"
[[ -z "${FAKE_EVENTS:-}" ]] || printf 'curl %s\\n' "$*" >> "$FAKE_EVENTS"
if [[ "$*" == *"/_mesh/deregister/"* ]]; then
  stopped=0
  if [[ -n "${FAKE_EVENTS:-}" && -f "$FAKE_EVENTS" ]]; then
    while read -r line; do
      case "$line" in *" stop opencode-mesh-agent"*) stopped=1 ;; esac
    done < "$FAKE_EVENTS"
  fi
  connected=0
  IFS=',' read -r -a _units <<< "${FAKE_ACTIVE_UNITS:-}"
  for _unit in "${_units[@]}"; do
    case "$_unit" in
      opencode-mesh-agent.service|opencode-mesh-agent@*.service) connected=1 ;;
    esac
  done
  if [[ "$stopped" != 1 && "$connected" == 1 ]]; then
    [[ -z "${FAKE_EVENTS:-}" ]] || printf 'deregister-conflict %s\\n' "$*" >> "$FAKE_EVENTS"
    exit 22
  fi
fi
exit 0
"""

# The lifecycle scripts inspect /tmp before creating their fixed hash lock.
# Keep that check deterministic without changing the host /tmp permissions.
FAKE_STAT = """#!/usr/bin/env bash
if [[ "${1:-}" == -c ]]; then
  format=${2:-}
  path=${3:-}
  if [[ "$format" == %u && "${FAKE_REAL_INSTALL_OWNER:-}" == 1 && "$path" != /tmp ]]; then
    exec /usr/bin/stat "$@"
  fi
  case "$format:$path" in
    %a:/tmp) printf '%s\\n' "${FAKE_TMP_MODE:-1777}"; exit 0 ;;
    %u:/tmp) printf '0\\n'; exit 0 ;;
    %u:%a:/tmp) printf '0:%s\\n' "${FAKE_TMP_MODE:-1777}"; exit 0 ;;
    %u:%a:*holder) printf '%s\\n' "${FAKE_HOLDER_STAT:-1000:600}"; exit 0 ;;
    %u:%a:*) printf '%s\\n' "${FAKE_LOCK_STAT:-1000:700}"; exit 0 ;;
    %u:*) printf '1000\\n'; exit 0 ;;
  esac
fi
exec /usr/bin/stat "$@"
"""

# Temp files hold the remote script/data, executed in place by the mock ssh (simulating the remote filesystem).
FAKE_SSH = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "${FAKE_SSH_LOG:?}"
while [[ "${1:-}" == -o ]]; do shift 2; done
shift
exec bash -c "$*"
"""

# Python stub inside the venv: logs pip calls, answers version queries, exits success otherwise.
STUB_PY = f"""#!{REAL_PY}
import os, sys
if sys.argv[1:3] == ["-m", "pip"]:
    log = os.environ.get("FAKE_PIP_LOG", "")
    if log:
        with open(log, "a") as f:
            f.write(" ".join(sys.argv[1:]) + "\\n")
    sys.exit(0)
if sys.argv[1:2] == ["-c"] and "src.__version__" in sys.argv[1]:
    print("0.2.1")
    sys.exit(0)
sys.exit(0)
"""

# python3 on PATH: intercepts venv/pip/version queries, passes everything else through to the real interpreter (including heredoc JSON generation).
FAKE_PYTHON = f"""#!{REAL_PY}
import os, sys

def main():
    args = sys.argv[1:]
    if args[:2] == ["-m", "venv"]:
        vd = args[2]
        bindir = os.path.join(vd, "bin")
        os.makedirs(bindir, exist_ok=True)
        stub = os.path.join(bindir, "python")
        with open(stub, "w") as f:
            f.write(STUB_TEMPLATE)
        os.chmod(stub, 0o755)
        with open(os.path.join(vd, "pyvenv.cfg"), "w") as f:
            f.write("[venv]\\n")
        return 0
    if args[:2] == ["-m", "pip"]:
        log = os.environ.get("FAKE_PIP_LOG", "")
        if log:
            with open(log, "a") as f:
                f.write(" ".join(args) + "\\n")
        return 0
    if args[:1] == ["-c"] and "src.__version__" in args[0]:
        print("0.2.1")
        return 0
    os.execv(sys.executable, [sys.executable] + args)

STUB_TEMPLATE = {STUB_PY!r}
main()
"""


def make_fakebin(tmp_path):
    """Build the fake command directory, returning (fakebin, logs dict)."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    logs = {
        "systemctl": tmp_path / "systemctl.log",
        "loginctl": tmp_path / "loginctl.log",
        "curl": tmp_path / "curl.log",
        "ssh": tmp_path / "ssh.log",
        "pip": tmp_path / "pip.log",
        "events": tmp_path / "events.log",
    }
    tools = {
        "id": FAKE_ID,
        "systemctl": FAKE_SYSTEMCTL,
        "loginctl": FAKE_LOGINCTL,
        "curl": FAKE_CURL,
        "stat": FAKE_STAT,
        "ssh": FAKE_SSH,
        "python3": FAKE_PYTHON,
    }
    for name, content in tools.items():
        target = fakebin / name
        target.write_text(content)
        target.chmod(0o755)
    return fakebin, logs


def run_script(name, args, env, cwd=None):
    return subprocess.run(
        [str(SCRIPTS / name), *args],
        env=env,
        cwd=str(cwd) if cwd else str(REPO),
        capture_output=True,
        text=True,
        timeout=120,
    )


def base_env(tmp_path, fakebin, logs, **extra):
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["FAKE_SYSTEMCTL_LOG"] = str(logs["systemctl"])
    env["FAKE_LOGINCTL_LOG"] = str(logs["loginctl"])
    env["FAKE_CURL_LOG"] = str(logs["curl"])
    env["FAKE_SSH_LOG"] = str(logs["ssh"])
    env["FAKE_PIP_LOG"] = str(logs["pip"])
    env["FAKE_EVENTS"] = str(logs["events"])
    env["HOME"] = str(tmp_path / "home")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    env.update(extra)
    return env


def make_source(tmp_path):
    """Build a fake source repo used for MESH_SOURCE_DIR / tar packaging."""
    src = tmp_path / "mesh-src"
    (src / "src").mkdir(parents=True)
    (src / "src" / "main.py").write_text("placeholder\n")
    (src / "scripts").mkdir()
    (src / "scripts" / "install.sh").write_text("#!/usr/bin/env bash\n")
    (src / "pyproject.toml").write_text("[project]\n")
    (src / "config").mkdir()
    (src / "config" / ".keep").write_text("")
    (src / "deploy").mkdir()
    return src


def seed_shared_code(install_dir):
    """Seed shared code in an existing install dir (install/deploy must not overwrite it)."""
    (install_dir / "src").mkdir(parents=True, exist_ok=True)
    (install_dir / "src" / "SENTINEL").write_text("keep-me\n")
    (install_dir / "scripts").mkdir(exist_ok=True)
    (install_dir / "scripts" / "install.sh").write_text("#!/usr/bin/env bash\n")
    (install_dir / "pyproject.toml").write_text("[project]\n")
    venv_bin = install_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    stub = venv_bin / "python"
    stub.write_text(STUB_PY)
    stub.chmod(0o755)
    (install_dir / "deploy").mkdir(exist_ok=True)


def read_json(path):
    return json.loads(Path(path).read_text())


def unit_dir(tmp_path):
    return tmp_path / "xdg" / "systemd" / "user"


def seed_unit(tmp_path, name, install_dir):
    d = unit_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.service").write_text(
        f"[Unit]\n[Service]\nWorkingDirectory={install_dir}\n"
        f"ExecStart={install_dir}/.venv/bin/python -m src.main\n"
    )


def agent_env(**extra):
    return {
        "MESH_GATEWAY_URL": "https://mesh.example.com",
        "MESH_ENROLL_TOKEN": "enroll-token",
        "OPENCODE_URL": "http://127.0.0.1:4096",
        **extra,
    }


def lifecycle_lock_dir(install_dir):
    key = hashlib.sha256(str(install_dir.resolve()).encode()).hexdigest()
    return Path(f"/tmp/opencode-mesh-{key}.lock")


# ---------------------------------------------------------------- install.sh


def test_install_named_keeps_shared_code_and_merges_config(tmp_path):
    """Named install: keep shared source/venv, only update agents[beta], preserve other instances and unknown top-level fields, install only without enabling."""
    fakebin, logs = make_fakebin(tmp_path)
    source = make_source(tmp_path)
    inst = tmp_path / "inst"
    seed_shared_code(inst)
    (inst / "config").mkdir(exist_ok=True)
    (inst / "config" / "agents.json").write_text(json.dumps({
        "gateway_url": "https://mesh.example.com",
        "extra_top": {"keep": True},
        "agents": {"alpha": {"opencode_url": "http://127.0.0.1:4097"}},
    }))
    (inst / "data").mkdir(exist_ok=True)

    env = base_env(tmp_path, fakebin, logs, **agent_env(
        MESH_SOURCE_DIR=str(source),
        MESH_INSTALL_DIR=str(inst),
        MESH_DEVICE_NAME="Device Beta",
        MESH_INSTALL_ONLY="1",
    ))
    result = run_script("install.sh", ["agent", "beta"], env)

    assert result.returncode == 0, result.stderr
    agents = read_json(inst / "config" / "agents.json")
    assert agents["gateway_url"] == "https://mesh.example.com"
    assert agents["extra_top"] == {"keep": True}
    assert set(agents["agents"]) == {"alpha", "beta"}
    assert agents["agents"]["alpha"]["opencode_url"] == "http://127.0.0.1:4097"
    assert agents["agents"]["beta"]["device_name"] == "Device Beta"
    assert agents["agents"]["beta"]["opencode_url"] == "http://127.0.0.1:4096"
    # config must not write the identity path
    assert "state_file" not in agents and "state_file" not in agents["agents"]["beta"]

    unit = unit_dir(tmp_path) / "opencode-mesh-agent.service"
    assert unit.exists()
    assert "--config" in unit.read_text() and "--instance" not in unit.read_text()
    assert "--all-instances" not in unit.read_text()
    assert not (unit_dir(tmp_path) / "opencode-mesh-agent@beta.service").exists()

    # shared code must not be overwritten
    assert (inst / "src" / "SENTINEL").read_text() == "keep-me\n"
    assert (inst / ".venv" / "bin" / "python").read_text() == STUB_PY
    # install only: daemon-reload runs, no enable/start/restart, no linger
    systemctl_log = logs["systemctl"].read_text()
    assert "daemon-reload" in systemctl_log
    assert "enable" not in systemctl_log and "start" not in systemctl_log
    assert not logs["loginctl"].exists() or logs["loginctl"].read_text() == ""


def test_install_default_bootstraps_new_dir_and_keeps_paths(tmp_path):
    """Default instance installs into a fresh dir: bootstrap shared code, write agents.default and the default unit name (compatibility path)."""
    fakebin, logs = make_fakebin(tmp_path)
    source = make_source(tmp_path)
    inst = tmp_path / "inst2"

    env = base_env(tmp_path, fakebin, logs, **agent_env(
        MESH_SOURCE_DIR=str(source),
        MESH_INSTALL_DIR=str(inst),
        MESH_DEVICE_NAME="Default Box",
        MESH_INSTALL_ONLY="1",
    ))
    result = run_script("install.sh", ["agent"], env)

    assert result.returncode == 0, result.stderr
    assert (inst / "src" / "main.py").exists()
    assert (inst / ".venv" / "bin" / "python").exists()
    agents = read_json(inst / "config" / "agents.json")
    assert set(agents["agents"]) == {"default"}
    assert agents["agents"]["default"]["device_name"] == "Default Box"
    assert (unit_dir(tmp_path) / "opencode-mesh-agent.service").exists()
    log = logs["systemctl"].read_text()
    assert "daemon-reload" in log
    assert "enable" not in log and "start" not in log


def test_install_rejects_legacy_single_config(tmp_path):
    """Legacy config/agent.json present: reject explicitly, never silently overwrite."""
    fakebin, logs = make_fakebin(tmp_path)
    source = make_source(tmp_path)
    inst = tmp_path / "legacy"
    (inst / "config").mkdir(parents=True)
    (inst / "config" / "agent.json").write_text("{}")

    env = base_env(tmp_path, fakebin, logs, **agent_env(
        MESH_SOURCE_DIR=str(source),
        MESH_INSTALL_DIR=str(inst),
    ))
    result = run_script("install.sh", ["agent"], env)

    assert result.returncode != 0
    assert not (inst / "config" / "agents.json").exists()
    assert (inst / "config" / "agent.json").read_text() == "{}"


@pytest.mark.parametrize(
    "args",
    [["agent", "."], ["agent", "a/b"], ["agent", "a b"], ["agent", "a@b"],
     ["agent", "a..b"], ["agent", "default"], ["agent", ""]],
)
def test_install_rejects_bad_instance_names(tmp_path, args):
    fakebin, logs = make_fakebin(tmp_path)
    source = make_source(tmp_path)
    inst = tmp_path / "badname"

    env = base_env(tmp_path, fakebin, logs, **agent_env(
        MESH_SOURCE_DIR=str(source),
        MESH_INSTALL_DIR=str(inst),
    ))
    result = run_script("install.sh", args, env)
    assert result.returncode != 0
    assert not (inst / "config" / "agents.json").exists()


def test_gateway_install_uses_the_selected_port(tmp_path):
    fakebin, logs = make_fakebin(tmp_path)
    source = make_source(tmp_path)
    inst = tmp_path / "gateway"
    env = base_env(tmp_path, fakebin, logs, MESH_SOURCE_DIR=str(source),
                   MESH_INSTALL_DIR=str(inst), MESH_INSTALL_ONLY="1",
                   MESH_LISTEN_PORT="18443", MESH_USERNAME="test", MESH_PASSWORD="test-password")
    result = run_script("install.sh", ["gateway"], env)
    assert result.returncode == 0, result.stderr
    assert read_json(inst / "config/gateway.json")["listen_port"] == 18443


@pytest.mark.parametrize("broken", ["python", "pip", "dependencies"])
def test_gateway_retry_repairs_runtime_without_overwriting_configuration(tmp_path, broken):
    fakebin, logs = make_fakebin(tmp_path)
    source = make_source(tmp_path)
    inst = tmp_path / "partial"
    seed_shared_code(inst)
    (inst / "config").mkdir()
    config = inst / "config/gateway.json"
    config.write_text('{"enroll_token":"keep-token","auth":{"password":"keep-password"}}\n')
    seed_unit(tmp_path, "opencode-mesh-gateway", inst)
    unit = unit_dir(tmp_path) / "opencode-mesh-gateway.service"
    config_before, unit_before = config.read_bytes(), unit.read_bytes()
    python = inst / ".venv/bin/python"
    marker = tmp_path / "dependencies-installed"
    if broken == "python":
        python.unlink()
    elif broken == "pip":
        python.write_text("#!/bin/sh\nexit 1\n")
    else:
        python.write_text(f'''#!{REAL_PY}
import sys
from pathlib import Path
marker = Path({str(marker)!r})
if sys.argv[1:4] == ['-m', 'pip', 'install']:
    if '-e' in sys.argv: marker.touch()
    sys.exit(0)
if sys.argv[1:4] == ['-m', 'pip', '--version']: sys.exit(0)
sys.exit(0 if marker.exists() else 1)
''')
    env = base_env(tmp_path, fakebin, logs, MESH_SOURCE_DIR=str(source), MESH_INSTALL_DIR=str(inst),
                   FAKE_ACTIVE_UNITS="opencode-mesh-gateway.service")
    result = run_script("install.sh", ["gateway"], env)
    assert result.returncode == 0, result.stderr
    assert config.read_bytes() == config_before
    assert unit.read_bytes() == unit_before
    assert (inst / "src/SENTINEL").read_text() == "keep-me\n"
    assert "restart opencode-mesh-gateway.service" in logs["systemctl"].read_text()
    assert "keep-token" not in result.stdout and "keep-password" not in result.stdout
    if broken == "dependencies":
        assert marker.exists()


def test_install_gateway_rejects_instance(tmp_path):
    fakebin, logs = make_fakebin(tmp_path)
    source = make_source(tmp_path)
    inst = tmp_path / "gw"

    env = base_env(tmp_path, fakebin, logs, **agent_env(
        MESH_SOURCE_DIR=str(source),
        MESH_INSTALL_DIR=str(inst),
    ))
    result = run_script("install.sh", ["gateway", "win"], env)
    assert result.returncode != 0


@pytest.mark.parametrize(
    ("tmp_mode", "accepted"),
    [("777", False), ("1777", True)],
)
def test_install_requires_a_sticky_tmp_without_changing_host_tmp(tmp_path, tmp_mode, accepted):
    """The real installer must reject a non-sticky /tmp reported by the stat stub."""
    fakebin, logs = make_fakebin(tmp_path)
    source = make_source(tmp_path)
    inst = tmp_path / f"tmp-mode-{tmp_mode}"
    env = base_env(tmp_path, fakebin, logs, **agent_env(
        MESH_SOURCE_DIR=str(source),
        MESH_INSTALL_DIR=str(inst),
        MESH_INSTALL_ONLY="1",
        FAKE_TMP_MODE=tmp_mode,
    ))

    result = run_script("install.sh", ["agent"], env)

    assert (result.returncode == 0) is accepted, result.stderr
    if not accepted:
        assert "unsafe /tmp permissions" in result.stderr


@pytest.mark.parametrize("holder_target", ["regular", "dangling"])
def test_install_rejects_untrusted_lock_entries_then_allows_a_normal_lock(tmp_path, holder_target):
    """Pre-existing foreign lock metadata or any holder symlink must not be followed."""
    fakebin, logs = make_fakebin(tmp_path)
    source = make_source(tmp_path)
    inst = tmp_path / f"untrusted-{holder_target}"
    lock_dir = lifecycle_lock_dir(inst)
    if lock_dir.exists() or lock_dir.is_symlink():
        pytest.skip(f"test lock path already exists: {lock_dir}")
    lock_dir.mkdir(mode=0o700)
    holder = lock_dir / "holder"
    target = tmp_path / "holder-target"
    if holder_target == "regular":
        target.write_text("not a lock")
    holder.symlink_to(target)

    env = base_env(tmp_path, fakebin, logs, **agent_env(
        MESH_SOURCE_DIR=str(source),
        MESH_INSTALL_DIR=str(inst),
        MESH_INSTALL_ONLY="1",
    ))
    try:
        rejected = run_script("install.sh", ["agent"], env)
        assert rejected.returncode != 0
        assert "unsafe Mesh lifecycle lock holder" in rejected.stderr
        if holder_target == "dangling":
            assert not target.exists(), "a dangling holder symlink must never be followed"
        else:
            assert target.read_text() == "not a lock"

        # This directory was created by this test.  Removing it proves a fresh,
        # well-formed lock can still be acquired after a malicious one is refused.
        holder.unlink()
        lock_dir.rmdir()
        accepted = run_script("install.sh", ["agent"], env)
        assert accepted.returncode == 0, accepted.stderr
    finally:
        if lock_dir.exists() and not lock_dir.is_symlink():
            for child in lock_dir.iterdir():
                child.unlink()
            lock_dir.rmdir()


def test_install_creates_a_missing_nested_install_root(tmp_path):
    """Ownership discovery must not stat a parent that does not exist yet."""
    fakebin, logs = make_fakebin(tmp_path)
    source = make_source(tmp_path)
    inst = tmp_path / "missing" / "nested" / "install"
    env = base_env(tmp_path, fakebin, logs, **agent_env(
        MESH_SOURCE_DIR=str(source),
        MESH_INSTALL_DIR=str(inst),
        MESH_INSTALL_ONLY="1",
        FAKE_REAL_INSTALL_OWNER="1",
    ))

    result = run_script("install.sh", ["agent"], env)

    assert result.returncode == 0, result.stderr
    assert (inst / "config" / "agents.json").exists()


def test_install_serializes_only_the_same_canonical_directory(tmp_path):
    """A held lifecycle lock rejects its directory but does not block another install."""
    fakebin, logs = make_fakebin(tmp_path)
    source = make_source(tmp_path)
    locked = tmp_path / "locked"
    other = tmp_path / "other"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    key = hashlib.sha256(str(locked.resolve()).encode()).hexdigest()
    lock_dir = Path(f"/tmp/opencode-mesh-{key}.lock")
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_dir.chmod(0o700)
    lock_path = lock_dir / "holder"
    lock_path.touch()
    lock_path.chmod(0o600)
    with lock_path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked_env = base_env(tmp_path, fakebin, logs, **agent_env(
            MESH_SOURCE_DIR=str(source), MESH_INSTALL_DIR=str(locked),
            MESH_INSTALL_ONLY="1", XDG_RUNTIME_DIR=str(runtime),
        ))
        blocked = run_script("install.sh", ["agent"], locked_env)
        assert blocked.returncode != 0
        assert "another Mesh lifecycle operation" in blocked.stderr
        other_env = base_env(tmp_path, fakebin, logs, **agent_env(
            MESH_SOURCE_DIR=str(source), MESH_INSTALL_DIR=str(other),
            MESH_INSTALL_ONLY="1", XDG_RUNTIME_DIR=str(runtime),
        ))
        allowed = run_script("install.sh", ["agent"], other_env)
        assert allowed.returncode == 0, allowed.stderr


# --------------------------------------------------------------- uninstall.sh


def make_install_state(tmp_path, inst):
    """Lay out an install dir containing default and win instances plus units."""
    (inst / "config").mkdir(parents=True, exist_ok=True)
    (inst / "config" / "agents.json").write_text(json.dumps({
        "gateway_url": "https://mesh.example.com",
        "enroll_token": "e",
        "agents": {"default": {"opencode_url": "http://127.0.0.1:4096"},
                   "win": {"opencode_url": "http://127.0.0.1:4097"}},
    }))
    data = inst / "data"
    data.mkdir(exist_ok=True)
    for name in ("agent-state.json", "agent-state-win.json"):
        (data / name).write_text(json.dumps({"device_id": f"dev-{name}", "agent_token": "tok"}))
    (inst / "src").mkdir(exist_ok=True)
    (inst / "src" / "SENTINEL").write_text("keep\n")
    (inst / "deploy").mkdir(exist_ok=True) if not (inst / "deploy").exists() else None
    seed_unit(tmp_path, "opencode-mesh-agent", inst)
    seed_unit(tmp_path, "opencode-mesh-agent@win", inst)
    return inst


def uninstall_env(tmp_path, fakebin, logs, inst, **extra):
    return base_env(tmp_path, fakebin, logs, MESH_INSTALL_DIR=str(inst), **extra)


def test_uninstall_single_instances_isolated(tmp_path):
    """Single-instance uninstall: remove only the matching unit and config keys; shared dirs, state and other instances stay; the last instance removes the empty agents.json."""
    fakebin, logs = make_fakebin(tmp_path)
    inst = make_install_state(tmp_path, tmp_path / "u1")
    ud = unit_dir(tmp_path)

    env = uninstall_env(tmp_path, fakebin, logs, inst)
    result = run_script("uninstall.sh", ["agent"], env)
    assert result.returncode == 0, result.stderr
    assert not (ud / "opencode-mesh-agent.service").exists()
    assert (ud / "opencode-mesh-agent@win.service").exists()
    agents = read_json(inst / "config" / "agents.json")
    assert set(agents["agents"]) == {"win"}  # default key removed, win kept
    assert (inst / "data" / "agent-state.json").exists()  # state kept
    # shared dir and src kept
    assert (inst / "src" / "SENTINEL").read_text() == "keep\n"

    # uninstall last instance: agents.json deleted once agents is empty, data still kept
    result = run_script("uninstall.sh", ["agent", "win"], env)
    assert result.returncode == 0, result.stderr
    assert not (ud / "opencode-mesh-agent@win.service").exists()
    assert read_json(inst / "config" / "agents.json")["agents"] == {}
    assert (inst / "data" / "agent-state-win.json").exists()
    assert inst.exists()


def test_uninstall_default_with_other_instance_keeps_directory(tmp_path):
    """Default instance uninstall while another (inactive) instance exists -> keep shared dir and config."""
    fakebin, logs = make_fakebin(tmp_path)
    inst = make_install_state(tmp_path, tmp_path / "u2")
    ud = unit_dir(tmp_path)

    env = uninstall_env(tmp_path, fakebin, logs, inst)
    result = run_script("uninstall.sh", ["agent"], env)
    assert result.returncode == 0, result.stderr
    assert not (ud / "opencode-mesh-agent.service").exists()
    assert (ud / "opencode-mesh-agent@win.service").exists()
    assert inst.exists()
    agents = read_json(inst / "config" / "agents.json")
    assert set(agents["agents"]) == {"win"}


def test_uninstall_all_removes_everything(tmp_path):
    """all: remove every unit and config key, deregister each agent, delete the install dir when default KEEP=N."""
    fakebin, logs = make_fakebin(tmp_path)
    inst = make_install_state(tmp_path, tmp_path / "u3")
    seed_unit(tmp_path, "opencode-mesh-gateway", inst)
    (inst / "config" / "gateway.json").write_text("{}")
    ud = unit_dir(tmp_path)

    env = uninstall_env(tmp_path, fakebin, logs, inst)
    result = run_script("uninstall.sh", ["all"], env)
    assert result.returncode == 0, result.stderr
    assert not (ud / "opencode-mesh-agent.service").exists()
    assert not (ud / "opencode-mesh-agent@win.service").exists()
    assert not (ud / "opencode-mesh-gateway.service").exists()
    assert not inst.exists()
    curl_log = logs["curl"].read_text()
    assert curl_log.count("_mesh/deregister/") == 2
    assert "dev-agent-state.json" in curl_log and "dev-agent-state-win.json" in curl_log


def test_uninstall_rejects_bad_instance_args(tmp_path):
    fakebin, logs = make_fakebin(tmp_path)
    inst = make_install_state(tmp_path, tmp_path / "u4")

    env = uninstall_env(tmp_path, fakebin, logs, inst)
    assert run_script("uninstall.sh", ["gateway", "win"], env).returncode != 0
    assert run_script("uninstall.sh", ["agent", ""], env).returncode != 0
    assert run_script("uninstall.sh", ["agent", "default"], env).returncode != 0
    assert run_script("uninstall.sh", ["agent", "a/b"], env).returncode != 0


# ------------------------------------------------- one shared supervisor unit


def seed_supervisor_unit(tmp_path, install_dir):
    d = unit_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "opencode-mesh-agent.service").write_text(
        f"[Unit]\n[Service]\nWorkingDirectory={install_dir}\n"
        f"ExecStart={install_dir}/.venv/bin/python -m src.main --mode agent "
        f"--config {install_dir}/config/agents.json\n"
    )


def unit_exec_start(tmp_path, name):
    unit = unit_dir(tmp_path) / f"{name}.service"
    if not unit.exists():
        return ""
    for line in unit.read_text().splitlines():
        if line.startswith("ExecStart="):
            return line.split("=", 1)[1]
    return ""


def lifecycle_events(logs):
    """systemctl and curl calls in the order the mock commands observed them."""
    if not logs["events"].exists():
        return []
    return logs["events"].read_text().splitlines()


def event_position(events, needle):
    for position, line in enumerate(events):
        if needle in line:
            return position
    return -1


def make_supervisor_install_state(tmp_path):
    fakebin, logs = make_fakebin(tmp_path)
    source = make_source(tmp_path)
    inst = tmp_path / "inst"
    seed_shared_code(inst)
    (inst / "config").mkdir(exist_ok=True)
    (inst / "config" / "agents.json").write_text(json.dumps({
        "gateway_url": "https://mesh.example.com",
        "enroll_token": "enroll-token",
        "agents": {"default": {"opencode_url": "http://127.0.0.1:4096"}},
    }))
    return fakebin, logs, inst, source


def supervisor_install_env(tmp_path, fakebin, logs, source, inst, **extra):
    return base_env(tmp_path, fakebin, logs, **agent_env(
        MESH_SOURCE_DIR=str(source), MESH_INSTALL_DIR=str(inst),
        MESH_INSTALL_ONLY="1", FAKE_REAL_INSTALL_OWNER="1", **extra))


def test_unified_install_puts_every_instance_under_the_shared_unit(tmp_path):
    """Every installation uses one supervisor unit for the whole mapping without an opt-in."""
    fakebin, logs, inst, source = make_supervisor_install_state(tmp_path)

    result = run_script("install.sh", ["agent", "win"],
                        supervisor_install_env(tmp_path, fakebin, logs, source, inst))

    assert result.returncode == 0, result.stderr
    exec_start = unit_exec_start(tmp_path, "opencode-mesh-agent")
    assert "--mode agent" in exec_start
    assert "--all-instances" not in exec_start
    assert "--instance" not in exec_start
    assert not (unit_dir(tmp_path) / "opencode-mesh-agent@win.service").exists()
    assert set(read_json(inst / "config" / "agents.json")["agents"]) == {"default", "win"}


def test_unified_install_refuses_to_overwrite_a_configured_instance(tmp_path):
    """Re-running the installer over an existing instance name must not silently replace its values."""
    fakebin, logs, inst, source = make_supervisor_install_state(tmp_path)
    config = read_json(inst / "config" / "agents.json")
    config["agents"]["win"] = {"opencode_url": "http://127.0.0.1:4099"}
    (inst / "config" / "agents.json").write_text(json.dumps(config))

    result = run_script("install.sh", ["agent", "win"],
                        supervisor_install_env(tmp_path, fakebin, logs, source, inst))

    assert result.returncode != 0
    assert "already configured" in result.stderr
    assert read_json(inst / "config" / "agents.json")["agents"]["win"]["opencode_url"] \
        == "http://127.0.0.1:4099"


def test_a_second_unified_install_adds_an_instance_instead_of_refusing(tmp_path):
    """The shared unit already exists, so a later run must extend the mapping rather than bail out."""
    fakebin, logs, inst, source = make_supervisor_install_state(tmp_path)
    env = supervisor_install_env(tmp_path, fakebin, logs, source, inst)
    assert run_script("install.sh", ["agent", "win"], env).returncode == 0

    result = run_script("install.sh", ["agent", "beta"], env)

    assert result.returncode == 0, result.stderr
    assert set(read_json(inst / "config" / "agents.json")["agents"]) == {"default", "win", "beta"}


def test_install_only_refuses_to_change_a_running_service(tmp_path):
    """The supervisor read its instance set at start-up, so a config edit under INSTALL_ONLY would only
    take effect at an unrelated restart and register a device nobody asked for."""
    fakebin, logs, inst, source = make_supervisor_install_state(tmp_path)
    env = supervisor_install_env(tmp_path, fakebin, logs, source, inst)
    assert run_script("install.sh", ["agent", "win"], env).returncode == 0
    unit = unit_dir(tmp_path) / "opencode-mesh-agent.service"
    config = inst / "config" / "agents.json"
    unit_before, config_before = unit.read_bytes(), config.read_bytes()

    blocked = run_script("install.sh", ["agent", "beta"],
                         supervisor_install_env(tmp_path, fakebin, logs, source, inst,
                                                FAKE_ACTIVE_UNITS="opencode-mesh-agent.service"))

    assert blocked.returncode != 0
    assert "running" in blocked.stderr
    assert unit.read_bytes() == unit_before
    assert config.read_bytes() == config_before


def test_unified_install_reuses_the_shared_unit_only_when_it_owns_this_directory(tmp_path):
    """Rewriting a unit from another install root would hand this mapping to a foreign service."""
    fakebin, logs, inst, source = make_supervisor_install_state(tmp_path)
    seed_supervisor_unit(tmp_path, Path("/elsewhere/opencode-mesh"))
    before = (inst / "config" / "agents.json").read_bytes()

    result = run_script("install.sh", ["agent", "win"],
                        supervisor_install_env(tmp_path, fakebin, logs, source, inst))

    assert result.returncode != 0
    assert "another directory" in result.stderr
    assert (inst / "config" / "agents.json").read_bytes() == before


def test_unified_install_of_the_default_instance_omits_the_name(tmp_path):
    """`agent default` stays a usage error; the default instance is configured by leaving the name off."""
    fakebin, logs = make_fakebin(tmp_path)
    source = make_source(tmp_path)
    inst = tmp_path / "inst"
    seed_shared_code(inst)
    (inst / "config").mkdir(exist_ok=True)
    (inst / "config" / "agents.json").write_text(json.dumps({
        "gateway_url": "https://mesh.example.com",
        "enroll_token": "enroll-token",
        "agents": {},
    }))
    env = supervisor_install_env(tmp_path, fakebin, logs, source, inst)

    rejected = run_script("install.sh", ["agent", "default"], env)
    assert rejected.returncode != 0
    assert "default" in rejected.stderr

    accepted = run_script("install.sh", ["agent"], env)
    assert accepted.returncode == 0, accepted.stderr
    assert set(read_json(inst / "config" / "agents.json")["agents"]) == {"default"}
    assert "--all-instances" not in unit_exec_start(tmp_path, "opencode-mesh-agent")
    assert "--mode agent" in unit_exec_start(tmp_path, "opencode-mesh-agent")


def test_unified_install_is_refused_while_a_legacy_instance_unit_exists(tmp_path):
    """Two units managing the same mapping would fight over the same identities, so one must go first."""
    fakebin, logs, inst, source = make_supervisor_install_state(tmp_path)
    seed_unit(tmp_path, "opencode-mesh-agent@win", inst)

    result = run_script("install.sh", ["agent", "win"],
                        supervisor_install_env(tmp_path, fakebin, logs, source, inst))

    assert result.returncode != 0
    assert "legacy" in result.stderr.lower()


@pytest.mark.parametrize("old_selector", ["--instance default", "--all-instances"])
def test_install_requires_migration_of_an_old_default_unit(tmp_path, old_selector):
    fakebin, logs, inst, source = make_supervisor_install_state(tmp_path)
    seed_supervisor_unit(tmp_path, inst)
    unit = unit_dir(tmp_path) / "opencode-mesh-agent.service"
    unit.write_text(unit.read_text().rstrip() + " " + old_selector + "\n")
    before_unit = unit.read_bytes()
    config = inst / "config/agents.json"
    before_config = config.read_bytes()
    result = run_script("install.sh", ["agent", "win"],
                         supervisor_install_env(tmp_path, fakebin, logs, source, inst))
    assert result.returncode != 0
    assert "migrate" in result.stderr
    assert unit.read_bytes() == before_unit
    assert config.read_bytes() == before_config
    assert not lifecycle_events(logs)


def test_plain_install_reuses_the_shared_unit(tmp_path):
    """No environment switch is needed to add an instance to the existing service."""
    fakebin, logs, inst, source = make_supervisor_install_state(tmp_path)
    seed_supervisor_unit(tmp_path, inst)

    result = run_script("install.sh", ["agent", "win"], base_env(
        tmp_path, fakebin, logs, **agent_env(
            MESH_SOURCE_DIR=str(source), MESH_INSTALL_DIR=str(inst),
            MESH_INSTALL_ONLY="1", FAKE_REAL_INSTALL_OWNER="1")))

    assert result.returncode == 0, result.stderr
    assert set(read_json(inst / "config/agents.json")["agents"]) == {"default", "win"}
    assert not (unit_dir(tmp_path) / "opencode-mesh-agent@win.service").exists()


@pytest.mark.parametrize("mode", ["agent", "gateway"])
@pytest.mark.parametrize("value", ["0", "1"])
def test_removed_service_mode_switch_is_rejected(tmp_path, mode, value):
    """An obsolete mode switch must not silently select a different service layout."""
    fakebin, logs = make_fakebin(tmp_path)
    source = make_source(tmp_path)
    inst = tmp_path / "gw"

    result = run_script("install.sh", [mode], base_env(
        tmp_path, fakebin, logs, MESH_SOURCE_DIR=str(source), MESH_INSTALL_DIR=str(inst),
        MESH_INSTALL_ONLY="1", FAKE_REAL_INSTALL_OWNER="1", MESH_ALL_INSTANCES=value,
        MESH_LISTEN_PORT="18080", MESH_USERNAME="admin", MESH_PASSWORD="pw"))

    assert result.returncode != 0
    assert "removed" in result.stderr.lower()


def test_install_pins_an_instance_gateway_that_differs_from_the_shared_one(tmp_path):
    """Instance overrides never change the shared defaults used by the other instances."""
    fakebin, logs, inst, source = make_supervisor_install_state(tmp_path)
    config = read_json(inst / "config" / "agents.json")
    config["agents"]["alpha"] = {"opencode_url": "http://127.0.0.1:4097"}
    (inst / "config" / "agents.json").write_text(json.dumps(config))
    env = supervisor_install_env(tmp_path, fakebin, logs, source, inst,
                                 MESH_GATEWAY_URL="https://other.example.com",
                                 MESH_ENROLL_TOKEN="other-token")

    result = run_script("install.sh", ["agent", "win"], env)

    assert result.returncode == 0, result.stderr
    merged = read_json(inst / "config" / "agents.json")
    assert merged["gateway_url"] == "https://mesh.example.com"
    assert merged["agents"]["win"]["gateway_url"] == "https://other.example.com"
    assert "gateway_url" not in merged["agents"]["alpha"]
    assert merged["agents"]["win"]["enroll_token"] == "other-token"
    assert merged["enroll_token"] == "enroll-token"


def test_uninstall_stops_the_shared_unit_before_deregistering_the_default_instance(tmp_path):
    """The Gateway refuses to drop a device whose Agent is still connected, so the unit stops first."""
    fakebin, logs = make_fakebin(tmp_path)
    inst = make_install_state(tmp_path, tmp_path / "u5")
    seed_supervisor_unit(tmp_path, inst)

    env = uninstall_env(tmp_path, fakebin, logs, inst, FAKE_ACTIVE_UNITS="opencode-mesh-agent.service")
    result = run_script("uninstall.sh", ["agent"], env)
    assert result.returncode == 0, result.stderr

    events = lifecycle_events(logs)
    stop = event_position(events, "stop opencode-mesh-agent.service")
    deregister = event_position(events, "/_mesh/deregister/")
    assert stop >= 0 and deregister >= 0, events
    assert stop < deregister, events
    assert not [line for line in events if line.startswith("deregister-conflict")], events
    # the mapping shrank by one instance and the survivors keep running afterwards
    assert set(read_json(inst / "config" / "agents.json")["agents"]) == {"win"}
    assert (unit_dir(tmp_path) / "opencode-mesh-agent.service").exists()
    assert event_position(events, "restart opencode-mesh-agent.service") > deregister, events
    assert (inst / "data" / "agent-state.json").exists()


def test_uninstall_stops_the_shared_unit_before_deregistering_a_named_instance(tmp_path):
    """A named instance lives under the shared unit too, so that unit must be handled, not the @name one."""
    fakebin, logs = make_fakebin(tmp_path)
    inst = make_install_state(tmp_path, tmp_path / "u7")
    seed_supervisor_unit(tmp_path, inst)

    env = uninstall_env(tmp_path, fakebin, logs, inst, FAKE_ACTIVE_UNITS="opencode-mesh-agent.service")
    result = run_script("uninstall.sh", ["agent", "win"], env)
    assert result.returncode == 0, result.stderr

    events = lifecycle_events(logs)
    stop = event_position(events, "stop opencode-mesh-agent.service")
    deregister = event_position(events, "/_mesh/deregister/")
    assert stop >= 0 and deregister >= 0, events
    assert stop < deregister, events
    assert not [line for line in events if line.startswith("deregister-conflict")], events
    assert set(read_json(inst / "config" / "agents.json")["agents"]) == {"default"}
    assert (unit_dir(tmp_path) / "opencode-mesh-agent.service").exists()
    assert (inst / "data" / "agent-state-win.json").exists()


def test_uninstall_leaves_an_inactive_shared_unit_stopped(tmp_path):
    """A service that was already stopped must stay stopped, and needs no stop before deregistering."""
    fakebin, logs = make_fakebin(tmp_path)
    inst = make_install_state(tmp_path, tmp_path / "u8")
    seed_supervisor_unit(tmp_path, inst)

    env = uninstall_env(tmp_path, fakebin, logs, inst)
    result = run_script("uninstall.sh", ["agent"], env)
    assert result.returncode == 0, result.stderr

    events = lifecycle_events(logs)
    assert not [line for line in events
                if " stop " in line or " restart " in line or " disable " in line], events
    assert event_position(events, "/_mesh/deregister/") >= 0, events
    assert set(read_json(inst / "config" / "agents.json")["agents"]) == {"win"}
    assert (unit_dir(tmp_path) / "opencode-mesh-agent.service").exists()


def test_uninstall_aborts_before_deregistering_when_the_shared_unit_cannot_stop(tmp_path):
    """If the Agent cannot be stopped the device stays connected, so no deregistration and no config edit."""
    fakebin, logs = make_fakebin(tmp_path)
    inst = make_install_state(tmp_path, tmp_path / "u9")
    seed_supervisor_unit(tmp_path, inst)
    before = (inst / "config" / "agents.json").read_bytes()

    env = uninstall_env(tmp_path, fakebin, logs, inst,
                        FAKE_ACTIVE_UNITS="opencode-mesh-agent.service",
                        FAKE_STOP_FAIL_UNITS="opencode-mesh-agent.service")
    result = run_script("uninstall.sh", ["agent"], env)

    assert result.returncode != 0
    assert (inst / "config" / "agents.json").read_bytes() == before
    assert not [line for line in lifecycle_events(logs) if "/_mesh/deregister/" in line]
    assert (unit_dir(tmp_path) / "opencode-mesh-agent.service").exists()


def test_uninstall_refuses_a_shared_unit_owned_by_another_directory(tmp_path):
    """The shared unit supervises every instance, so its ownership is checked before anything is touched."""
    fakebin, logs = make_fakebin(tmp_path)
    inst = make_install_state(tmp_path, tmp_path / "u10")
    seed_supervisor_unit(tmp_path, Path("/elsewhere/opencode-mesh"))
    before = (inst / "config" / "agents.json").read_bytes()

    env = uninstall_env(tmp_path, fakebin, logs, inst, FAKE_ACTIVE_UNITS="opencode-mesh-agent.service")
    result = run_script("uninstall.sh", ["agent", "win"], env)

    assert result.returncode != 0
    assert "another directory" in result.stderr
    assert (inst / "config" / "agents.json").read_bytes() == before
    assert not [line for line in lifecycle_events(logs) if "/_mesh/deregister/" in line]


def test_uninstall_removes_the_shared_unit_with_the_last_instance(tmp_path):
    """With nothing left to supervise, the unit is disabled and deleted as any other one."""
    fakebin, logs = make_fakebin(tmp_path)
    inst = tmp_path / "u6"
    (inst / "config").mkdir(parents=True)
    (inst / "config" / "agents.json").write_text(json.dumps({
        "gateway_url": "https://mesh.example.com", "enroll_token": "e",
        "agents": {"default": {"opencode_url": "http://127.0.0.1:4096"}}}))
    (inst / "data").mkdir()
    (inst / "data" / "agent-state.json").write_text(
        json.dumps({"device_id": "dev-default", "agent_token": "tok"}))
    seed_supervisor_unit(tmp_path, inst)

    env = uninstall_env(tmp_path, fakebin, logs, inst, FAKE_ACTIVE_UNITS="opencode-mesh-agent.service")
    result = run_script("uninstall.sh", ["agent"], env)

    assert result.returncode == 0, result.stderr
    assert not (unit_dir(tmp_path) / "opencode-mesh-agent.service").exists()
    assert read_json(inst / "config" / "agents.json")["agents"] == {}
    events = lifecycle_events(logs)
    assert event_position(events, "disable opencode-mesh-agent.service") > 0, events
    assert event_position(events, "/_mesh/deregister/") > \
        event_position(events, "stop opencode-mesh-agent.service"), events
    assert (inst / "data" / "agent-state.json").exists()
