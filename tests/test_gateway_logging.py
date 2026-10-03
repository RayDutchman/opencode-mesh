"""Gateway access/error logging configuration behavior.

Uvicorn's own switches are all-or-nothing: ``access_log=False`` empties every
access handler, and ``log_level`` moves ``uvicorn.error``, ``uvicorn.access``
and ``uvicorn.asgi`` together, so neither can keep errors while dropping
successful request lines. These tests lock the Mesh behavior layered on top:

- a status-threshold filter that keeps error responses and drops successful
  ones, installed on the ``uvicorn.access`` handlers only;
- the configuration keys that select it, defaulting to today's full access log;
- ``log_level`` validation, because uvicorn raises ``KeyError`` on values it
  does not know (``uvicorn/config.py``: ``LOG_LEVELS[self.log_level.lower()]``).

Records are built in the exact shape uvicorn emits: five ``args`` for HTTP
(``protocols/http/h11_impl.py``) and two for a WebSocket accept
(``protocols/websockets/websockets_sansio_impl.py``), which carries no status
code at all and must therefore survive any threshold.
"""

import asyncio
import io
import json
import logging
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn

import src.main as mesh
from src.main import Gateway, main


# ---------- helpers ----------

@contextmanager
def isolated_uvicorn_loggers():
    """Attach capturing handlers to both uvicorn loggers and restore them after."""
    access_stream, error_stream = io.StringIO(), io.StringIO()
    saved = {}
    for name, stream in (("uvicorn.access", access_stream), ("uvicorn.error", error_stream)):
        logger = logging.getLogger(name)
        saved[name] = (list(logger.handlers), list(logger.filters), logger.level,
                       logger.propagate, logger.disabled)
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        logger.handlers = [handler]
        logger.filters = []
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.disabled = False
    try:
        yield access_stream, error_stream
    finally:
        for name, (handlers, filters, level, propagate, disabled) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers = handlers
            logger.filters = filters
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled


def access_record(status, path="/_mesh/devices", method="GET", logger="uvicorn.access"):
    """The LogRecord uvicorn's access logger emits for a finished HTTP request."""
    return logging.LogRecord(logger, logging.INFO, __file__, 477,
                             '%s - "%s %s HTTP/%s" %d',
                             ("192.0.2.10", method, path, "1.1", status), None)


def websocket_accept_record(path="/_mesh/ws/device-a"):
    """The LogRecord uvicorn emits for a WebSocket accept; it carries no status code."""
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 440,
                             '%s - "WebSocket %s" [accepted]', ("192.0.2.10", path), None)


def build_gateway(directory, **extra):
    """A real Gateway on a throwaway state file, following the test_auth_boundaries pattern."""
    cfg = {"state_file": str(Path(directory) / "registry.json"),
           "auth": {"username": "test", "password": "test-pass"},
           "enroll_token": "test-enrollment"}
    cfg.update(extra)
    return Gateway(cfg)


async def run_lifespan(gateway):
    async with gateway.lifespan(gateway.app):
        pass


def handler_filters(logger_name="uvicorn.access"):
    return [f for handler in logging.getLogger(logger_name).handlers for f in handler.filters]


# ---------- status threshold filter ----------

def test_status_filter_keeps_errors_and_rejects_successful_responses():
    keep = mesh.AccessStatusFilter(400)
    for status in (400, 401, 404, 500, 503):
        assert keep.filter(access_record(status)) is True, status
    for status in (101, 200, 201, 204, 302):
        assert keep.filter(access_record(status)) is False, status


def test_status_filter_keeps_records_without_a_status_code():
    """A WebSocket accept line has no status; a threshold must not silently drop it."""
    keep = mesh.AccessStatusFilter(400)
    assert keep.filter(websocket_accept_record()) is True
    assert keep.filter(logging.LogRecord("uvicorn.access", logging.INFO, __file__, 477,
                                         "started server process", None, None)) is True


def test_status_filter_honours_a_configured_threshold():
    keep = mesh.AccessStatusFilter(500)
    assert keep.filter(access_record(404)) is False
    assert keep.filter(access_record(500)) is True
    assert mesh.AccessStatusFilter(400).min_status == 400


def test_status_filter_rejects_everything_below_a_high_threshold():
    keep = mesh.AccessStatusFilter(600)
    assert keep.filter(access_record(500)) is False
    assert keep.filter(access_record(599)) is False


# ---------- installation onto uvicorn.access only ----------

def test_lifespan_installs_the_filter_on_access_handlers_only():
    with isolated_uvicorn_loggers():
        gateway = build_gateway("/tmp", access_log_status_min=400)
        asyncio.run(run_lifespan(gateway))
        assert handler_filters("uvicorn.access")
        assert handler_filters("uvicorn.error") == []


def test_lifespan_filter_drops_successful_access_lines_and_keeps_errors():
    with isolated_uvicorn_loggers() as (access_stream, error_stream):
        gateway = build_gateway("/tmp", access_log_status_min=400)
        asyncio.run(run_lifespan(gateway))
        access = logging.getLogger("uvicorn.access")
        access.info('%s - "%s %s HTTP/%s" %d', "192.0.2.10", "GET", "/_mesh/devices", "1.1", 200)
        access.info('%s - "%s %s HTTP/%s" %d', "192.0.2.10", "GET", "/_mesh/boom", "1.1", 500)
        access.info('%s - "WebSocket %s" [accepted]', "192.0.2.10", "/_mesh/ws/device-a")
        logging.getLogger("uvicorn.error").error("gateway failed")
        out = access_stream.getvalue()
        assert "/_mesh/devices" not in out
        assert "/_mesh/boom" in out and "500" in out
        assert "WebSocket" in out
        assert "gateway failed" in error_stream.getvalue()
        assert "gateway failed" not in out


def test_default_configuration_keeps_every_access_line():
    """Without the key the Gateway must behave exactly as before: no filter at all."""
    with isolated_uvicorn_loggers() as (access_stream, _):
        gateway = build_gateway("/tmp")
        asyncio.run(run_lifespan(gateway))
        assert handler_filters("uvicorn.access") == []
        access = logging.getLogger("uvicorn.access")
        access.info('%s - "%s %s HTTP/%s" %d', "192.0.2.10", "GET", "/_mesh/devices", "1.1", 200)
        assert '"GET /_mesh/devices HTTP/1.1" 200' in access_stream.getvalue()


def test_repeated_lifespans_do_not_stack_filters():
    with isolated_uvicorn_loggers():
        gateway = build_gateway("/tmp", access_log_status_min=400)
        asyncio.run(run_lifespan(gateway))
        asyncio.run(run_lifespan(gateway))
        assert len(handler_filters("uvicorn.access")) == 1


# ---------- configuration keys ----------

def test_access_log_status_min_is_unset_by_default_and_read_when_configured():
    assert mesh.access_log_status_min({}) is None
    assert mesh.access_log_status_min({"access_log_status_min": 500}) == 500
    assert mesh.access_log_status_min({"access_log_status_min": "400"}) == 400


def test_access_log_status_min_survives_an_invalid_value():
    """A typo must not take the Gateway down at startup; it falls back to no filtering."""
    assert mesh.access_log_status_min({"access_log_status_min": "verbose"}) is None
    assert mesh.access_log_status_min({"access_log_status_min": None}) is None
    assert mesh.access_log_status_min({"access_log_status_min": -1}) is None


def test_gateway_server_options_switch_for_the_access_log():
    assert mesh.gateway_server_options({})["access_log"] is True
    assert mesh.gateway_server_options({"access_log": True})["access_log"] is True
    assert mesh.gateway_server_options({"access_log": False})["access_log"] is False
    assert mesh.gateway_server_options({"access_log": 0})["access_log"] is False


def test_gateway_server_options_keeps_the_existing_uvicorn_settings():
    options = mesh.gateway_server_options({"listen_host": "0.0.0.0", "listen_port": 9100,
                                           "max_request_bytes": 1048576})
    assert options["host"] == "0.0.0.0"
    assert options["port"] == 9100
    assert options["timeout_graceful_shutdown"] == 5
    assert options["ws_max_size"] >= 1048576 * 2


# ---------- log level ----------

@pytest.mark.parametrize("level", ["critical", "error", "warning", "info", "debug", "trace"])
def test_resolve_log_level_accepts_every_uvicorn_level(level):
    assert mesh.resolve_log_level({"log_level": level}) == level


def test_resolve_log_level_normalizes_case_and_defaults_to_info():
    assert mesh.resolve_log_level({"log_level": "WARNING"}) == "warning"
    assert mesh.resolve_log_level({"log_level": "  Info "}) == "info"
    assert mesh.resolve_log_level({}) == "info"


@pytest.mark.parametrize("value", ["verbose", "", "off", "notice", 30, None, True, ["info"]])
def test_resolve_log_level_falls_back_to_info_for_unusable_values(value):
    """uvicorn indexes LOG_LEVELS[level] and raises KeyError; Mesh must not crash."""
    assert mesh.resolve_log_level({"log_level": value}) == "info"


def test_gateway_server_options_exposes_the_validated_log_level():
    assert mesh.gateway_server_options({"log_level": "debug"})["log_level"] == "debug"
    assert mesh.gateway_server_options({"log_level": "shout"})["log_level"] == "info"
    assert mesh.gateway_server_options({})["log_level"] == "info"


# ---------- the real entry point ----------

def test_main_hands_the_resolved_logging_options_to_uvicorn(monkeypatch, tmp_path):
    captured = {}

    def fake_run(app, **kwargs):
        captured.update(kwargs)
        captured["app"] = app

    monkeypatch.setattr(uvicorn, "run", fake_run)
    config = tmp_path / "gateway.json"
    config.write_text(json.dumps({"state_file": str(tmp_path / "state.json"),
                                  "auth": {"username": "test", "password": "test-pass"},
                                  "enroll_token": "test-enrollment",
                                  "access_log": False, "log_level": "warning"}),
                      encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["src.main", "--mode", "gateway", "--config", str(config)])
    main()
    assert captured["access_log"] is False
    assert captured["log_level"] == "warning"
    assert captured["app"].title == "OpenCode Mesh Gateway"


def test_main_defaults_match_the_previous_hardcoded_startup(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
    config = tmp_path / "gateway.json"
    config.write_text(json.dumps({"state_file": str(tmp_path / "state.json")}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["src.main", "--mode", "gateway", "--config", str(config)])
    main()
    assert captured["access_log"] is True
    assert captured["log_level"] == "info"
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8090
