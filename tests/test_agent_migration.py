"""迁移保留人工配置原件与设备身份，不操作服务。"""

import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("agent_migration", Path(__file__).parents[1] / "scripts/migrate-agent-config.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_migration_preserves_identity_and_unknown_settings(tmp_path):
    source = tmp_path / "old.json"
    identity = {"device_id": "device-a", "agent_token": "test-token"}
    state = tmp_path / "legacy-state.json"
    state.write_text(json.dumps(identity))
    old = {"gateway_url": "https://mesh.example.com", "enroll_token": "test",
           "state_file": str(state), "opencode_url": "http://localhost:4096", "custom": {"flag": True}}
    source.write_text(json.dumps(old))
    target = tmp_path / "install" / "config" / "agents.json"
    module.migrate(source, target, None)
    assert not target.parent.exists()
    module.migrate(source, target, None, True)
    result = json.loads(target.read_text())
    assert result["agents"]["default"]["custom"] == {"flag": True}
    assert "state_file" not in result["agents"]["default"]
    assert json.loads(source.read_text()) == old
    copied = target.parent.parent / "data" / "agent-state.json"
    assert json.loads(copied.read_text()) == identity
    assert target.stat().st_mode & 0o777 == 0o600
    assert copied.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        module.migrate(source, target, None, True)


def test_conflicting_identity_refuses_before_config_write(tmp_path):
    source = tmp_path / "old.json"
    state = tmp_path / "old-state.json"
    state.write_text('{"device_id":"device-a"}')
    source.write_text(json.dumps({"state_file": str(state)}))
    destination = tmp_path / "data" / "agent-state.json"
    destination.parent.mkdir()
    destination.write_text('{"device_id":"device-b"}')
    target = tmp_path / "config" / "agents.json"
    with pytest.raises(ValueError):
        module.migrate(source, target, None, True)
    assert not target.exists()
    assert json.loads(destination.read_text())["device_id"] == "device-b"


def test_relative_state_requires_explicit_working_directory(tmp_path):
    source = tmp_path / "old.json"
    source.write_text('{"state_file":"state.json"}')
    (tmp_path / "state.json").write_text('{"device_id":"device-a"}')
    target = tmp_path / "config" / "agents.json"
    with pytest.raises(ValueError):
        module.migrate(source, target, None, True)
    module.migrate(source, target, tmp_path, True)
    assert target.exists()
