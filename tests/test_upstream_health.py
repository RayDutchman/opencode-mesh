"""Regression tests for Agent upstream health reporting without network services."""
import asyncio
import base64
import json
import time

import httpx
import pytest

import src.main as main
from src.main import Agent, Gateway, Registry

REAL_ASYNC_CLIENT = httpx.AsyncClient


def make_agent(**cfg):
    return Agent({"opencode_url": "http://agent.test"} | cfg)


def test_health_worker_debounces_failures_recovers_and_keeps_basic_auth_private(monkeypatch):
    async def scenario():
        calls = []
        sent = []
        completed = asyncio.Event()
        responses = [
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(200, json={"version": "2.1.0"}),
        ]

        async def handler(request):
            calls.append(request)
            response = responses.pop(0)
            if not responses:
                completed.set()
            return response

        class Client:
            def __init__(self, **kwargs):
                self.auth = kwargs["auth"]
                self.client = REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

            async def __aenter__(self):
                await self.client.__aenter__()
                return self.client

            async def __aexit__(self, *args):
                return await self.client.__aexit__(*args)

        class Control:
            async def send(self, raw):
                sent.append(json.loads(raw))

        original_sleep = main.asyncio.sleep

        async def fast_sleep(delay):
            await original_sleep(0)

        monkeypatch.setattr(main.httpx, "AsyncClient", Client)
        monkeypatch.setattr(main.asyncio, "sleep", fast_sleep)
        agent = make_agent(opencode_basic_auth={"username": "user", "password": "secret"})
        worker = asyncio.create_task(agent.upstream_health_worker(Control()))
        await asyncio.wait_for(completed.wait(), 1)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        assert [request.url.path for request in calls] == ["/api/info"] * 3
        assert all(request.headers["authorization"] == "Basic dXNlcjpzZWNyZXQ=" for request in calls)
        assert agent.upstream_health == "healthy"
        assert [message["upstream_health"] for message in sent] == ["unhealthy", "healthy"]
        assert all("secret" not in json.dumps(message) for message in sent)

    asyncio.run(scenario())


@pytest.mark.parametrize("response,status", [
    (httpx.Response(401), "auth_failed"),
    (httpx.Response(403), "auth_failed"),
    (httpx.Response(200, content=b"not-json"), "unhealthy"),
    (httpx.Response(200, json={"version": "1.9.0"}), "unhealthy"),
    (httpx.Response(500), "unhealthy"),
])
def test_health_worker_classifies_nonhealthy_responses_after_two_failures(monkeypatch, response, status):
    async def scenario():
        count = 0
        second = asyncio.Event()

        async def handler(request):
            nonlocal count
            count += 1
            if count == 2:
                second.set()
            return response

        class Client:
            def __init__(self, **kwargs):
                self.client = REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

            async def __aenter__(self):
                await self.client.__aenter__()
                return self.client

            async def __aexit__(self, *args):
                return await self.client.__aexit__(*args)

        class Control:
            async def send(self, raw):
                pass

        original_sleep = main.asyncio.sleep

        async def fast_sleep(delay):
            await original_sleep(0)

        monkeypatch.setattr(main.httpx, "AsyncClient", Client)
        monkeypatch.setattr(main.asyncio, "sleep", fast_sleep)
        agent = make_agent()
        worker = asyncio.create_task(agent.upstream_health_worker(Control()))
        await asyncio.wait_for(second.wait(), 1)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        assert agent.upstream_health == status

    asyncio.run(scenario())


def test_registry_health_is_fresh_only_when_control_is_online_and_not_persisted(tmp_path, monkeypatch):
    registry = Registry({"state_file": str(tmp_path / "state.json")})
    clock = [100.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: clock[0])
    ws = object()
    registry.devices["healthy"] = {"device_id": "healthy", "ws": ws, "last_seen": time.time(),
                                   "upstream_health": "healthy", "upstream_checked": 100.0}
    registry.devices["bad-age"] = {"device_id": "bad-age", "ws": object(), "last_seen": time.time(),
                                   "upstream_health": "healthy", "upstream_checked": float("nan")}
    registry.save()
    saved = json.loads((tmp_path / "state.json").read_text())
    assert "ws" not in saved["devices"]["healthy"]
    assert "upstream_health" not in saved["devices"]["healthy"]
    restored = Registry({"state_file": str(tmp_path / "state.json")})
    assert "upstream_health" not in restored.devices["healthy"]
    assert "upstream_checked" not in restored.devices["healthy"]
    public = {item["device_id"]: item for item in registry.public()}
    assert public["healthy"]["online"] is True
    assert public["healthy"]["available"] is True
    assert public["bad-age"]["upstream_health"] == "unknown"
    assert public["bad-age"]["available"] is False
    clock[0] += 31
    assert {item["device_id"]: item for item in registry.public()}["healthy"]["upstream_health"] == "unknown"


def test_gateway_rejects_invalid_health_age_and_reattach_clears_health(tmp_path):
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "state.json"), "auth": {"username": "u", "password": "p"}, "request_timeout": 0.05})
        device = {"device_id": "device", "auth_token": "token", "ws": None}
        gateway.registry.devices["device"] = device

        class Socket:
            async def close(self, code):
                pass

        first = Socket()
        second = Socket()
        await gateway.attach_device(device, first)
        device["upstream_health"] = "healthy"
        device["upstream_checked"] = time.monotonic()
        await gateway.attach_device(device, second)
        assert device["ws"] is second
        assert device["upstream_health"] == "unknown"
        assert "upstream_checked" not in device

    asyncio.run(scenario())


def test_health_worker_uses_a_total_two_second_deadline(monkeypatch):
    async def scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, *args, **kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        class Control:
            async def send(self, raw):
                pass

        monkeypatch.setattr(main.httpx, "AsyncClient", Client)
        agent = make_agent()
        worker = asyncio.create_task(agent.upstream_health_worker(Control()))
        await asyncio.wait_for(started.wait(), 0.1)
        await asyncio.wait_for(cancelled.wait(), 2.2)
        assert agent.upstream_health == "unknown"
        assert agent.upstream_health_completed is not None
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


def test_health_worker_does_not_follow_redirects(monkeypatch):
    async def scenario():
        requested = asyncio.Event()

        async def handler(request):
            if request.url.path == "/api/info":
                requested.set()
                return httpx.Response(302, headers={"location": "/redirected"})
            return httpx.Response(200, json={"version": "2.1.0"})

        class Client:
            def __init__(self, **kwargs):
                self.client = REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

            async def __aenter__(self):
                await self.client.__aenter__()
                return self.client

            async def __aexit__(self, *args):
                return await self.client.__aexit__(*args)

        class Control:
            async def send(self, raw):
                pass

        original_sleep = main.asyncio.sleep

        async def fast_sleep(delay):
            await original_sleep(0)

        monkeypatch.setattr(main.httpx, "AsyncClient", Client)
        monkeypatch.setattr(main.asyncio, "sleep", fast_sleep)
        agent = make_agent()
        worker = asyncio.create_task(agent.upstream_health_worker(Control()))
        await asyncio.wait_for(requested.wait(), 0.2)
        await original_sleep(0.02)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        assert agent.upstream_health != "healthy"

    asyncio.run(scenario())


def test_gateway_ignores_invalid_pong_health_without_closing_control(tmp_path):
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "state.json"), "auth": {"username": "u", "password": "p"}, "request_timeout": 0.05})
        gateway.registry.devices["device"] = {"device_id": "device", "auth_token": "token"}
        messages = asyncio.Queue()

        class Socket:
            headers = {"x-mesh-agent-token": "token"}

            async def accept(self):
                pass

            async def close(self, code):
                pass

            async def receive(self):
                return await messages.get()

        endpoint = next(route.endpoint for route in gateway.app.routes if getattr(route, "path", None) == "/_mesh/agent/{device_id}")
        task = asyncio.create_task(endpoint(Socket(), "device"))
        await messages.put({"type": "websocket.receive", "text": json.dumps({"type": "pong", "upstream_health": [], "upstream_health_age": 0})})
        await messages.put({"type": "websocket.receive", "text": json.dumps({"type": "pong", "upstream_health": "healthy", "upstream_health_age": 0})})
        await asyncio.sleep(0)
        assert gateway.registry.devices["device"]["upstream_health"] == "healthy"
        await messages.put({"type": "websocket.receive", "text": json.dumps({"type": "pong", "upstream_health": "unknown", "upstream_health_age": 0})})
        await messages.put({"type": "websocket.receive", "text": json.dumps({"type": "pong", "upstream_health": "healthy", "upstream_health_age": True})})
        await asyncio.sleep(0)
        assert gateway.registry.devices["device"]["upstream_health"] == "unknown"
        await messages.put({"type": "websocket.receive", "text": json.dumps({"type": "pong", "upstream_health": "healthy", "upstream_health_age": False})})
        await asyncio.sleep(0)
        assert gateway.registry.devices["device"]["upstream_health"] == "unknown"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("basic", [{}, {"password": "secret"}])
def test_health_worker_treats_incomplete_basic_auth_as_no_auth(monkeypatch, basic):
    async def scenario():
        created = asyncio.Event()
        seen_auth = []

        class Client:
            def __init__(self, **kwargs):
                seen_auth.append(kwargs.get("auth"))

            async def __aenter__(self):
                created.set()
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, *args, **kwargs):
                await asyncio.Event().wait()

        class Control:
            async def send(self, raw):
                pass

        monkeypatch.setattr(main.httpx, "AsyncClient", Client)
        worker = asyncio.create_task(make_agent(opencode_basic_auth=basic).upstream_health_worker(Control()))
        await asyncio.wait_for(created.wait(), 0.1)
        assert seen_auth == [None]
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


def test_agent_run_cancels_health_tasks_when_hello_send_fails(monkeypatch, tmp_path):
    async def scenario():
        heartbeat_cancelled = asyncio.Event()
        worker_cancelled = asyncio.Event()
        heartbeat_started = asyncio.Event()
        worker_started = asyncio.Event()

        class Response:
            status_code = 200
            headers = {}
            text = ""

            def raise_for_status(self):
                pass

            def json(self):
                return {"device_id": "device", "agent_token": "token"}

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, *args, **kwargs):
                return Response()

        class Socket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def send(self, raw):
                await heartbeat_started.wait()
                await worker_started.wait()
                raise ConnectionError("hello failed")

            async def close(self, **kwargs):
                pass

        async def heartbeat(ws):
            heartbeat_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                heartbeat_cancelled.set()

        async def worker(ws):
            worker_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                worker_cancelled.set()

        monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: Client())
        monkeypatch.setattr(main.websockets, "connect", lambda *args, **kwargs: Socket())
        agent = Agent({"opencode_url": "http://127.0.0.1:9", "gateway_url": "http://gateway",
                       "allow_insecure_gateway": True, "enroll_token": "enroll",
                       "state_file": str(tmp_path / "agent.json")})
        agent.control_heartbeat = heartbeat
        agent.upstream_health_worker = worker
        run = asyncio.create_task(agent.run())
        await asyncio.wait_for(heartbeat_cancelled.wait(), 0.2)
        await asyncio.wait_for(worker_cancelled.wait(), 0.2)
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)

    asyncio.run(scenario())


def test_agent_ping_returns_current_health_envelope(monkeypatch, tmp_path):
    async def scenario():
        hello = asyncio.Event()
        pong = asyncio.Event()
        incoming = asyncio.Queue()
        sent = []

        class Response:
            status_code = 200
            headers = {}
            text = ""

            def raise_for_status(self):
                pass

            def json(self):
                return {"device_id": "device", "agent_token": "token"}

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, *args, **kwargs):
                return Response()

        class Socket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def send(self, raw):
                message = json.loads(raw)
                sent.append(message)
                if message["type"] == "agent_hello":
                    hello.set()
                if message["type"] == "pong":
                    pong.set()

            def __aiter__(self):
                return self

            async def __anext__(self):
                return json.dumps(await incoming.get())

            async def close(self, **kwargs):
                pass

        async def idle(ws):
            await asyncio.Event().wait()

        monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: Client())
        monkeypatch.setattr(main.websockets, "connect", lambda *args, **kwargs: Socket())
        agent = Agent({"opencode_url": "http://127.0.0.1:9", "gateway_url": "http://gateway",
                       "allow_insecure_gateway": True, "enroll_token": "enroll",
                       "state_file": str(tmp_path / "agent.json")})
        agent.control_heartbeat = idle
        agent.upstream_health_worker = idle
        agent.upstream_health = "healthy"
        agent.upstream_health_completed = time.monotonic()
        run = asyncio.create_task(agent.run())
        await asyncio.wait_for(hello.wait(), 1)
        assert agent.upstream_health == "unknown"
        assert agent.upstream_health_completed is None
        agent.upstream_health = "healthy"
        agent.upstream_health_completed = time.monotonic()
        await incoming.put({"type": "ping"})
        await asyncio.wait_for(pong.wait(), 1)
        reply = next(message for message in sent if message["type"] == "pong")
        assert reply["upstream_health"] == "healthy"
        assert isinstance(reply["upstream_health_age"], float)
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("health", ["unreachable", "auth_failed", "unhealthy"])
def test_known_unhealthy_device_html_navigation_returns_offline_page_without_forwarding(tmp_path, health):
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "state.json"), "auth": {"username": "u", "password": "p"}, "request_timeout": 0.05})
        sent = []

        class DeviceSocket:
            async def send_text(self, raw):
                sent.append(json.loads(raw))

        gateway.registry.devices["device"] = {"device_id": "device", "ws": DeviceSocket(),
                                              "last_seen": time.time(), "upstream_health": health,
                                              "upstream_checked": time.monotonic()}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://test", auth=("u", "p")) as client:
            response = await client.get("/_mesh/device/device/", headers={"accept": "text/html"})
            root_response = await client.get("/", headers={"accept": "text/html"})
        assert response.status_code == 200
        assert response.text == gateway.offline_page("device")
        assert root_response.status_code == 200
        assert root_response.text == gateway.offline_page("device")
        assert not sent

    asyncio.run(scenario())


def test_unhealthy_explicit_server_navigation_does_not_fall_back_to_healthy_device(tmp_path):
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "state.json"), "auth": {"username": "u", "password": "p"}, "request_timeout": 0.05})
        unhealthy_sent = []
        healthy_sent = []

        class Socket:
            def __init__(self, sent):
                self.sent = sent

            async def send_text(self, raw):
                self.sent.append(json.loads(raw))

        gateway.registry.devices["unhealthy"] = {"device_id": "unhealthy", "ws": Socket(unhealthy_sent),
                                                 "last_seen": time.time(), "upstream_health": "unreachable",
                                                 "upstream_checked": time.monotonic()}
        gateway.registry.devices["healthy"] = {"device_id": "healthy", "ws": Socket(healthy_sent),
                                               "last_seen": time.time(), "upstream_health": "healthy",
                                               "upstream_checked": time.monotonic()}
        key = base64.urlsafe_b64encode(b"http://test/_mesh/device/unhealthy").decode().rstrip("=")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://test", auth=("u", "p")) as client:
            response = await client.get(f"/server/{key}/session/session-a", headers={"accept": "text/html"})
        assert response.status_code == 200
        assert response.text == gateway.offline_page("unhealthy")
        assert not unhealthy_sent
        assert not healthy_sent

    asyncio.run(scenario())


def test_home_handoff_can_leave_unhealthy_default_without_changing_preference(tmp_path):
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "state.json"), "default_device": "down",
                           "auth": {"username": "u", "password": "p"}})
        sent = []

        class Socket:
            async def send_text(self, raw):
                item = json.loads(raw)
                sent.append(item)
                gateway.pending[item["id"]].set_result({"status": 200, "headers": {"content-type": "text/plain"},
                                                        "body": base64.b64encode(b"selected page").decode()})

        for name, health in [("down", "unreachable"), ("up", "healthy")]:
            gateway.registry.devices[name] = {"device_id": name, "ws": Socket(), "last_seen": time.time(),
                                               "upstream_health": health, "upstream_checked": time.monotonic()}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://test", auth=("u", "p")) as client:
            response = await client.get("/?mesh_device=up", headers={"accept": "text/html"})
            unavailable = await client.get("/?mesh_device=down", headers={"accept": "text/html"})
        assert response.text == "selected page"
        assert unavailable.text == gateway.offline_page("down")
        assert len(sent) == 1 and sent[0]["path"] == "/"
        assert gateway.cfg["default_device"] == "down"

    asyncio.run(scenario())


def test_known_unhealthy_device_keeps_api_and_post_proxy_semantics(tmp_path):
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "state.json"), "auth": {"username": "u", "password": "p"}})
        sent = []

        class DeviceSocket:
            async def send_text(self, raw):
                item = json.loads(raw)
                sent.append(item)
                gateway.pending[item["id"]].set_result({"type": "response", "id": item["id"], "status": 200,
                                                         "headers": {"content-type": "application/json"}, "body": "e30="})

        gateway.registry.devices["device"] = {"device_id": "device", "ws": DeviceSocket(),
                                              "last_seen": time.time(), "upstream_health": "unreachable",
                                              "upstream_checked": time.monotonic()}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://test", auth=("u", "p")) as client:
            api = await client.get("/_mesh/device/device/api/info", headers={"accept": "text/html"})
            post = await client.post("/_mesh/device/device/session", content=b"{}")
        assert api.status_code == 200 and api.json() == {}
        assert post.status_code == 200 and post.json() == {}
        assert [item["path"] for item in sent] == ["/api/info", "/session"]

    asyncio.run(scenario())
