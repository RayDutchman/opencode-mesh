"""Regression tests for bounded review findings without network services."""
import asyncio
import base64
import json
import time

import httpx
import pytest
import src.main as main
from src.main import Agent, Gateway, filter_response_headers
import src.p2p as p2p
from src.p2p import ChunkAssembler, frame


def make_agent(**cfg):
    return Agent({"opencode_url": "http://127.0.0.1:9"} | cfg)


def test_response_filter_removes_connection_named_hop_headers():
    headers = filter_response_headers({
        "Content-Type": "text/plain",
        "Connection": "X-Hop, keep-alive",
        "X-Hop": "private",
        "Keep-Alive": "timeout=5",
    })
    assert headers == {"Content-Type": "text/plain"}


def test_completed_assembly_releases_payload_budget_before_slow_dispatch():
    assembler = ChunkAssembler(limit=16, budget=16, max_assemblies=2)
    assert assembler.feed(frame("first", 0, b"x" * 16, True)) == "accepted"
    assert assembler.result("first") == b"x" * 16
    assembler.complete("first")
    assert assembler.feed(frame("second", 0, b"y" * 16, False)) is None


def test_completed_ids_have_a_separate_finite_replay_window():
    assembler = ChunkAssembler(limit=16, max_assemblies=1, completed_max=2, ttl=300)
    for message_id in ("first", "second"):
        assert assembler.feed(frame(message_id, 0, b"x", True)) == "accepted"
        assembler.complete(message_id)
    # Active-assembly capacity must not evict recently completed IDs.
    assert assembler.feed(frame("first", 0, b"x", True)) == "error"
    assert assembler.feed(frame("third", 0, b"x", True)) == "accepted"
    assembler.complete("third")
    # The bounded window deliberately expires its oldest ID rather than claiming exactly-once forever.
    assert assembler.feed(frame("first", 0, b"x", True)) == "accepted"


def test_framed_request_releases_assembly_budget_before_slow_upstream_response():
    async def scenario():
        agent = make_agent(max_p2p_message_bytes=16, max_p2p_total_bytes=16)
        started = asyncio.Event()
        release = asyncio.Event()

        class Channel:
            readyState = "open"
            bufferedAmount = 0

            def send(self, raw):
                pass

        async def local_request(item, timeout=120):
            started.set()
            await release.wait()
            return {"type": "response", "id": item["id"], "status": 200,
                    "headers": {}, "body": ""}

        agent.local_request = local_request
        channel = Channel()
        payload = json.dumps({"type": "request", "id": "slow", "method": "POST",
                              "path": "/", "headers": {}, "body": ""}).encode()
        # The small configured budget is insufficient for this JSON payload, so
        # use the real assembler directly with a matching test-sized budget.
        agent.p2p_assemblers[id(channel)] = ChunkAssembler(limit=len(payload), budget=len(payload))
        task = asyncio.create_task(agent.p2p_message(channel, frame("slow", 0, payload, True)))
        await asyncio.wait_for(started.wait(), 1)
        assert agent.p2p_assemblers[id(channel)].total_of("slow") == 0
        release.set()
        await task

    asyncio.run(scenario())


def test_peer_capacity_is_reserved_before_answer_offer(monkeypatch):
    async def scenario():
        entered = 0
        both_entered = asyncio.Event()
        release = asyncio.Event()

        class Peer:
            async def close(self):
                pass

        async def answer_offer(*args, **kwargs):
            nonlocal entered
            entered += 1
            if entered == 2:
                both_entered.set()
            await release.wait()
            return Peer(), {"type": "answer", "sdp": "test"}

        class Control:
            async def send(self, payload):
                pass

        monkeypatch.setattr(main, "answer_offer", answer_offer)
        agent = make_agent(max_p2p_peers=1)
        first = asyncio.create_task(agent.handle_p2p_offer({"id": "one", "offer": {}}, Control()))
        await asyncio.sleep(0)
        second = asyncio.create_task(agent.handle_p2p_offer({"id": "two", "offer": {}}, Control()))
        await asyncio.sleep(0)
        # Only the admitted offer may enter the negotiation barrier.
        assert entered == 1
        release.set()
        await asyncio.gather(first, second)
        assert len(agent.p2p_peers) == 1

    asyncio.run(scenario())


def test_p2p_same_request_id_is_single_flight_and_cancelled_once():
    async def scenario():
        agent = make_agent()
        started = 0
        release = asyncio.Event()
        sent = []

        class Channel:
            readyState = "open"
            bufferedAmount = 0

            def send(self, raw):
                sent.append(json.loads(raw))

        async def local_request(item, timeout=120):
            nonlocal started
            started += 1
            await release.wait()
            return {"type": "response", "id": item["id"], "status": 200,
                    "headers": {}, "body": base64.b64encode(b"ok").decode()}

        agent.local_request = local_request
        channel = Channel()
        request = {"type": "request", "id": "same", "method": "POST", "path": "/mutation",
                   "headers": {}, "body": ""}
        first = asyncio.create_task(agent.p2p_message(channel, request))
        await asyncio.sleep(0)
        second = asyncio.create_task(agent.p2p_message(channel, dict(request)))
        await asyncio.sleep(0)
        assert started == 1
        await agent.p2p_message(channel, {"type": "cancel", "id": "same"})
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        assert started == 1
        assert [message.get("type") for message in sent].count("cancelled") == 1

    asyncio.run(scenario())


def test_agent_ws_queue_rejects_overflow_without_waiting():
    agent = make_agent(ws_bridge_queue_frames=1, ws_bridge_queue_bytes=8)
    agent.ws_queues["bridge"] = asyncio.Queue(maxsize=1)
    assert agent.enqueue_ws_message("bridge", {"type": "ws_data", "data": "1234"})
    assert not agent.enqueue_ws_message("bridge", {"type": "ws_data", "data": "5678"})


def test_gateway_ws_queue_rejects_overflow_without_sender_tasks(tmp_path):
    gateway = Gateway({"state_file": str(tmp_path / "state.json"),
                       "ws_bridge_queue_frames": 1, "ws_bridge_queue_bytes": 8})
    assert gateway.enqueue_browser_message("bridge", "1234", False)
    assert not gateway.enqueue_browser_message("bridge", "5678", False)


def test_gateway_proxy_strips_connection_named_headers_from_actual_asgi_response(tmp_path):
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "state.json"),
                           "auth": {"username": "user", "password": "pass"}})

        class DeviceSocket:
            async def send_text(self, raw):
                request = json.loads(raw)
                gateway.pending[request["id"]].set_result({
                    "type": "response", "id": request["id"], "status": 200,
                    "headers": {"Content-Type": "text/plain", "Connection": "X-Hop",
                                "X-Hop": "private", "Keep-Alive": "timeout=5"},
                    "body": base64.b64encode(b"ok").decode(),
                })

        socket = DeviceSocket()
        gateway.registry.devices["device"] = {"device_id": "device", "ws": socket,
                                              "last_seen": time.time()}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),
                                     base_url="http://test", auth=("user", "pass")) as client:
            response = await client.get("/_mesh/device/device/api/info")
        assert response.status_code == 200
        assert response.text == "ok"
        assert "x-hop" not in response.headers
        assert "keep-alive" not in response.headers

    asyncio.run(scenario())


def test_gateway_inflight_send_counts_against_bridge_byte_budget(tmp_path):
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "state.json"),
                           "ws_bridge_queue_frames": 2, "ws_bridge_queue_bytes": 8})
        entered = asyncio.Event()
        release = asyncio.Event()

        class Bridge:
            async def send_text(self, payload):
                entered.set()
                await release.wait()

        gateway.browser_ws["bridge"] = Bridge()
        assert gateway.enqueue_browser_message("bridge", "12345678", False)
        await asyncio.wait_for(entered.wait(), 1)
        assert not gateway.enqueue_browser_message("bridge", "abcdefgh", False)
        release.set()
        sender = gateway.browser_sender_tasks["bridge"]
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)

    asyncio.run(scenario())


def test_agent_inflight_send_counts_against_bridge_byte_budget(monkeypatch):
    async def scenario():
        agent = make_agent(ws_bridge_queue_frames=2, ws_bridge_queue_bytes=8)
        entered = asyncio.Event()
        release = asyncio.Event()

        class Target:
            subprotocol = None
            close_code = 1000

            async def send(self, payload):
                entered.set()
                await release.wait()

            async def close(self, **kwargs):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.Event().wait()

        class Connect:
            async def __aenter__(self):
                return Target()

            async def __aexit__(self, *args):
                return False

        class Control:
            async def send(self, payload):
                pass

        monkeypatch.setattr(main.websockets, "connect", lambda *args, **kwargs: Connect())
        task = asyncio.create_task(agent.local_ws({"id": "bridge", "path": "/", "headers": {}}, Control()))
        while "bridge" not in agent.ws_queues:
            await asyncio.sleep(0)
        assert agent.enqueue_ws_message("bridge", {"type": "ws_data", "kind": "text", "data": "12345678"})
        await asyncio.wait_for(entered.wait(), 1)
        assert not agent.enqueue_ws_message("bridge", {"type": "ws_data", "kind": "text", "data": "abcdefgh"})
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_gateway_overflow_schedules_one_close_for_many_late_frames(tmp_path, monkeypatch):
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "state.json"),
                           "ws_bridge_queue_frames": 1, "ws_bridge_queue_bytes": 4})
        assert gateway.enqueue_browser_message("bridge", "1234", False)
        gateway.browser_ws["bridge"] = object()
        scheduled = []
        monkeypatch.setattr(asyncio, "create_task", lambda coroutine: scheduled.append(coroutine))
        for _ in range(100):
            if not gateway.enqueue_browser_message("bridge", "5678", False):
                gateway.schedule_browser_overflow_close("bridge")
        assert len(scheduled) == 1
        for coroutine in scheduled:
            coroutine.close()

    asyncio.run(scenario())


def test_agent_control_overflow_schedules_one_error_for_many_late_frames():
    agent = make_agent(ws_bridge_queue_frames=1, ws_bridge_queue_bytes=4)
    agent.ws_queues["bridge"] = asyncio.Queue(maxsize=1)
    assert agent.enqueue_ws_message("bridge", {"type": "ws_data", "data": "1234"})
    assert agent.claim_ws_overflow("bridge")
    for _ in range(100):
        assert not agent.enqueue_ws_message("bridge", {"type": "ws_data", "data": "5678"})
        assert not agent.claim_ws_overflow("bridge")


def test_gateway_sender_failure_ends_worker_and_cleans_bridge_state(tmp_path):
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "state.json")})
        sent = []

        class BrokenBridge:
            async def send_text(self, payload):
                raise RuntimeError("browser disconnected")

            async def close(self, code):
                pass

        class Owner:
            async def send_text(self, raw):
                sent.append(json.loads(raw))

        gateway.browser_ws["bridge"] = BrokenBridge()
        gateway.owners["bridge"] = Owner()
        gateway.browser_send_locks["bridge"] = asyncio.Lock()
        assert gateway.enqueue_browser_message("bridge", "data", False)
        sender = gateway.browser_sender_tasks["bridge"]
        await asyncio.wait_for(sender, 1)
        assert "bridge" not in gateway.browser_sender_tasks
        assert "bridge" not in gateway.browser_queues
        assert "bridge" not in gateway.browser_queue_bytes
        assert "bridge" not in gateway.browser_ws
        assert "bridge" not in gateway.owners
        assert [message["type"] for message in sent] == ["ws_close"]

    asyncio.run(scenario())


def test_gateway_cancels_unanswered_p2p_offer_when_http_task_is_cancelled(tmp_path):
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "state.json"),
                           "auth": {"username": "u", "password": "p"}})
        sent = []
        offer_sent = asyncio.Event()

        class DeviceSocket:
            async def send_text(self, raw):
                sent.append(json.loads(raw))
                if sent[-1]["type"] == "p2p_offer":
                    offer_sent.set()

        gateway.registry.devices["device"] = {"device_id": "device", "ws": DeviceSocket(),
                                              "last_seen": time.time()}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),
                                     base_url="http://test", auth=("u", "p")) as client:
            task = asyncio.create_task(client.post("/_mesh/p2p/offer", json={
                "device_id": "device", "type": "offer", "sdp": "test"}))
            while not sent:
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert [item["type"] for item in sent] == ["p2p_offer", "cancel"]
        assert sent[0]["id"] == sent[1]["id"]
        assert sent[0]["id"].startswith("p2p-")

    asyncio.run(scenario())


def test_agent_offer_cancel_closes_returned_peer_and_releases_reservation(monkeypatch):
    async def scenario():
        agent = make_agent()
        entered = asyncio.Event()
        release = asyncio.Event()
        closed = []

        class Peer:
            async def close(self):
                closed.append(True)

        async def answer_offer(*args, **kwargs):
            entered.set()
            await release.wait()
            return Peer(), {"type": "answer", "sdp": "test"}

        class Control:
            async def send(self, raw):
                pass

        monkeypatch.setattr(main, "answer_offer", answer_offer)
        offer = {"type": "p2p_offer", "id": "p2p-test", "offer": {}}
        task = asyncio.create_task(agent.handle_p2p_offer(offer, Control()))
        await entered.wait()
        release.set()
        await task
        assert "p2p-test" in agent.p2p_sessions
        await agent.cancel_p2p_offer("p2p-test")
        assert closed == [True]
        assert not agent.p2p_sessions
        assert agent.p2p_reservations == 0

    asyncio.run(scenario())


def test_control_tombstones_are_bounded_and_expire(monkeypatch):
    agent = make_agent()
    clock = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: clock[0])
    for index in range(257):
        agent.remember_control_id(f"id-{index}")
    assert not agent.is_completed_control_id("id-0")
    assert agent.is_completed_control_id("id-256")
    clock[0] += 301
    assert not agent.is_completed_control_id("id-256")


def test_gateway_asgi_disconnect_after_offer_body_cancels_and_cleans_maps(tmp_path):
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "state.json"),
                           "auth": {"username": "u", "password": "p"}})
        sent = []
        offer_sent = asyncio.Event()

        class DeviceSocket:
            async def send_text(self, raw):
                sent.append(json.loads(raw))
                if sent[-1]["type"] == "p2p_offer":
                    offer_sent.set()

        gateway.registry.devices["device"] = {"device_id": "device", "ws": DeviceSocket(),
                                              "last_seen": time.time()}
        body = json.dumps({"device_id": "device", "type": "offer", "sdp": "test"}).encode()
        receive_calls = 0

        async def receive():
            nonlocal receive_calls
            receive_calls += 1
            if receive_calls == 1:
                return {"type": "http.request", "body": body, "more_body": False}
            await offer_sent.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": "/_mesh/p2p/offer",
                 "raw_path": b"/_mesh/p2p/offer", "query_string": b"",
                 "headers": [(b"authorization", b"Basic dTpw")],
                 "client": ("127.0.0.1", 1234), "server": ("test", 80)}
        started = time.monotonic()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await asyncio.wait_for(gateway.app(scope, receive, send), 1)
        assert time.monotonic() - started < 0.5
        assert [item["type"] for item in sent] == ["p2p_offer", "cancel"]
        assert sent[0]["id"] == sent[1]["id"]
        assert not gateway.p2p_answers
        assert not gateway.p2p_owners

    asyncio.run(scenario())


def test_offer_cancel_closes_session_when_negotiation_task_has_finished(monkeypatch):
    async def scenario():
        agent = make_agent()
        closed = []

        class Peer:
            async def close(self):
                closed.append(True)

        async def answer_offer(*args, **kwargs):
            return Peer(), {"type": "answer", "sdp": "test"}

        class Control:
            async def send(self, raw):
                pass

        monkeypatch.setattr(main, "answer_offer", answer_offer)
        await agent.handle_p2p_offer({"id": "p2p-finished", "offer": {}}, Control())
        assert "p2p-finished" in agent.p2p_sessions
        await agent.cancel_p2p_offer("p2p-finished")
        assert closed == [True]
        assert "p2p-finished" not in agent.p2p_sessions
        assert "p2p-finished" not in agent.p2p_session_watchdogs

    asyncio.run(scenario())


def test_offer_send_failure_after_peer_registration_closes_peer_and_watchdog(monkeypatch):
    async def scenario():
        agent = make_agent()
        closed = []

        class Peer:
            async def close(self):
                closed.append(True)

        async def answer_offer(*args, **kwargs):
            return Peer(), {"type": "answer", "sdp": "test"}

        class BrokenControl:
            async def send(self, raw):
                raise ConnectionError("closed")

        monkeypatch.setattr(main, "answer_offer", answer_offer)
        await agent.handle_p2p_offer({"id": "p2p-broken", "offer": {}}, BrokenControl())
        assert closed == [True]
        assert not agent.p2p_sessions
        assert not agent.p2p_session_watchdogs
        assert not agent.p2p_peers

    asyncio.run(scenario())


def test_answer_offer_closes_created_peer_when_cancelled(monkeypatch):
    async def scenario():
        closed = []
        handlers = {}

        class Peer:
            connectionState = "new"
            iceGatheringState = "complete"
            localDescription = type("Description", (), {"type": "answer", "sdp": "answer"})()

            def __init__(self, configuration=None):
                pass

            def on(self, name):
                def register(callback):
                    handlers[name] = callback
                    return callback
                return register

            async def setRemoteDescription(self, description):
                raise asyncio.CancelledError()

            async def createAnswer(self):
                raise AssertionError("must not create answer after cancellation")

            async def setLocalDescription(self, answer):
                pass

            async def close(self):
                closed.append(True)

        monkeypatch.setattr(p2p, "load_aiortc", lambda: (Peer, lambda **kwargs: kwargs))
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await p2p.answer_offer({"type": "offer", "sdp": "offer"}, lambda *_: None,
                                   lambda: asyncio.sleep(0), loopback_candidate=False)
        assert closed == [True]

    asyncio.run(scenario())


def test_answer_offer_marks_already_open_datachannel_ready(monkeypatch):
    async def scenario():
        peer_handlers = {}
        channel_handlers = {}
        ready = asyncio.Event()

        class Channel:
            label = "mesh"
            readyState = "open"

            def on(self, name):
                def register(callback):
                    channel_handlers[name] = callback
                    return callback
                return register

        class Peer:
            connectionState = "new"
            iceGatheringState = "complete"
            localDescription = type("Description", (), {"type": "answer", "sdp": "answer"})()

            def __init__(self, configuration=None):
                pass

            def on(self, name):
                def register(callback):
                    peer_handlers[name] = callback
                    return callback
                return register

            async def setRemoteDescription(self, description):
                peer_handlers["datachannel"](Channel())

            async def createAnswer(self):
                return object()

            async def setLocalDescription(self, answer):
                pass

            async def close(self):
                pass

        async def mark_ready():
            ready.set()

        monkeypatch.setattr(p2p, "load_aiortc", lambda: (Peer, lambda **kwargs: kwargs))
        await p2p.answer_offer({"type": "offer", "sdp": "offer"}, lambda *_: None,
                               lambda: asyncio.sleep(0), loopback_candidate=False, on_ready=mark_ready)
        await asyncio.wait_for(ready.wait(), 0.1)
        assert "open" in channel_handlers

    asyncio.run(scenario())


def test_offer_watchdog_keeps_already_ready_session_and_reset_closes_all(monkeypatch):
    async def scenario():
        agent = make_agent(p2p_unready_seconds=0.01)
        closed = []

        class Peer:
            async def close(self):
                closed.append(True)

        async def answer_offer(*args, **kwargs):
            await kwargs["on_ready"]()
            return Peer(), {"type": "answer", "sdp": "test"}

        class Control:
            async def send(self, raw):
                pass

        monkeypatch.setattr(main, "answer_offer", answer_offer)
        await agent.handle_p2p_offer({"id": "p2p-ready", "offer": {}}, Control())
        await asyncio.sleep(0.03)
        assert "p2p-ready" in agent.p2p_sessions
        assert "p2p-ready" not in agent.p2p_session_watchdogs
        await agent.reset_p2p_state()
        assert closed == [True]
        assert not agent.p2p_sessions
        assert not agent.p2p_session_watchdogs

    asyncio.run(scenario())


def test_control_loop_deduplicates_active_and_completed_p2p_offers(monkeypatch, tmp_path):
    async def scenario():
        started = []
        closed = []
        hello = asyncio.Event()
        answered = asyncio.Event()
        incoming = asyncio.Queue()
        negotiation_started = asyncio.Event()
        release_negotiation = asyncio.Event()

        class Peer:
            async def close(self):
                closed.append(True)

        async def answer_offer(*args, **kwargs):
            started.append(True)
            negotiation_started.set()
            await release_negotiation.wait()
            return Peer(), {"type": "answer", "sdp": "test"}

        class RegisterResponse:
            status_code = 200
            headers = {}
            text = ""

            def raise_for_status(self):
                pass

            def json(self):
                return {"device_id": "device", "agent_token": "token"}

        class RegisterClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, *args, **kwargs):
                return RegisterResponse()

        class ControlSocket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def send(self, raw):
                message = json.loads(raw)
                if message["type"] == "agent_hello":
                    hello.set()
                if message["type"] == "p2p_answer":
                    answered.set()

            def __aiter__(self):
                return self

            async def __anext__(self):
                item = await incoming.get()
                if item is None:
                    raise StopAsyncIteration
                return json.dumps(item)

            async def close(self, **kwargs):
                pass

        monkeypatch.setattr(main, "answer_offer", answer_offer)
        monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: RegisterClient())
        monkeypatch.setattr(main.websockets, "connect", lambda *args, **kwargs: ControlSocket())
        agent = Agent({"opencode_url": "http://127.0.0.1:9", "gateway_url": "http://gateway", "allow_insecure_gateway": True,
                       "enroll_token": "enroll", "state_file": str(tmp_path / "agent.json")})
        run = asyncio.create_task(agent.run())
        await asyncio.wait_for(hello.wait(), 1)
        offer = {"type": "p2p_offer", "id": "p2p-replay", "offer": {}}
        await incoming.put(offer)
        await asyncio.wait_for(negotiation_started.wait(), 1)
        await incoming.put(dict(offer))
        await asyncio.sleep(0.02)
        assert started == [True]
        release_negotiation.set()
        await asyncio.wait_for(answered.wait(), 1)
        await incoming.put(dict(offer))
        await asyncio.sleep(0.02)
        assert started == [True]
        assert len(agent.p2p_peers) == 1
        await incoming.put({"type": "cancel", "id": "p2p-replay"})
        for _ in range(20):
            if closed:
                break
            await asyncio.sleep(0.01)
        assert closed == [True]
        assert not agent.p2p_sessions
        assert not agent.p2p_peers
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_active", [False, True])
def test_control_loop_rejects_completed_type_conflicts_and_ignores_late_cancels(monkeypatch, tmp_path, cancel_active):
    async def scenario():
        hello = asyncio.Event()
        request_started = asyncio.Event()
        release_request = asyncio.Event()
        request_calls = []
        stream_calls = []
        ws_calls = []
        sent = []
        incoming = asyncio.Queue()

        class RegisterResponse:
            status_code = 200
            headers = {}
            text = ""

            def raise_for_status(self):
                pass

            def json(self):
                return {"device_id": "device", "agent_token": "token"}

        class RegisterClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, *args, **kwargs):
                return RegisterResponse()

        class ControlSocket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def send(self, raw):
                message = json.loads(raw)
                sent.append(message)
                if message["type"] == "agent_hello":
                    hello.set()

            def __aiter__(self):
                return self

            async def __anext__(self):
                item = await incoming.get()
                if item is None:
                    raise StopAsyncIteration
                return json.dumps(item)

            async def close(self, **kwargs):
                pass

        async def local_request(item, timeout=120):
            request_calls.append(item["id"])
            request_started.set()
            await release_request.wait()
            return {"type": "response", "id": item["id"], "status": 200,
                    "headers": {}, "body": ""}

        async def local_stream(item, ws):
            stream_calls.append(item["id"])
            await agent.send_control(ws, {"type": "stream_error", "id": item["id"],
                                          "error": "upstream failed"})

        async def local_ws(item, ws):
            ws_calls.append(item["id"])

        monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: RegisterClient())
        monkeypatch.setattr(main.websockets, "connect", lambda *args, **kwargs: ControlSocket())
        agent = Agent({"opencode_url": "http://127.0.0.1:9", "gateway_url": "http://gateway",
                       "allow_insecure_gateway": True, "enroll_token": "enroll",
                       "state_file": str(tmp_path / "agent.json")})
        agent.local_request = local_request
        agent.local_stream = local_stream
        agent.local_ws = local_ws
        run = asyncio.create_task(agent.run())
        await asyncio.wait_for(hello.wait(), 1)

        request = {"type": "request", "id": "shared", "method": "POST", "path": "/",
                   "headers": {}, "body": ""}
        await incoming.put(request)
        await asyncio.wait_for(request_started.wait(), 1)
        await incoming.put(dict(request))
        await incoming.put({"type": "stream_request", "id": "shared", "method": "GET", "path": "/",
                            "headers": {}, "body": ""})
        await incoming.put({"type": "ws_open", "id": "shared", "path": "/", "headers": {}})
        await asyncio.sleep(0.02)
        assert request_calls == ["shared"]
        assert not stream_calls
        assert not ws_calls

        if cancel_active:
            await incoming.put({"type": "cancel", "id": "shared"})
        else:
            release_request.set()
        for _ in range(20):
            if agent.is_completed_control_id("shared"):
                break
            await asyncio.sleep(0.01)
        assert agent.is_completed_control_id("shared")
        assert sum(message.get("status") == 200 for message in sent) == (0 if cancel_active else 1)
        await incoming.put(dict(request))
        await incoming.put({"type": "stream_request", "id": "shared", "method": "GET", "path": "/",
                            "headers": {}, "body": ""})
        await incoming.put({"type": "ws_open", "id": "shared", "path": "/", "headers": {}})
        await asyncio.sleep(0.05)
        assert request_calls == ["shared"]
        assert not stream_calls
        assert not ws_calls
        assert sum(message.get("status") == 409 for message in sent) == 1
        assert sum(message.get("type") == "stream_error" for message in sent) == 1
        assert sum(message.get("type") == "ws_error" for message in sent) == 1
        await incoming.put({"type": "cancel", "id": "shared"})
        await incoming.put({"type": "cancel", "id": "shared"})
        await asyncio.sleep(0.02)
        assert request_calls == ["shared"]
        assert not stream_calls
        assert not ws_calls
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)

    asyncio.run(scenario())


def test_offer_watchdog_closes_unready_peer_and_cleans_session(monkeypatch):
    async def scenario():
        agent = make_agent(p2p_unready_seconds=0.01)
        closed = []

        class Peer:
            async def close(self):
                closed.append(True)

        async def answer_offer(*args, **kwargs):
            return Peer(), {"type": "answer", "sdp": "test"}

        class Control:
            async def send(self, raw):
                pass

        monkeypatch.setattr(main, "answer_offer", answer_offer)
        await agent.handle_p2p_offer({"id": "p2p-unready", "offer": {}}, Control())
        await asyncio.sleep(0.03)
        assert closed == [True]
        assert not agent.p2p_sessions
        assert not agent.p2p_session_watchdogs
        assert not agent.p2p_peers

    asyncio.run(scenario())


def test_control_loop_stream_error_and_active_ws_duplicate_are_single_flight(monkeypatch, tmp_path):
    async def scenario():
        hello = asyncio.Event()
        stream_done = asyncio.Event()
        ws_started = asyncio.Event()
        release_ws = asyncio.Event()
        incoming = asyncio.Queue()
        sent = []
        stream_calls = []
        ws_calls = []

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
                if message.get("id") == "stream" and message["type"] == "stream_error":
                    stream_done.set()

            def __aiter__(self):
                return self

            async def __anext__(self):
                item = await incoming.get()
                if item is None:
                    raise StopAsyncIteration
                return json.dumps(item)

            async def close(self, **kwargs):
                pass

        async def local_stream(item, ws):
            stream_calls.append(item["id"])
            await agent.send_control(ws, {"type": "stream_error", "id": item["id"], "error": "failed"})

        async def local_ws(item, ws):
            ws_calls.append(item["id"])
            ws_started.set()
            await release_ws.wait()

        monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: Client())
        monkeypatch.setattr(main.websockets, "connect", lambda *args, **kwargs: Socket())
        agent = Agent({"opencode_url": "http://127.0.0.1:9", "gateway_url": "http://gateway",
                       "allow_insecure_gateway": True, "enroll_token": "enroll",
                       "state_file": str(tmp_path / "agent.json")})
        agent.local_stream = local_stream
        agent.local_ws = local_ws
        run = asyncio.create_task(agent.run())
        await asyncio.wait_for(hello.wait(), 1)
        stream = {"type": "stream_request", "id": "stream", "method": "GET", "path": "/", "headers": {}, "body": ""}
        await incoming.put(stream)
        await asyncio.wait_for(stream_done.wait(), 1)
        await incoming.put(dict(stream))
        ws = {"type": "ws_open", "id": "socket", "path": "/", "headers": {}}
        await incoming.put(ws)
        await asyncio.wait_for(ws_started.wait(), 1)
        await incoming.put(dict(ws))
        await asyncio.sleep(0.02)
        assert stream_calls == ["stream"]
        assert ws_calls == ["socket"]
        assert sum(message.get("type") == "stream_error" and message.get("id") == "stream" for message in sent) == 2
        release_ws.set()
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)

    asyncio.run(scenario())
