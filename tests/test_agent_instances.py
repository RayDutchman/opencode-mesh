"""Same-host instances keep distinct identities; display names never participate in identity computation."""

import asyncio
import json
import sys

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


def write_shared_config(tmp_path, cfg):
    path = tmp_path / "config" / "agents.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg))
    return path


def test_per_instance_gateway_override_preserves_shared_config(tmp_path):
    """An instance may name its own Gateway and enrollment token; the shared values stay defaults."""
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


def test_cli_defaults_to_supervising_all_instances(tmp_path, monkeypatch):
    """The public Agent command launches the supervisor rather than selecting default."""
    from src.main import main
    path = write_shared_config(tmp_path, {"agents": {"default": {}, "beta": {}}})
    captured = []

    async def capture(config_path, instances):
        captured.append((config_path, instances))

    monkeypatch.setattr("src.main._supervise_until_stopped", capture)
    monkeypatch.setattr(sys, "argv", ["src.main", "--mode", "agent", "--config", str(path)])
    main()
    assert captured == [(str(path), ["default", "beta"])]


def test_worker_selection_is_not_a_public_cli_option():
    from src.main import build_parser
    help_text = build_parser().format_help()
    assert "--agent-instance" not in help_text
    assert "--all-instances" not in help_text
    assert "--instance" not in help_text


@pytest.mark.parametrize("obsolete", [["--instance", "default"], ["--all-instances"]])
def test_cli_rejects_removed_service_options(tmp_path, obsolete):
    """Old service units need explicit migration, not a silently changed meaning."""
    from src.main import build_parser
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(
            ["--mode", "agent", "--config", str(tmp_path / "agents.json"),
             *obsolete])
    assert excinfo.value.code == 2


def test_internal_worker_is_rejected_in_gateway_mode(tmp_path, monkeypatch):
    """The Gateway serves devices and never supervises Agents, so the flag is refused there."""
    from src.main import main
    path = write_shared_config(tmp_path, {
        "gateway_url": "https://mesh-a.example.com", "enroll_token": "token-a",
        "agents": {"default": {"opencode_url": "http://127.0.0.1:4096"}}})

    def refuse(*args, **kwargs):
        raise AssertionError("the Gateway must not accept an Agent worker selector")

    monkeypatch.setattr("src.main.uvicorn.run", refuse)
    monkeypatch.setattr(sys, "argv", ["src.main", "--mode", "gateway",
                                       "--config", str(path), "--agent-instance", "default"])
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 2
