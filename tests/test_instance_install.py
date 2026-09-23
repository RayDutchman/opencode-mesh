"""install.sh / deploy-agent.sh / uninstall.sh 同机多实例行为测试。

契约（docs/superpowers/specs/2026-09-23-multi-agent-design.md 与用户确认）：
- 所有 Agent 共享人工配置 config/agents.json：顶层公共字段 + agents 映射按实例。
- 配置不写 state_file，身份由程序按安装根目录与实例名派生（默认 agent-state.json，具名 agent-state-<name>.json）。
- 默认服务 opencode-mesh-agent.service（--instance default），具名 opencode-mesh-agent@<name>.service。
- 安装只合并指定实例键，保留其他实例与未知顶层字段；已有共享代码不覆盖；MESH_INSTALL_ONLY=1 不启用/启动。
- 检测到旧式单 Agent 配置（agent.json / agent.local.json / agent-*.json）时显式拒绝，不默默覆盖。
- 单实例卸载只移除该实例配置键与独立 unit，保留共享目录、其他实例与自动身份状态。
- 全部通过临时目录与 mock 命令验证，不真实部署。

运行：.venv/bin/python -m pytest tests/test_instance_install.py -q
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

# 临时文件保存远端脚本/数据，由 mock ssh 就地执行（模拟远端文件系统）。
FAKE_SSH = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "${FAKE_SSH_LOG:?}"
while [[ "${1:-}" == -o ]]; do shift 2; done
shift
exec bash -c "$*"
"""

# venv 内 python stub：记录 pip 调用，回答版本查询，其余成功退出。
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

# PATH 中的 python3：拦截 venv/pip/版本查询，其余透传到真实解释器（含 heredoc JSON 生成）。
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
    """构造 fake 命令目录，返回 (fakebin, logs 字典)。"""
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
    """构造 MESH_SOURCE_DIR / tar 打包用的伪源码仓库。"""
    src = tmp_path / "mesh-src"
    (src / "src").mkdir(parents=True)
    (src / "src" / "main.py").write_text("placeholder\n")
    (src / "scripts").mkdir()
    (src / "scripts" / "install.sh").write_text("#!/usr/bin/env bash\n")
    (src / "pyproject.toml").write_text("[project]\n")
    (src / "config").mkdir()
    (src / "config" / ".keep").write_text("")
    (src / "deploy").mkdir()
    (src / "deploy" / "opencode-mesh-agent.service.example").write_text(
        "[Service]\nWorkingDirectory=/opt/opencode-mesh\n"
        "ExecStart=/opt/opencode-mesh/.venv/bin/python -m src.main --mode agent --config /opt/opencode-mesh/config/agent.json\n"
        "ReadWritePaths=/opt/opencode-mesh/data /opt/opencode-mesh/config\n"
    )
    return src


def seed_shared_code(install_dir):
    """在既有安装目录铺共享代码（安装/部署不得覆盖它们）。"""
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
    (install_dir / "deploy" / "opencode-mesh-agent.service.example").write_text(
        "[Service]\nWorkingDirectory=/opt/opencode-mesh\n"
        "ExecStart=/opt/opencode-mesh/.venv/bin/python -m src.main --mode agent --config /opt/opencode-mesh/config/agent.json\n"
        "ReadWritePaths=/opt/opencode-mesh/data /opt/opencode-mesh/config\n"
    )


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
    """具名安装：保留共享源码/venv，只更新 agents[beta]，保留其他实例与未知顶层字段，仅安装不启用。"""
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
    # 配置不写身份路径
    assert "state_file" not in agents and "state_file" not in agents["agents"]["beta"]

    unit = unit_dir(tmp_path) / "opencode-mesh-agent@beta.service"
    assert unit.exists()
    assert "--config" in unit.read_text() and "--instance beta" in unit.read_text()

    # 共享代码未被覆盖
    assert (inst / "src" / "SENTINEL").read_text() == "keep-me\n"
    assert (inst / ".venv" / "bin" / "python").read_text() == STUB_PY
    # 仅安装：daemon-reload 执行，不 enable/start/restart，不启用 linger
    systemctl_log = logs["systemctl"].read_text()
    assert "daemon-reload" in systemctl_log
    assert "enable" not in systemctl_log and "start" not in systemctl_log
    assert not logs["loginctl"].exists() or logs["loginctl"].read_text() == ""


def test_install_default_bootstraps_new_dir_and_keeps_paths(tmp_path):
    """默认实例安装到新目录：bootstrap 共享代码，写 agents.default 与默认单元名（兼容路径）。"""
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
    """旧式 config/agent.json 存在时显式拒绝，不默默覆盖。"""
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


# --------------------------------------------------------------- deploy-agent.sh


def test_deploy_named_existing_code_skips_source_and_merges(tmp_path):
    """具名远端部署到已有共享代码目录：不传源码、远端合并 agents.json、仅安装不启用。"""
    fakebin, logs = make_fakebin(tmp_path)
    inst = tmp_path / "remote-inst"
    seed_shared_code(inst)
    (inst / "config").mkdir(exist_ok=True)
    (inst / "config" / "agents.json").write_text(json.dumps({
        "gateway_url": "https://mesh.example.com",
        "agents": {"alpha": {"opencode_url": "http://127.0.0.1:4097"}},
    }))
    (inst / "data").mkdir(exist_ok=True)

    env = base_env(tmp_path, fakebin, logs, **agent_env(
        MESH_INSTANCE="beta",
        MESH_DEVICE_NAME="Remote Beta",
        MESH_INSTALL_ONLY="1",
        HOME=str(tmp_path / "home"),
    ))
    result = run_script("deploy-agent.sh", ["user@device", str(inst)], env)

    assert result.returncode == 0, result.stderr
    agents = read_json(inst / "config" / "agents.json")
    assert set(agents["agents"]) == {"alpha", "beta"}
    assert agents["agents"]["beta"]["device_name"] == "Remote Beta"
    assert "state_file" not in agents["agents"]["beta"]
    # 已有共享代码：没有 tar 传输与 venv/pip 调用
    ssh_log = logs["ssh"].read_text()
    assert "tar -xzf" not in ssh_log
    assert not logs["pip"].exists() or not logs["pip"].read_text()
    unit = unit_dir(tmp_path) / "opencode-mesh-agent@beta.service"
    assert unit.exists()
    assert "--instance beta" in unit.read_text()
    assert "enable --now" not in ssh_log
    assert (inst / "src" / "SENTINEL").read_text() == "keep-me\n"


def test_deploy_named_new_dir_bootstraps_and_enables(tmp_path):
    """具名远端部署到新目录：tar 传输源码、远端建 venv、写 agents.json、完整安装启用服务。"""
    fakebin, logs = make_fakebin(tmp_path)
    source = make_source(tmp_path)
    inst = tmp_path / "fresh-inst"

    env = base_env(tmp_path, fakebin, logs, **agent_env(
        MESH_INSTANCE="beta",
        MESH_DEVICE_NAME="Fresh Beta",
        HOME=str(tmp_path / "home"),
    ))
    result = run_script("deploy-agent.sh", ["user@device", str(inst)], env, cwd=source)

    assert result.returncode == 0, result.stderr
    ssh_log = logs["ssh"].read_text()
    assert "tar -xzf" in ssh_log
    assert (inst / "src" / "main.py").exists()
    assert (inst / ".venv" / "bin" / "python").exists()
    agents = read_json(inst / "config" / "agents.json")
    assert set(agents["agents"]) == {"beta"}
    unit = unit_dir(tmp_path) / "opencode-mesh-agent@beta.service"
    assert unit.exists()
    assert "--instance beta" in unit.read_text()
    assert "enable --now opencode-mesh-agent@beta.service" in logs["systemctl"].read_text()


def test_deploy_rejects_legacy_config_on_remote(tmp_path):
    """远端已有旧式 agent.local.json 时显式拒绝。"""
    fakebin, logs = make_fakebin(tmp_path)
    source = make_source(tmp_path)
    inst = tmp_path / "legacy-remote"
    (inst / "config").mkdir(parents=True)
    (inst / "config" / "agent.local.json").write_text("{}")

    env = base_env(tmp_path, fakebin, logs, **agent_env(
        MESH_INSTANCE="beta",
        HOME=str(tmp_path / "home"),
    ))
    result = run_script("deploy-agent.sh", ["user@device", str(inst)], env, cwd=source)

    assert result.returncode != 0
    assert not (inst / "config" / "agents.json").exists()


# --------------------------------------------------------------- uninstall.sh


def make_install_state(tmp_path, inst):
    """铺一套含 default 与 win 两个实例的安装目录 + 单元。"""
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
    """单实例卸载：只移除对应单元与配置键；共享目录、状态、其他实例保留；末实例删除空 agents.json。"""
    fakebin, logs = make_fakebin(tmp_path)
    inst = make_install_state(tmp_path, tmp_path / "u1")
    ud = unit_dir(tmp_path)

    env = uninstall_env(tmp_path, fakebin, logs, inst)
    result = run_script("uninstall.sh", ["agent"], env)
    assert result.returncode == 0, result.stderr
    assert not (ud / "opencode-mesh-agent.service").exists()
    assert (ud / "opencode-mesh-agent@win.service").exists()
    agents = read_json(inst / "config" / "agents.json")
    assert set(agents["agents"]) == {"win"}  # default 键被移除，win 保留
    assert (inst / "data" / "agent-state.json").exists()  # 状态保留
    # 共享目录与 src 保留
    assert (inst / "src" / "SENTINEL").read_text() == "keep\n"

    # 卸载最后一个实例：agents.json 应按空 agents 删除，数据仍保留
    result = run_script("uninstall.sh", ["agent", "win"], env)
    assert result.returncode == 0, result.stderr
    assert not (ud / "opencode-mesh-agent@win.service").exists()
    assert read_json(inst / "config" / "agents.json")["agents"] == {}
    assert (inst / "data" / "agent-state-win.json").exists()
    assert inst.exists()


def test_uninstall_default_with_other_instance_keeps_directory(tmp_path):
    """默认实例卸载时其他（inactive）实例存在 → 保留共享目录与配置。"""
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
    """all：全部单元与配置键移除，deregister 每个 agent，默认 KEEP=N 时删除安装目录。"""
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
