"""同机实例注册保持独立身份，显示名不参与身份计算。"""

import asyncio
import json

import pytest

from src.main import Agent


def test_shared_config_resolves_instances_without_mutating_source(tmp_path):
    from src.main import resolve_agent_config
    path = tmp_path / "config" / "agents.json"
    cfg = {"gateway_url": "https://mesh.example.com", "enroll_token": "shared",
           "agents": {"default": {"opencode_url": "http://localhost:4096"},
                      "windows": {"opencode_url": "http://localhost:4097", "device_name": "Device B"}}}
    original = json.dumps(cfg)
    first = resolve_agent_config(path, cfg, "default")
    second = resolve_agent_config(path, cfg, "windows")
    assert first["state_file"] == str(tmp_path / "data" / "agent-state.json")
    assert second["state_file"] == str(tmp_path / "data" / "agent-state-windows.json")
    assert second["opencode_url"] == "http://localhost:4097"
    assert second["enroll_token"] == "shared"
    assert "agents" not in second
    assert json.dumps(cfg) == original


@pytest.mark.parametrize("instance", ["", "../escape", "a/b", "a.b", "missing"])
def test_shared_config_rejects_invalid_or_missing_instance(tmp_path, instance):
    from src.main import resolve_agent_config
    with pytest.raises(ValueError):
        resolve_agent_config(tmp_path / "config" / "agents.json", {"agents": {"default": {}}}, instance)


def test_legacy_config_stays_compatible_but_cannot_be_reused_for_named_agent(tmp_path):
    from src.main import resolve_agent_config
    cfg = {"opencode_url": "http://localhost:4096", "state_file": "data/old.json"}
    assert resolve_agent_config(tmp_path / "agent.json", cfg, None) == cfg
    with pytest.raises(ValueError):
        resolve_agent_config(tmp_path / "agent.json", cfg, "windows")


@pytest.mark.parametrize("common", [True, False])
def test_shared_config_rejects_user_supplied_state_path(tmp_path, common):
    from src.main import resolve_agent_config
    cfg = {"agents": {"default": {"opencode_url": "http://localhost:4096"}}}
    (cfg if common else cfg["agents"]["default"])["state_file"] = "shared.json"
    with pytest.raises(ValueError):
        resolve_agent_config(tmp_path / "config" / "agents.json", cfg, "default")


@pytest.mark.parametrize("name", ["Device A", None])
def test_agent_registration_uses_configured_name_and_preserves_identity(tmp_path, monkeypatch, name):
    captured = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json):
            captured.append(json)
            raise asyncio.CancelledError()

    monkeypatch.setattr("src.main.httpx.AsyncClient", Client)
    monkeypatch.setattr("src.main.hostname", lambda: "host-default")
    states = [tmp_path / "first.json", tmp_path / "second.json"]
    for state in [*states, states[0]]:
        cfg = {"gateway_url": "https://mesh.example.com", "enroll_token": "test",
               "state_file": str(state), "opencode_url": "http://localhost:4096"}
        if name is not None:
            cfg["device_name"] = name
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(Agent(cfg).run())
    assert all(item["name"] == (name or "host-default") for item in captured)
    assert captured[0]["device_id"] != captured[1]["device_id"]
    assert captured[0]["device_id"] == captured[2]["device_id"]
    assert json.loads(states[0].read_text())["device_id"] == captured[0]["device_id"]
