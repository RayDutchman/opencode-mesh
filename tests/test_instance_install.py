"""install.sh / uninstall.sh multi-instance behavior tests.

Contract (docs/superpowers/specs/2026-09-23-multi-agent-design.md, confirmed with the user):
- All agents share the manual config/agents.json: top-level common fields + per-instance agents mapping.
- Config does not write state_file; identity is derived by the program from the install root and instance name (default agent-state.json, named agent-state-<name>.json).
- Default service opencode-mesh-agent.service (--instance default), named opencode-mesh-agent@<name>.service.
- Install only merges the given instance keys, keeping other instances and unknown top-level fields; existing shared code is not overwritten; MESH_INSTALL_ONLY=1 does not enable/start.
- Legacy single-agent configs (agent.json / agent.local.json / agent-*.json) are explicitly rejected, never silently overwritten.
- Single-instance uninstall removes only that instance's config keys and standalone unit, keeping shared directories, other instances and auto identity state.
- Everything is verified through temp directories and mock commands, no real deployment.

Run: .venv/bin/python -m pytest tests/test_instance_install.py -q
"""

import json
import os
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

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_SYSTEMCTL_LOG:?}"
exit 0
"""

FAKE_LOGINCTL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_LOGINCTL_LOG:?}"
exit 0
"""

FAKE_CURL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_CURL_LOG:?}"
exit 0
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
    }
    tools = {
        "id": FAKE_ID,
        "systemctl": FAKE_SYSTEMCTL,
        "loginctl": FAKE_LOGINCTL,
        "curl": FAKE_CURL,
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

    unit = unit_dir(tmp_path) / "opencode-mesh-agent@beta.service"
    assert unit.exists()
    assert "--config" in unit.read_text() and "--instance beta" in unit.read_text()

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
