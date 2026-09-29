"""Regression tests for bounded review findings without network services."""
import asyncio
import base64
import json
import time

import httpx
import src.main as main
from src.main import Agent, Gateway, filter_response_headers
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
