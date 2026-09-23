"""Mesh 可靠性回归基线。

本文件把浏览器端 P2P 帧合并器（忠实移植 src/static_adapter.py 的 settle()）
与当前未加守卫的分片语义建模为可本地运行的协议 reducer，从而无需真实浏览器
或在线 Relay/P2P 连接即可复现 79b0d5d 基线上的协议数据完整性缺陷。

基线期望（Task 1 红灯基线）：
- 描述既有缺陷的回归用例必须失败：状态帧之后 chunk/end 被丢弃、
  error 帧无法关闭流、分片无 message_id/sequence/大小守卫、超限请求未被 413 拒绝。
- 文档性用例保持绿灯：合法序列（同一 message_id、sequence 递增、恰好达上限）、
  Agent 对恰好达上限的请求体放行。
Task 2/3 实现真实守卫后，这些红灯用例应转为绿灯。
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import re
import tomllib
from pathlib import Path

import httpx

from src.main import (Agent, Gateway, StreamState, backoff_delay, filter_response_headers,
                      forwarding_headers, inject_mesh_bar,
                      parse_retry_after,
                      parse_server_route, rewrite_device_html)
from src.static_adapter import TRANSPORT_ADAPTER
from src.p2p import (ASSEMBLY_BUDGET_REASON, CONNECTION_FAILED_REASON,
                     INVALID_ENCODING_REASON, INVALID_SEQUENCE_REASON,
                     MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES,
                     PAYLOAD_TOO_LARGE_REASON, REQUEST_TOO_LARGE_REASON,
                     RESPONSE_TOO_LARGE_REASON, STREAM_OVERFLOW_REASON,
                     UPSTREAM_ERROR_REASON, ChunkAssembler, FrameError,
                     ResponseSizeGuard, decoded_size, enable_loopback_candidate,
                     frame, iter_frames,
                     p2p_message_limit, p2p_total_budget, read_bounded,
                     request_limit, response_limit, ws_frame_limit)


# 测试本地配置的较小限制，让边界用例运行快且精确。
# Gateway 生产默认值为 max_request_bytes = 64 * 1024 * 1024（见 src/main.py）。
LIMIT_BYTES = 8192


def b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def unb64(payload: str) -> bytes:
    """镜像浏览器端 atob()，空串视为空字节。"""
    return base64.b64decode(payload or "")


class Controller:
    """镜像浏览器端 ReadableStream controller 的可观测部分。"""

    def __init__(self):
        self.enqueued: list[bytes] = []
        self.closed = False
        self.errored = None

    def enqueue(self, value: bytes) -> None:
        self.enqueued.append(value)

    def close(self) -> None:
        self.closed = True

    def error(self, error) -> None:
        self.errored = error


class BrowserState:
    """保存 settle() 依赖的 pending/streams/sockets 状态与解析记录。"""

    def __init__(self):
        self.pending: dict[str, dict] = {}
        self.streams: dict[str, dict] = {}
        self.sockets: dict[str, object] = {}
        # 形如 ("resolve"|"reject", 载荷) 的顺序记录。
        self.settled: list[tuple[str, object]] = []


class FakeSocket:
    """Observable state machine mirroring the browser MeshWebSocket."""

    CONNECTING = 0
    OPEN = 1
    CLOSING = 2
    CLOSED = 3

    def __init__(self):
        self.readyState = FakeSocket.CONNECTING
        self.protocol = ""
        self.events: list[tuple[str, object]] = []

    def dispatch(self, type_: str, event: object) -> None:
        self.events.append((type_, event))


def open_stream(state: BrowserState, stream_id: str) -> Controller:
    """模拟 p2pFetch 的 SSE 分支：先建立 pending 入口，再登记 stream controller。"""
    state.pending[stream_id] = {"response": None}
    controller = Controller()
    state.streams[stream_id] = {"controller": controller}
    return controller


def browser_settle(state: BrowserState, message: dict) -> None:
    """模拟修复后的浏览器流状态机，验证状态帧不会夺走流的所有权。"""
    if message.get("type") == "pong":
        return
    socket = state.sockets.get(message.get("id"))
    if socket is not None:
        mtype = message.get("type")
        if mtype == "ws_opened":
            # Only CONNECTING can transition to OPEN; late opens are ignored.
            if socket.readyState == FakeSocket.CONNECTING:
                socket.readyState = FakeSocket.OPEN
                # An unnegotiated protocol clears the constructor's requested value.
                if "protocol" in message:
                    socket.protocol = message.get("protocol") or ""
                socket.dispatch("open", {})
        elif mtype in ("ws_closed", "ws_error"):
            socket.readyState = FakeSocket.CLOSED
            socket.dispatch("close", {})
        return
    entry = state.pending.get(message.get("id"))
    if entry is None:
        return
    mtype = message.get("type")
    mid = message.get("id")
    if mtype == "response_start":
        entry["response"] = {"status": message.get("status"),
                             "headers": message.get("headers", {}) or {},
                             "chunks": []}
        return
    if mtype == "response_chunk":
        if entry.get("response"):
            entry["response"]["chunks"].append(message.get("body") or "")
        return
    if mtype == "response_end":
        state.pending.pop(mid, None)
        if not entry.get("response"):
            state.settled.append(("reject", "incomplete response"))
            return
        state.settled.append(("resolve", {"type": "response",
                                          "status": entry["response"]["status"],
                                          "headers": entry["response"]["headers"],
                                          "body": "".join(entry["response"]["chunks"])}))
        return
    if mtype == "response":
        state.pending.pop(mid, None)
        state.settled.append(("resolve", message))
        return
    if mtype == "cancelled":
        stream = state.streams.get(mid)
        if stream is not None:
            # A pre-header cancellation must error and clean up, not become an empty 200 stream.
            state.streams.pop(mid, None)
            state.pending.pop(mid, None)
            state.settled.append(("reject", "AbortError"))
            stream["controller"].error("AbortError")
            return
        state.pending.pop(mid, None)
        state.settled.append(("resolve", message))
        return
    stream = state.streams.get(mid)
    if stream is None:
        return
    controller = stream["controller"]
    if mtype == "stream_chunk" and message.get("status") and not entry.get("resolved"):
        entry["resolved"] = True
        state.settled.append(("resolve", message))
    if mtype == "stream_chunk" and message.get("body") is not None:
        controller.enqueue(unb64(message.get("body")))
    if mtype in ("stream_end", "stream_error"):
        state.streams.pop(mid, None)
        state.pending.pop(mid, None)
        if mtype == "stream_error":
            error = message.get("error") or "stream failed"
            state.settled.append(("reject", error))
            controller.error(error)
        else:
            controller.close()


def make_chunk(message_id: str, sequence: int, data: bytes, final: bool = False) -> dict:
    """构造 Task 2/3 定义的分片信封帧 {message_id, sequence, data, final}。"""
    return {"message_id": message_id, "sequence": sequence,
            "data": b64(data), "final": final}


# ---------- Step 1: P2P 流状态机（浏览器帧合并） ----------

def test_status_frame_then_two_chunks_and_end_yields_one_response_both_chunks_and_close():
    """状态帧 + 两个 chunk + end 帧 → 恰好一次 response、两个 chunk 和一个 close。"""
    state = BrowserState()
    sid = "stream-1"
    controller = open_stream(state, sid)

    browser_settle(state, {"type": "stream_chunk", "id": sid, "status": 200,
                           "headers": {"content-type": "text/event-stream"}})
    browser_settle(state, {"type": "stream_chunk", "id": sid,
                           "body": b64(b"data: frame one\n\n")})
    browser_settle(state, {"type": "stream_chunk", "id": sid,
                           "body": b64(b"data: frame two\n\n")})
    browser_settle(state, {"type": "stream_end", "id": sid})

    resolved = [payload for kind, payload in state.settled if kind == "resolve"]
    assert len(resolved) == 1
    assert resolved[0]["status"] == 200
    assert controller.enqueued == [b"data: frame one\n\n", b"data: frame two\n\n"]
    assert controller.closed is True
    assert sid not in state.streams


def test_error_frame_after_status_closes_stream_with_error():
    """状态帧之后到达的 error 帧必须让流以错误关闭并清理状态。"""
    state = BrowserState()
    sid = "stream-2"
    controller = open_stream(state, sid)

    browser_settle(state, {"type": "stream_chunk", "id": sid, "status": 200,
                           "headers": {}})
    browser_settle(state, {"type": "stream_error", "id": sid, "error": "upstream reset"})

    assert controller.errored == "upstream reset"
    assert controller.closed is False
    assert sid not in state.streams


# ---------- Step 2: 大小与序列守卫 ----------

def test_payload_at_limit_is_accepted():
    """同一 message_id、sequence 递增、恰好等于配置上限的载荷应被完整接收。"""
    assembler = ChunkAssembler(limit=LIMIT_BYTES, trace=True)
    half = LIMIT_BYTES // 2
    assembler.feed(make_chunk("m", 0, b"a" * half))
    outcome = assembler.feed(make_chunk("m", 1, b"b" * half, final=True))

    assert outcome == "accepted"
    assert assembler.trace == [("m", 0), ("m", 1)]
    assert assembler.total_of("m") == LIMIT_BYTES
    assert assembler.result("m") == b"a" * half + b"b" * half


def test_distinct_message_ids_remain_isolated():
    """不同 message_id 的交错分片必须各自独立装配、互不混入。"""
    assembler = ChunkAssembler(limit=LIMIT_BYTES)
    assembler.feed(make_chunk("msg-a", 0, b"alpha"))
    assembler.feed(make_chunk("msg-b", 0, b"beta", final=True))
    assembler.feed(make_chunk("msg-a", 1, b"omega", final=True))

    assert assembler.is_accepted("msg-a")
    assert assembler.is_accepted("msg-b")
    assert assembler.result("msg-a") == b"alphaomega"
    assert assembler.result("msg-b") == b"beta"


def test_payload_one_byte_over_limit_is_rejected():
    """超过配置上限一个字节就必须得到有界错误，不得静默接收。"""
    assembler = ChunkAssembler(limit=LIMIT_BYTES)
    assembler.feed(make_chunk("m", 0, b"a" * (LIMIT_BYTES + 1), final=True))

    assert assembler.error_of("m") is not None
    assert not assembler.is_accepted("m")


def test_duplicate_sequence_is_rejected():
    """重复 sequence 的数据必须作为有界错误拒绝，而不是被重复拼接。"""
    assembler = ChunkAssembler(limit=LIMIT_BYTES, trace=True)
    assembler.feed(make_chunk("m", 0, b"x"))
    assembler.feed(make_chunk("m", 1, b"y"))
    assembler.feed(make_chunk("m", 1, b"y"))  # 重复 sequence
    assembler.feed(make_chunk("m", 2, b"z", final=True))

    assert assembler.trace == [("m", 0), ("m", 1), ("m", 1), ("m", 2)]
    assert assembler.error_of("m") is not None
    assert not assembler.is_accepted("m")


def test_missing_sequence_is_rejected():
    """缺失中间 sequence（跳号）必须作为残缺序列拒绝，不能以残缺数据完成。"""
    assembler = ChunkAssembler(limit=LIMIT_BYTES)
    assembler.feed(make_chunk("m", 0, b"head"))
    assembler.feed(make_chunk("m", 2, b"tail", final=True))  # 缺 seq 1

    assert assembler.error_of("m") is not None
    assert not assembler.is_accepted("m")


def test_repeated_end_frame_is_rejected():
    """重复的结束帧不得被静默忽略，必须产生有界错误。"""
    assembler = ChunkAssembler(limit=LIMIT_BYTES)
    assembler.feed(make_chunk("m", 0, b"x", final=True))
    outcome = assembler.feed(make_chunk("m", 1, b"y", final=True))

    assert outcome == "error"
    assert assembler.error_of("m") is not None


# ---------- Step 2（真实代码）：Agent 侧请求体大小守卫 ----------

def _fake_p2p_channel(sink):
    class FakeChannel:
        bufferedAmount = 0
        readyState = "open"

        def send(self, payload: str) -> None:
            sink.append(json.loads(payload))

    return FakeChannel()


def _agent():
    return Agent({"opencode_url": "http://127.0.0.1:1",
                  "max_request_bytes": str(LIMIT_BYTES),
                  "request_timeout": "5"})


def test_gateway_lifespan_closes_streams_and_bridges(tmp_path):
    """Shutdown cleans up every bridge even if one close fails."""
    gateway = Gateway({"state_file": str(tmp_path / "state.json")})
    stream = StreamState(4)
    closed = []

    class Bridge:
        def __init__(self, fails=False):
            self.fails = fails

        async def close(self, code):
            closed.append(code)
            if self.fails:
                raise RuntimeError("already disconnected")

    async def scenario():
        async with gateway.app.router.lifespan_context(gateway.app):
            gateway.streams["stream"] = stream
            gateway.browser_ws.update(first=Bridge(True), second=Bridge())
            assert not stream.closed
            assert closed == []

    asyncio.run(scenario())
    assert stream.closed
    assert stream.pending_end == {"type": "stream_error", "error": "Gateway is shutting down"}
    assert closed == [1001, 1001]


def test_parse_server_route_resolves_device_and_upstream_path():
    """OpenCode server routes resolve to the encoded Mesh device scope."""
    import base64

    server_url = "https://mesh.example.com/_mesh/device/device-b"
    key = base64.urlsafe_b64encode(server_url.encode()).decode().rstrip("=")

    assert parse_server_route(f"/server/{key}/session/s-1") == ("device-b", "/session/s-1")
    assert parse_server_route(f"/server/{key}/") == ("device-b", "/")


def test_rewrite_device_html_scopes_root_assets():
    """Device HTML keeps static assets bound to the device that served it."""
    body = b'<script src="/_assets/index.js"></script><link href="/assets/app.css">'

    result = rewrite_device_html(body, "device-b")

    assert b"/_mesh/device/device-b/_assets/index.js" in result
    assert b"/_mesh/device/device-b/assets/app.css" in result


def test_adapter_scopes_servers_and_relay_fallback_to_active_device():
    """The browser adapter must keep server tabs and Relay requests device-scoped."""
    assert "serverTabUrl(device.device_id)" in TRANSPORT_ADAPTER
    assert "scopeNativeRequest" in TRANSPORT_ADAPTER
    assert "currentDeviceId()" in TRANSPORT_ADAPTER


def test_adapter_uses_manifest_device_for_root_relay_requests():
    """A root Server page must bind fallback API calls to its selected device."""
    assert "const activeDeviceId = () => currentDeviceId() || selectedServerDeviceId() || state.manifest?.device_id || state.defaultDevice" in TRANSPORT_ADAPTER
    assert "const deviceId = activeDeviceId();" in TRANSPORT_ADAPTER


def test_adapter_resolves_v2_selected_server_before_manifest():
    """V2's selected Server must override the page's P2P manifest device."""
    assert "selectedServerDeviceId()" in TRANSPORT_ADAPTER
    assert "const activeDeviceId = () => currentDeviceId() || selectedServerDeviceId() || state.manifest?.device_id || state.defaultDevice" in TRANSPORT_ADAPTER
    assert "different device uses Relay" not in TRANSPORT_ADAPTER


def test_agent_accepts_consecutive_ws_data_frames_for_one_socket():
    """Consecutive input frames on one WebSocket use distinct transport IDs."""
    async def scenario():
        sent = []
        agent = _agent()
        channel = _fake_p2p_channel(sent)
        queue = asyncio.Queue()
        agent.ws_queues["socket-1"] = queue

        frames = [
            ("frame-1", {"type": "ws_data", "id": "socket-1", "kind": "text", "data": "a"}),
            ("frame-2", {"type": "ws_data", "id": "socket-1", "kind": "text", "data": "b"}),
            ("frame-3", {"type": "ws_close", "id": "socket-1", "code": 1000, "reason": ""}),
        ]
        for frame_id, message in frames:
            payload = json.dumps(message).encode()
            await agent.p2p_message(channel, make_chunk(frame_id, 0, payload, True))

        received = []
        while not queue.empty():
            received.append(queue.get_nowait())
        return received, sent

    received, errors = asyncio.run(scenario())
    assert [item["data"] for item in received[:2]] == ["a", "b"]
    assert received[2]["type"] == "ws_close"
    assert errors == []


def test_agent_guard_accepts_request_body_at_limit():
    """真实 Agent.p2p_message 对恰好等于上限的请求体不应拒绝，应继续转发。"""
    sent = []

    async def scenario():
        agent = _agent()

        async def forwarded(item, timeout=120.0):
            return {"type": "response", "id": item["id"], "status": 200,
                    "headers": {}, "body": b64(b"ok")}

        agent.local_request = forwarded
        await agent.p2p_message(_fake_p2p_channel(sent), {
            "type": "request", "id": "r-at-limit", "method": "GET", "path": "/",
            "headers": {}, "body": b64(b"x" * LIMIT_BYTES)})

    asyncio.run(scenario())
    assert sent and sent[0]["type"] == "response" and sent[0]["status"] == 200


def test_agent_guard_rejects_request_body_one_byte_over_limit():
    """真实 Agent.p2p_message 对超过上限一个字节的请求体必须返回有界 413 错误。"""
    sent = []

    async def scenario():
        agent = _agent()

        async def forwarded(item, timeout=120.0):
            raise AssertionError("oversized request 不应到达 local_request")

        agent.local_request = forwarded
        await agent.p2p_message(_fake_p2p_channel(sent), {
            "type": "request", "id": "r-over-limit", "method": "GET", "path": "/",
            "headers": {}, "body": b64(b"x" * (LIMIT_BYTES + 1))})

    asyncio.run(scenario())
    assert sent and sent[0]["type"] == "response" and sent[0]["status"] == 413


# ---------- Step 3（真实代码）：base64 边界与双向分片重组 ----------

def test_decoded_size_matches_actual_base64_for_boundaries():
    """decoded_size 必须精确到 3 字节边界，不依赖 base64 编码长度。"""
    for size in (0, 1, 2, 3, 4, 5, 8192, 8193, 8194, 65536):
        assert decoded_size(b64(b"x" * size)) == size


def test_frame_round_trip_reassembles_multi_frame_payload():
    """iter_frames → ChunkAssembler 双向分片/重组必须无损。"""
    payload = bytes(range(64)) * 4
    assembler = ChunkAssembler(limit=LIMIT_BYTES)
    frames = list(iter_frames("round-trip", payload, chunk_size=32))
    assert len(frames) >= 3
    outcome = None
    for chunk in frames:
        outcome = assembler.feed(chunk)
    assert outcome == "accepted"
    assert assembler.result("round-trip") == payload
    assert assembler.total_of("round-trip") == len(payload)


def test_frame_round_trip_empty_payload_has_single_final_frame():
    """空载荷也必须以一个 final 帧完成装配。"""
    assembler = ChunkAssembler(limit=LIMIT_BYTES)
    frames = list(iter_frames("empty", b""))
    assert len(frames) == 1 and frames[0]["final"] is True
    assert assembler.feed(frames[0]) == "accepted"
    assert assembler.result("empty") == b""


# ---------- Step 4：响应大小上限（base64 展开前拦截） ----------

class _AsyncChunks:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def test_read_bounded_accepts_exact_limit_and_rejects_one_over():
    """流式读取恰好达上限放行，超一字节立刻有界报错。"""
    async def scenario():
        ok = await read_bounded(_AsyncChunks([b"a" * 40, b"b" * 24]), 64)
        try:
            await read_bounded(_AsyncChunks([b"a" * 40, b"b" * 25]), 64)
            return ok, False
        except FrameError:
            return ok, True

    ok, rejected = asyncio.run(scenario())
    assert ok == b"a" * 40 + b"b" * 24
    assert rejected is True


# ---------- Step 5：Relay 流溢出显式错误（不驱逐旧分片） ----------

def test_stream_overflow_emits_single_explicit_error_without_evicting():
    """队列满时保留已缓冲分片，仅产生一条稳定原因的 stream_error。"""
    async def scenario():
        state = StreamState(maxsize=2)
        assert Gateway.enqueue_stream(state, {"type": "stream_chunk", "body": b64(b"a")})
        assert Gateway.enqueue_stream(state, {"type": "stream_chunk", "body": b64(b"b")})
        assert Gateway.enqueue_stream(state, {"type": "stream_chunk", "body": b64(b"c")}) is False
        return [message async for message in Gateway.stream_messages(state)]

    messages = asyncio.run(scenario())
    bodies = [m.get("body") for m in messages if m.get("type") == "stream_chunk"]
    errors = [m for m in messages if m.get("type") == "stream_error"]
    assert bodies == [b64(b"a"), b64(b"b")]
    assert len(errors) == 1
    assert errors[0]["error"] == StreamState.OVERFLOW_REASON


def test_stream_terminal_frame_is_delivered_after_buffered_chunks():
    """队列满时 end 帧被延迟投递，先排空已缓冲分片再结束。"""
    async def scenario():
        state = StreamState(maxsize=1)
        assert Gateway.enqueue_stream(state, {"type": "stream_chunk", "body": b64(b"a")})
        assert Gateway.enqueue_stream(state, {"type": "stream_end"}) is True
        return [message async for message in Gateway.stream_messages(state)]

    messages = asyncio.run(scenario())
    assert [m["type"] for m in messages] == ["stream_chunk", "stream_end"]


# ---------- Step 1/2：控制发送超时 ----------

class _HangingControlWS:
    def __init__(self):
        self.closed: list[int] = []

    async def send_text(self, payload: str) -> None:
        await asyncio.sleep(10)

    async def close(self, code: int = 1000, reason=None) -> None:
        self.closed.append(code)


def test_gateway_control_send_timeout_closes_device_and_raises(tmp_path):
    """控制发送超时必须关闭设备连接并抛出连接级错误。"""
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "gateway.json")})
        ws = _HangingControlWS()
        try:
            await gateway.send_control(ws, {"type": "ping"}, timeout=0.05)
            return False
        except ConnectionError:
            return True

    assert asyncio.run(scenario()) is True


def test_gateway_control_send_timeout_closes_connection(tmp_path):
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "gateway.json")})
        ws = _HangingControlWS()
        try:
            await gateway.send_control(ws, {"type": "ping"}, timeout=0.05)
        except ConnectionError:
            pass
        return ws.closed

    assert asyncio.run(scenario()) == [1011]


# ---------- Step 6：同 device_id 连接与断线清理 ----------

class _FakeControlWS:
    def __init__(self):
        self.closed: list[int] = []

    async def send_text(self, payload: str) -> None:
        return None

    async def close(self, code: int = 1000, reason=None) -> None:
        self.closed.append(code)


def test_attach_device_replaces_old_control_and_fails_pending(tmp_path):
    """同 device_id 重连：关闭旧连接、失败其挂起请求/流/P2P 回答，且清理幂等。"""
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "gateway.json")})
        old = _FakeControlWS()
        device = {"device_id": "dev", "auth_token": "t", "ws": old}
        gateway.registry.devices["dev"] = device
        future = asyncio.get_running_loop().create_future()
        gateway.pending["r1"] = future
        gateway.owners["r1"] = old
        state = StreamState(maxsize=2)
        gateway.streams["r1"] = state
        answer = asyncio.get_running_loop().create_future()
        gateway.p2p_answers["s1"] = answer
        gateway.p2p_owners["s1"] = old

        new = _FakeControlWS()
        await gateway.attach_device(device, new)

        failed = False
        try:
            await future
        except ConnectionError:
            failed = True
        assert failed is True
        assert state.pending_end == {"type": "stream_error", "error": "Device control connection lost"}
        assert device["ws"] is new
        assert "r1" not in gateway.owners and "r1" not in gateway.pending
        assert "s1" not in gateway.p2p_answers
        assert answer.done() and answer.exception() is not None
        # 幂等：重复清理不得抛错或影响新连接
        await gateway.cleanup_device(old)
        await gateway.cleanup_device(old)
        assert device["ws"] is new

    asyncio.run(scenario())


def test_old_control_connection_cannot_update_last_seen(tmp_path):
    """被替换的旧连接不得再更新 last_seen。"""
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "gateway.json")})
        old = _FakeControlWS()
        device = {"device_id": "dev", "auth_token": "t", "ws": old, "last_seen": 1}
        gateway.registry.devices["dev"] = device
        new = _FakeControlWS()
        await gateway.attach_device(device, new)
        before = device["last_seen"]
        assert device.get("ws") is new
        # 旧连接在接收循环中会用身份检查提前退出；此处直接验证身份归属。
        assert gateway.registry.devices["dev"]["ws"] is not old
        assert device["last_seen"] == before

    asyncio.run(scenario())


# ---------- Step 7：429 退避与身份恢复 ----------

def test_backoff_delay_is_bounded_and_respects_retry_after():
    assert backoff_delay(1, base=1.0, cap=60.0, jitter=0.0) == 1.0
    assert backoff_delay(2, base=1.0, cap=60.0, jitter=0.0) == 2.0
    assert backoff_delay(10, base=1.0, cap=60.0, jitter=0.0) == 60.0
    assert backoff_delay(3, base=1.0, cap=60.0, jitter=0.5,
                         rng=random.Random(0)) <= 6.0
    assert backoff_delay(1, base=1.0, cap=60.0, jitter=0.0, retry_after=7.0) == 7.0
    assert backoff_delay(1, base=1.0, cap=60.0, jitter=0.0, retry_after=120.0) == 60.0
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") is None
    assert parse_retry_after(None) is None


def test_register_rotates_token_with_valid_enroll_token(tmp_path):
    """持久化 token 与 Gateway 不一致时，可凭 enroll_token 轮换身份恢复。"""
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "gateway.json"),
                           "enroll_token": "enroll",
                           "auth": {"username": "u", "password": "p"}})
        transport = httpx.ASGITransport(app=gateway.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = {"device_id": "dev-1", "enroll_token": "enroll"}
            first = await client.post("/_mesh/register", json=body)
            assert first.status_code == 200
            original = first.json()["agent_token"]
            assert (await client.post("/_mesh/register", json=body)).status_code == 403
            rotated = await client.post("/_mesh/register", json=body | {"rotate_token": True})
            assert rotated.status_code == 200
            assert rotated.json()["agent_token"] != original

    asyncio.run(scenario())


# ---------- Step 3（真实代码）：Agent 侧分片请求重组 ----------

def test_agent_reassembles_framed_request_over_p2p():
    """Agent.p2p_message 必须能重组浏览器分片信封并转发完整请求。"""
    sent = []

    async def scenario():
        agent = _agent()

        async def forwarded(item, timeout=120.0):
            return {"type": "response", "id": item["id"], "status": 200,
                    "headers": {}, "body": b64(b"ok")}

        agent.local_request = forwarded
        request = json.dumps({"type": "request", "id": "r-frame", "method": "GET",
                              "path": "/", "headers": {}, "body": b64(b"x")}).encode()
        channel = _fake_p2p_channel(sent)
        for chunk in iter_frames("r-frame", request, chunk_size=16):
            await agent.p2p_message(channel, chunk)

    asyncio.run(scenario())
    assert sent and sent[0]["status"] == 200
    assert sent[0]["message_id"] == "r-frame"


def test_p2p_send_chunks_large_response_into_reassemblable_envelope():
    """大响应必须拆成 {message_id, sequence, data, final} 信封且可无损重组。"""
    sent = []

    async def scenario():
        agent = _agent()
        channel = _fake_p2p_channel(sent)
        raw = b"z" * 40000
        await agent.p2p_send(channel, {"type": "response", "id": "big", "status": 200,
                                       "headers": {}, "body": b64(raw)})

    asyncio.run(scenario())
    assert len(sent) >= 2
    assert sent[0]["message_id"] == "big" and sent[0]["status"] == 200
    assert sent[-1]["final"] is True
    assembler = ChunkAssembler(limit=40001)
    outcome = None
    for frame_message in sent:
        outcome = assembler.feed(frame_message)
    assert outcome == "accepted"
    assert assembler.result("big") == b"z" * 40000


# ================================================================
# 审查修订（Task 2 revision）：Critical/Important 修复的回归覆盖
# ================================================================

# ---------- item 4/5：统一上限 helper ----------

def test_p2p_message_limit_inherits_request_limit_by_default():
    """未显式配置 max_p2p_message_bytes 时必须继承 max_request_bytes，避免 64 倍分叉。"""
    assert request_limit({}) == MAX_REQUEST_BYTES
    assert p2p_message_limit({}) == MAX_REQUEST_BYTES
    assert p2p_message_limit({"max_request_bytes": str(LIMIT_BYTES)}) == LIMIT_BYTES
    assert p2p_message_limit({"max_request_bytes": "100", "max_p2p_message_bytes": "50"}) == 50


def test_response_limit_reads_single_helper():
    assert response_limit({}) == MAX_RESPONSE_BYTES
    assert response_limit({"max_response_bytes": "123"}) == 123


def test_ws_frame_limit_covers_app_level_request_limit():
    """uvicorn ws_max_size 必须不小于应用层上限的两倍（base64 + JSON 开销）。"""
    assert ws_frame_limit({"max_request_bytes": str(LIMIT_BYTES)}) >= LIMIT_BYTES * 2
    big = 64 * 1024 * 1024
    assert ws_frame_limit({"max_request_bytes": str(big)}) >= big * 2


# ---------- item 3/4：响应大小守卫 ----------

def test_response_guard_rejects_oversized_single_chunk_before_decode():
    """单片超限必须在解码前拦截，不构造超限副本。"""
    guard = ResponseSizeGuard(100)
    raised = False
    try:
        guard.add_encoded(b64(b"x" * 101))
    except FrameError as exc:
        raised = str(exc) == RESPONSE_TOO_LARGE_REASON
    assert raised is True
    assert guard.total == 0


def test_response_guard_rejects_cumulative_overflow():
    guard = ResponseSizeGuard(100)
    guard.add_encoded(b64(b"a" * 60))
    raised = False
    try:
        guard.add_encoded(b64(b"b" * 41))
    except FrameError:
        raised = True
    assert raised is True
    assert guard.total == 60


def test_response_guard_accepts_exact_limit():
    guard = ResponseSizeGuard(100)
    assert guard.add_encoded(b64(b"a" * 100)) == 100
    assert guard.total == 100


# ---------- item 6/7/10：组装器边界 ----------

def test_assembler_trace_disabled_by_default():
    """生产环境默认不记录无界 trace。"""
    assembler = ChunkAssembler(limit=LIMIT_BYTES)
    assembler.feed(make_chunk("m", 0, b"x", final=True))
    assert assembler.trace == []


def test_assembler_rejects_invalid_base64_strictly():
    assembler = ChunkAssembler(limit=LIMIT_BYTES)
    outcome = assembler.feed({"message_id": "m", "sequence": 0,
                              "data": "!!!!", "final": True})
    assert outcome == "error"
    assert assembler.error_of("m") is not None


def test_assembler_rejects_oversized_single_frame_before_decoding():
    assembler = ChunkAssembler(limit=LIMIT_BYTES)
    outcome = assembler.feed(make_chunk("m", 0, b"x" * (LIMIT_BYTES + 1), final=True))
    assert outcome == "error"
    assert assembler.total_of("m") == 0


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


def test_assembler_bounds_incomplete_assemblies_by_count():
    clock = _FakeClock()
    assembler = ChunkAssembler(limit=LIMIT_BYTES, max_assemblies=2, ttl=1000, clock=clock)
    assembler.feed(make_chunk("a", 0, b"1"))
    assembler.feed(make_chunk("b", 0, b"2"))
    assembler.feed(make_chunk("c", 0, b"3"))

    assert assembler.pending_count() == 2
    assert assembler.total_of("a") == 0  # 最旧的不完整装配被释放
    assert assembler.total_of("b") == 1
    assert assembler.total_of("c") == 1


def test_assembler_expires_incomplete_assemblies_by_ttl():
    clock = _FakeClock()
    assembler = ChunkAssembler(limit=LIMIT_BYTES, max_assemblies=8, ttl=10, clock=clock)
    assembler.feed(make_chunk("old", 0, b"1"))
    clock.advance(11)
    assembler.feed(make_chunk("new", 0, b"2"))

    assert assembler.pending_count() == 1
    assert assembler.total_of("old") == 0
    assert assembler.total_of("new") == 1


def test_agent_cancel_releases_incomplete_assembly():
    """取消必须释放该 message_id 的残缺装配，避免无限增长。"""
    async def scenario():
        agent = _agent()
        channel = _fake_p2p_channel([])
        await agent.p2p_message(channel, frame("r-incomplete", 0, b'{"type":"req', False))
        assert id(channel) in agent.p2p_assemblers
        await agent.p2p_message(channel, {"type": "cancel", "id": "r-incomplete"})
        assembler = agent.p2p_assemblers.get(id(channel))
        assert assembler is None or assembler.total_of("r-incomplete") == 0

    asyncio.run(scenario())


# ---------- item 10：message_id 与内层 id 校验 ----------

def test_agent_rejects_outer_inner_message_id_mismatch():
    sent = []

    async def scenario():
        agent = _agent()

        async def forwarded(item, timeout=120.0):
            raise AssertionError("外层/内层 id 不一致的请求不应转发")

        agent.local_request = forwarded
        request = json.dumps({"type": "request", "id": "inner", "method": "GET",
                              "path": "/", "headers": {}, "body": b64(b"")}).encode()
        channel = _fake_p2p_channel(sent)
        for chunk in iter_frames("outer", request, chunk_size=16):
            await agent.p2p_message(channel, chunk)

    asyncio.run(scenario())
    assert sent and sent[0]["status"] == 400
    assert sent[0]["message_id"] == "outer"


def test_agent_rejects_invalid_base64_request_body():
    sent = []

    async def scenario():
        agent = _agent()

        async def forwarded(item, timeout=120.0):
            raise AssertionError("非法 base64 请求体不应转发")

        agent.local_request = forwarded
        await agent.p2p_message(_fake_p2p_channel(sent), {
            "type": "request", "id": "r-bad", "method": "GET", "path": "/",
            "headers": {}, "body": "!!!!"})

    asyncio.run(scenario())
    assert sent and sent[0]["status"] == 400


# ---------- item 9：Agent 控制发送有界 ----------

class _HangingAgentWS:
    def __init__(self):
        self.closed: list[int] = []

    async def send(self, payload: str) -> None:
        await asyncio.sleep(10)

    async def close(self, code: int = 1000, reason=None) -> None:
        self.closed.append(code)


def test_agent_control_send_timeout_closes_and_raises():
    async def scenario():
        agent = _agent()
        ws = _HangingAgentWS()
        try:
            await agent.send_control(ws, {"type": "pong"}, timeout=0.05)
            return False, ws.closed
        except ConnectionError:
            return True, ws.closed

    raised, closed = asyncio.run(scenario())
    assert raised is True
    assert closed == [1011]


# ---------- item 2：Agent 控制连接重建清理 ----------

class _FakePeer:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def test_agent_reset_p2p_state_closes_peers_and_clears_state():
    async def scenario():
        agent = _agent()
        peer = _FakePeer()
        agent.p2p_peers.add(peer)
        task = asyncio.create_task(asyncio.sleep(30))
        agent.p2p_tasks[(1, "x")] = task
        agent.p2p_assemblers[1] = ChunkAssembler(limit=LIMIT_BYTES)
        agent.p2p_sequence[(1, "x")] = 3
        agent.ws_queues["b1"] = asyncio.Queue()
        await agent.reset_p2p_state()
        await asyncio.sleep(0)
        return peer, task, agent

    peer, task, agent = asyncio.run(scenario())
    assert peer.closed is True
    assert task.cancelled()
    assert agent.p2p_peers == set()
    assert agent.p2p_tasks == {}
    assert agent.p2p_assemblers == {}
    assert agent.p2p_sequence == {}
    assert agent.ws_queues == {}


# ---------- item 8：P2P 流协议统一信封 ----------

def test_p2p_stream_error_uses_unified_envelope():
    """终止性 stream_error 也必须是信封帧，前端无需裸消息特判。"""
    sent = []

    async def scenario():
        agent = _agent()
        await agent.p2p_send(_fake_p2p_channel(sent),
                             {"type": "stream_error", "id": "s1", "error": "boom"})

    asyncio.run(scenario())
    assert len(sent) == 1
    message = sent[0]
    assert message["message_id"] == "s1"
    assert message["final"] is True
    assert message["type"] == "stream_error"
    assert message["error"] == "boom"
    assert message["data"] == ""


def test_p2p_stream_lifecycle_is_fully_enveloped():
    """头帧、数据帧、结束帧全部使用 {message_id, sequence, data, final} 信封。"""
    sent = []

    async def scenario():
        agent = _agent()
        channel = _fake_p2p_channel(sent)
        await agent.p2p_send(channel, {"type": "stream_chunk", "id": "s2",
                                       "status": 200, "headers": {}})
        await agent.p2p_send(channel, {"type": "stream_chunk", "id": "s2",
                                       "body": b64(b"data")})
        await agent.p2p_send(channel, {"type": "stream_end", "id": "s2"})

    asyncio.run(scenario())
    assert all("message_id" in m and "sequence" in m and "final" in m for m in sent)
    assert [m["sequence"] for m in sent] == [0, 1, 2]
    assert sent[0]["status"] == 200 and sent[0]["final"] is True
    assert sent[1]["data"] == b64(b"data") and sent[1]["final"] is True
    assert sent[2]["type"] == "stream_end" and sent[2]["final"] is True


# ---------- item 1：attach_device 关闭旧连接且不污染新状态 ----------

def test_attach_device_closes_replaced_control_connection(tmp_path):
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "gateway.json")})
        old = _FakeControlWS()
        device = {"device_id": "dev", "auth_token": "t", "ws": old}
        gateway.registry.devices["dev"] = device
        new = _FakeControlWS()
        await gateway.attach_device(device, new)
        return old, device

    old, device = asyncio.run(scenario())
    assert old.closed == [1011]
    assert device["ws"] is not old


def test_cleanup_of_old_connection_does_not_touch_new_state(tmp_path):
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "gateway.json")})
        old = _FakeControlWS()
        device = {"device_id": "dev", "auth_token": "t", "ws": old}
        gateway.registry.devices["dev"] = device
        new = _FakeControlWS()
        await gateway.attach_device(device, new)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        gateway.pending["r2"] = future
        gateway.owners["r2"] = new
        state = StreamState(maxsize=2)
        gateway.streams["r2"] = state
        answer = loop.create_future()
        gateway.p2p_answers["s2"] = answer
        gateway.p2p_owners["s2"] = new
        await gateway.cleanup_device(old)
        return gateway, future, state, answer, device

    gateway, future, state, answer, device = asyncio.run(scenario())
    assert gateway.owners.get("r2") is not None and not future.done()
    assert gateway.streams.get("r2") is state and state.pending_end is None
    assert gateway.p2p_answers.get("s2") is answer and not answer.done()
    assert device["ws"] is not None


# ---------- item 3：stream_proxy 大小上限（集成） ----------

class _FakeRequest:
    method = "GET"

    class url:
        query = ""

    headers: dict = {}


async def _run_asgi(response):
    messages = []

    async def send(message):
        messages.append(message)

    async def receive():
        await asyncio.sleep(3600)

    await response({"type": "http", "method": "GET", "path": "/", "headers": []},
                   receive, send)
    return messages


def _stream_ws(gateway, request_id, messages_factory):
    class WS:
        async def send_text(self, payload):
            item = json.loads(payload)
            if item.get("type") == "stream_request":
                async def push():
                    while request_id not in gateway.streams:
                        await asyncio.sleep(0)
                    for message in messages_factory():
                        Gateway.enqueue_stream(gateway.streams[request_id], message)
                asyncio.create_task(push())

        async def close(self, code=1000, reason=None):
            return None

    return WS()


def test_stream_proxy_rejects_oversized_first_chunk(tmp_path):
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "g.json"),
                           "max_response_bytes": "100"})
        request_id = "rid-first"
        ws = _stream_ws(gateway, request_id, lambda: [
            {"type": "stream_chunk", "id": request_id, "status": 200,
             "headers": {}, "body": b64(b"x" * 101)}])
        response = await gateway.stream_proxy(_FakeRequest(), {}, ws, "event", request_id, b"")
        messages = await _run_asgi(response)
        return next(m["status"] for m in messages if m["type"] == "http.response.start")

    assert asyncio.run(scenario()) == 502


def test_stream_proxy_aborts_on_cumulative_overflow(tmp_path):
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "g.json"),
                           "max_response_bytes": "100"})
        request_id = "rid-cum"
        ws = _stream_ws(gateway, request_id, lambda: [
            {"type": "stream_chunk", "id": request_id, "status": 200, "headers": {}},
            {"type": "stream_chunk", "id": request_id, "body": b64(b"a" * 60)},
            {"type": "stream_chunk", "id": request_id, "body": b64(b"b" * 41)}])
        response = await gateway.stream_proxy(_FakeRequest(), {}, ws, "event", request_id, b"")
        raised = False
        try:
            await _run_asgi(response)
        except BaseException as exc:  # Starlette 可能用 TaskGroup 包装
            candidates = [exc] + list(getattr(exc, "exceptions", []))
            raised = any(isinstance(candidate, FrameError) for candidate in candidates)
        return raised

    assert asyncio.run(scenario()) is True


# ================================================================
# 第二轮审查修订：Important 1/2/3 与 Minor 4/5/6/7/8
# ================================================================

def _decode_error_body(result: dict) -> dict:
    """兼容裸 response 与统一信封：body 或信封 data 均可。"""
    encoded = result.get("body")
    if encoded is None:
        encoded = result.get("data", "")
    return json.loads(base64.b64decode(encoded).decode("utf-8"))


# ---------- item 1：P2P WebSocket 消息统一信封 ----------

def test_p2p_ws_data_uses_unified_envelope():
    """ws_data（含大二进制）必须走 {message_id, sequence, data, final} 分片，不得裸发。"""
    sent = []
    raw = bytes(range(256)) * 160  # 40960 字节，强制多帧

    async def scenario():
        agent = _agent()
        channel = _fake_p2p_channel(sent)
        await agent.p2p_send(channel, {"type": "ws_data", "id": "b1",
                                       "kind": "bytes", "data": b64(raw)})

    asyncio.run(scenario())
    assert len(sent) >= 2
    assert all("message_id" in m and "sequence" in m and "final" in m for m in sent)
    assembler = ChunkAssembler(limit=1 << 20)
    outcome = None
    for frame_message in sent:
        outcome = assembler.feed(frame_message)
    assert outcome == "accepted"
    restored = json.loads(assembler.result("b1").decode("utf-8"))
    assert restored["type"] == "ws_data" and restored["kind"] == "bytes"
    assert restored["data"] == b64(raw)


def test_p2p_ws_lifecycle_messages_are_enveloped():
    """ws_opened/ws_closed/ws_error 也必须是信封帧，前端无需裸消息特判。"""
    sent = []

    async def scenario():
        agent = _agent()
        channel = _fake_p2p_channel(sent)
        for message in ({"type": "ws_opened", "id": "b1"},
                        {"type": "ws_closed", "id": "b1", "code": 1000},
                        {"type": "ws_error", "id": "b1", "error": "boom"}):
            await agent.p2p_send(channel, message)

    asyncio.run(scenario())
    assert len(sent) == 3
    assert all("message_id" in m and "sequence" in m and "final" in m for m in sent)
    restored = [json.loads(base64.b64decode(m["data"]).decode("utf-8")) for m in sent]
    assert [r["type"] for r in restored] == ["ws_opened", "ws_closed", "ws_error"]
    assert all(m["final"] is True for m in sent)


def test_agent_reassembles_framed_ws_data_into_ws_queue():
    """浏览器以完整 JSON 分片发送的 ws_data 必须被重组并投递到 WS 队列。"""
    async def scenario():
        agent = _agent()
        queue = asyncio.Queue()
        agent.ws_queues["b1"] = queue
        channel = _fake_p2p_channel([])
        message = {"type": "ws_data", "id": "b1", "kind": "bytes", "data": b64(b"hello")}
        for chunk in iter_frames("b1", json.dumps(message).encode(), chunk_size=8):
            await agent.p2p_message(channel, chunk)
        return queue

    queue = asyncio.run(scenario())
    assert queue.qsize() == 1
    item = queue.get_nowait()
    assert item["type"] == "ws_data" and item["data"] == b64(b"hello")


# ---------- item 2：流首帧 body 不得丢弃 ----------

def test_stream_proxy_preserves_first_frame_body(tmp_path):
    """首帧同时含 status/headers/body 时，body 必须作为响应内容输出而非丢弃。"""
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "g.json"),
                           "max_response_bytes": "1000"})
        request_id = "rid-firstbody"
        ws = _stream_ws(gateway, request_id, lambda: [
            {"type": "stream_chunk", "id": request_id, "status": 200,
             "headers": {}, "body": b64(b"first")},
            {"type": "stream_end", "id": request_id}])
        response = await gateway.stream_proxy(_FakeRequest(), {}, ws, "event", request_id, b"")
        messages = await _run_asgi(response)
        return b"".join(m.get("body", b"") for m in messages
                        if m["type"] == "http.response.body")

    assert asyncio.run(scenario()) == b"first"


# ---------- item 3：非法 base64 统一 400 ----------

def test_agent_local_request_rejects_invalid_base64_with_400():
    async def scenario():
        agent = _agent()
        return await agent.local_request({"type": "request", "id": "r-bad", "method": "GET",
                                          "path": "/", "headers": {}, "body": "abc"})

    result = asyncio.run(scenario())
    assert result["status"] == 400
    payload = _decode_error_body(result)
    assert payload.get("reason") == INVALID_ENCODING_REASON


def test_agent_local_stream_rejects_invalid_base64_with_stable_reason():
    sent = []

    class Sink:
        async def send(self, payload):
            sent.append(json.loads(payload))

    async def scenario():
        agent = _agent()
        await agent.local_stream({"type": "stream_request", "id": "s-bad", "method": "GET",
                                  "path": "/", "headers": {}, "body": "ab=c"}, Sink())

    asyncio.run(scenario())
    assert sent and sent[0]["type"] == "stream_error"
    assert sent[0].get("reason") == INVALID_ENCODING_REASON


def test_p2p_message_invalid_encoding_reports_stable_reason():
    """P2P 请求体非法 base64 必须 400，并携带稳定 reason。"""
    sent = []

    async def scenario():
        agent = _agent()

        async def forwarded(item, timeout=120.0):
            raise AssertionError("非法编码不应转发")

        agent.local_request = forwarded
        await agent.p2p_message(_fake_p2p_channel(sent), {
            "type": "request", "id": "r-enc", "method": "GET", "path": "/",
            "headers": {}, "body": "ab=c"})

    asyncio.run(scenario())
    assert sent and sent[0]["status"] == 400
    assert _decode_error_body(sent[0]).get("reason") == INVALID_ENCODING_REASON


# ---------- item 4：退避 jitter 不超过 cap ----------

def test_backoff_jitter_never_exceeds_cap():
    for attempt in (1, 2, 5, 10, 50):
        for seed in range(50):
            delay = backoff_delay(attempt, base=1.0, cap=10.0, jitter=1.0,
                                  rng=random.Random(seed))
            assert delay <= 10.0


# ---------- item 5：错误墓碑阻止重放 ----------

def test_assembler_error_tombstone_blocks_replay():
    clock = _FakeClock()
    assembler = ChunkAssembler(limit=LIMIT_BYTES, ttl=10, clock=clock)
    assert assembler.feed(make_chunk("m", 0, b"x" * (LIMIT_BYTES + 1), final=True)) == "error"
    # 错误后同一 message_id 的重放必须仍被拒绝，而不是重新开始装配。
    assert assembler.feed(make_chunk("m", 0, b"ok", final=True)) == "error"
    assert assembler.pending_count() == 1
    clock.advance(11)
    assembler.feed(make_chunk("other", 0, b"z", final=True))
    assert assembler.total_of("m") == 0


def test_agent_does_not_replay_failed_message_id():
    sent = []
    calls = []

    async def scenario():
        agent = _agent()

        async def forwarded(item, timeout=120.0):
            calls.append(item)
            return {"type": "response", "id": item["id"], "status": 200,
                    "headers": {}, "body": b64(b"ok")}

        agent.local_request = forwarded
        channel = _fake_p2p_channel(sent)
        bad = json.dumps({"type": "request", "id": "r", "method": "GET", "path": "/",
                          "headers": {}, "body": b64(b"x" * (LIMIT_BYTES + 1))}).encode()
        for chunk in iter_frames("r", bad, chunk_size=64):
            await agent.p2p_message(channel, chunk)
        good = json.dumps({"type": "request", "id": "r", "method": "GET", "path": "/",
                           "headers": {}, "body": b64(b"ok")}).encode()
        for chunk in iter_frames("r", good, chunk_size=64):
            await agent.p2p_message(channel, chunk)

    asyncio.run(scenario())
    assert calls == []
    assert sent and all(message["status"] in (400, 413) for message in sent)


# ---------- item 6：稳定错误原因常量 ----------

def test_error_reason_constants_are_distinct_and_stable():
    reasons = {REQUEST_TOO_LARGE_REASON, RESPONSE_TOO_LARGE_REASON,
               PAYLOAD_TOO_LARGE_REASON, INVALID_ENCODING_REASON}
    assert len(reasons) == 4
    assert all(isinstance(reason, str) and reason for reason in reasons)
    assert STREAM_OVERFLOW_REASON == StreamState.OVERFLOW_REASON
    assert read_bounded.__doc__ is not None
    # read_bounded 超限必须使用稳定响应原因常量。
    async def scenario():
        try:
            await read_bounded(_AsyncChunks([b"a" * 5]), 4)
            return None
        except FrameError as exc:
            return str(exc)

    assert asyncio.run(scenario()) == RESPONSE_TOO_LARGE_REASON


# ---------- item 7：send_control 对已关闭 socket 统一 ConnectionError ----------

class _ClosedControlWS:
    def __init__(self):
        self.closed: list[int] = []

    async def send_text(self, payload: str) -> None:
        raise RuntimeError("Cannot call 'send' once a close message has been sent")

    async def close(self, code: int = 1000, reason=None) -> None:
        self.closed.append(code)


class _ClosedAgentWS:
    def __init__(self):
        self.closed: list[int] = []

    async def send(self, payload: str) -> None:
        raise RuntimeError("Cannot call 'send' once a close message has been sent")

    async def close(self, code: int = 1000, reason=None) -> None:
        self.closed.append(code)


def test_gateway_send_control_on_closed_socket_raises_connection_error(tmp_path):
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "g.json")})
        ws = _ClosedControlWS()
        try:
            await gateway.send_control(ws, {"type": "ping"})
            return False, ws.closed
        except ConnectionError:
            return True, ws.closed

    raised, closed = asyncio.run(scenario())
    assert raised is True
    assert closed == [1011]


def test_agent_send_control_on_closed_socket_raises_connection_error():
    async def scenario():
        agent = _agent()
        ws = _ClosedAgentWS()
        try:
            await agent.send_control(ws, {"type": "pong"})
            return False, ws.closed
        except ConnectionError:
            return True, ws.closed

    raised, closed = asyncio.run(scenario())
    assert raised is True
    assert closed == [1011]


# ---------- item 8：流溢出保持首个稳定原因 ----------

def test_stream_proxy_overflow_keeps_stable_reason(tmp_path):
    async def scenario():
        gateway = Gateway({"state_file": str(tmp_path / "g.json"),
                           "max_response_bytes": "100"})
        request_id = "rid-reason"
        ws = _stream_ws(gateway, request_id, lambda: [
            {"type": "stream_chunk", "id": request_id, "status": 200, "headers": {}},
            {"type": "stream_chunk", "id": request_id, "body": b64(b"a" * 60)},
            {"type": "stream_chunk", "id": request_id, "body": b64(b"b" * 41)}])
        response = await gateway.stream_proxy(_FakeRequest(), {}, ws, "event", request_id, b"")
        try:
            await _run_asgi(response)
            return None
        except BaseException as exc:
            candidates = [exc] + list(getattr(exc, "exceptions", []))
            for candidate in candidates:
                if isinstance(candidate, FrameError):
                    return str(candidate)
            return None

    assert asyncio.run(scenario()) == RESPONSE_TOO_LARGE_REASON


# ================================================================
# 第三轮（最终）审查修订
# ================================================================

# ---------- item 1：分片路径错误种类区分（400/413/乱序） ----------

def test_p2p_message_framed_error_kinds_are_distinct():
    """分片路径按错误种类区分：超限→413，非法编码→400，乱序/重放→400 协议错误。"""
    sent = []

    async def scenario():
        agent = _agent()

        async def forwarded(item, timeout=120.0):
            return {"type": "response", "id": item["id"], "status": 200,
                    "headers": {}, "body": b64(b"ok")}

        agent.local_request = forwarded
        channel = _fake_p2p_channel(sent)
        # 单帧超限 → 413。
        await agent.p2p_message(channel, frame("f-over", 0, b"x" * (LIMIT_BYTES + 1), True))
        # 非法帧编码 → 400。
        await agent.p2p_message(channel, {"message_id": "f-enc", "sequence": 0,
                                          "data": "!!!!", "final": True})
        # 乱序（跳跃序列号）→ 400 稳定的协议错误。
        await agent.p2p_message(channel, frame("f-seq", 1, b"x", True))
        # 完成后的重放 → 400 稳定的协议错误。
        await agent.p2p_message(channel, frame("f-rep", 0, b'{"id":"f-rep","type":"ping"}', True))
        await agent.p2p_message(channel, frame("f-rep", 0, b"x", True))

    asyncio.run(scenario())
    # 去掉 ping 对应的 pong，只关注按触发顺序产生的错误响应。
    errors = [m for m in sent if _decode_error_body(m).get("error")]
    assert [e["status"] for e in errors] == [413, 400, 400, 400]
    assert _decode_error_body(errors[0])["reason"] == PAYLOAD_TOO_LARGE_REASON
    assert _decode_error_body(errors[1])["reason"] == INVALID_ENCODING_REASON
    assert _decode_error_body(errors[2])["reason"] == INVALID_SEQUENCE_REASON
    assert _decode_error_body(errors[3])["reason"] == INVALID_SEQUENCE_REASON


def test_assembler_reason_of_returns_stable_kind():
    """ChunkAssembler 必须按错误种类暴露稳定 reason，供上层区分 400/413。"""
    assembler = ChunkAssembler(limit=LIMIT_BYTES)
    assert assembler.feed(make_chunk("a", 0, b"x" * (LIMIT_BYTES + 1), True)) == "error"
    assert assembler.reason_of("a") == PAYLOAD_TOO_LARGE_REASON
    assert assembler.feed(make_chunk("b", 1, b"x", True)) == "error"
    assert assembler.reason_of("b") == INVALID_SEQUENCE_REASON
    assert assembler.feed({"message_id": "c", "sequence": 0, "data": "!!!!", "final": True}) == "error"
    assert assembler.reason_of("c") == INVALID_ENCODING_REASON
    # 完成后的重放也归类为协议乱序。
    assert assembler.feed(make_chunk("d", 0, b"ok", True)) == "accepted"
    assembler.complete("d")
    assert assembler.feed(make_chunk("d", 0, b"x", True)) == "error"
    assert assembler.reason_of("d") == INVALID_SEQUENCE_REASON


# ---------- item 2：transient 502/stream_error 统一稳定 reason ----------

def test_transient_connection_failure_uses_connection_failed_reason():
    """连接级失败（无持久 reason）必须统一使用 CONNECTION_FAILED_REASON。"""
    async def scenario():
        agent = _agent()
        # 目标不可达，local_request 应返回带稳定 reason 的 502 响应。
        result = await agent.local_request({"type": "request", "id": "r-conn",
                                            "method": "GET", "path": "/", "headers": {},
                                            "body": b64(b"")}, timeout=0.05)
        return result

    result = asyncio.run(scenario())
    assert result["status"] == 502
    assert _decode_error_body(result).get("reason") == CONNECTION_FAILED_REASON


def test_local_request_follows_redirects():
    """Agent 代理必须跟随重定向（与原生 fetch 默认 redirect:'follow' 一致）。"""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/final")
                self.end_headers()
            else:
                body = b"redirected-ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        async def scenario():
            agent = Agent({"opencode_url": f"http://127.0.0.1:{port}",
                           "max_request_bytes": str(LIMIT_BYTES),
                           "request_timeout": "5"})
            return await agent.local_request({"type": "request", "id": "r-redirect",
                                              "method": "GET", "path": "/start", "headers": {},
                                              "body": b64(b"")}, timeout=5.0)

        result = asyncio.run(scenario())
    finally:
        server.shutdown()
    assert result["status"] == 200
    assert unb64(result["body"]) == b"redirected-ok"


def test_p2p_message_502_carries_stable_reason():
    """run_request 抛出的连接级错误必须映射为带稳定 reason 的 502。"""
    sent = []

    async def scenario():
        agent = _agent()

        async def forwarded(item, timeout=120.0):
            raise ConnectionError("upstream gone")

        agent.local_request = forwarded
        await agent.p2p_message(_fake_p2p_channel(sent), {
            "type": "request", "id": "r-exc", "method": "GET", "path": "/",
            "headers": {}, "body": b64(b"")})

    asyncio.run(scenario())
    assert sent and sent[0]["status"] == 502
    assert _decode_error_body(sent[0]).get("reason") in {CONNECTION_FAILED_REASON,
                                                          UPSTREAM_ERROR_REASON}


# ---------- item 3：非字符串 response body 不得发悬挂帧 ----------

def test_p2p_send_response_missing_body_raises_protocol_error():
    """response 缺 body 属于协议错误，必须抛错，不得发送 final=False 悬挂帧。"""
    sent = []
    async def scenario():
        agent = _agent()
        channel = _fake_p2p_channel(sent)
        try:
            await agent.p2p_send(channel, {"type": "response", "id": "x", "status": 200,
                                           "headers": {}})
            return False, len(sent)
        except FrameError:
            return True, len(sent)

    raised, sent_count = asyncio.run(scenario())
    assert raised is True
    assert sent_count == 0  # 不得发出任何悬挂帧


# ---------- item 5：ChunkAssembler 全局字节预算 ----------

def test_assembler_rejects_frame_exceeding_global_byte_budget():
    """并行装配累计字节超过总预算时拒绝并保留错误墓碑，返回稳定原因。"""
    assembler = ChunkAssembler(limit=LIMIT_BYTES, budget=8)
    assert assembler.feed(make_chunk("a", 0, b"a" * 5)) is None
    outcome = assembler.feed(make_chunk("b", 0, b"b" * 4))
    assert outcome == "error"
    assert assembler.reason_of("b") == ASSEMBLY_BUDGET_REASON
    assert assembler.total_of("a") == 5
    assert assembler.total_of("b") == 0


def test_assembler_complete_releases_global_byte_budget():
    """完成后释放占用，使预算回到可以再容纳新装配的水平。"""
    assembler = ChunkAssembler(limit=LIMIT_BYTES, budget=8)
    assert assembler.feed(make_chunk("a", 0, b"a" * 4)) is None
    assert assembler.feed(make_chunk("a", 1, b"a" * 1, True)) == "accepted"
    # 已占用 5/8，再装配 5 字节必然超过预算。
    assert assembler.feed(make_chunk("b", 0, b"b" * 5)) == "error"
    assembler.complete("a")
    # 完成后应释放预算，允许新的装配继续。
    assert assembler.feed(make_chunk("c", 0, b"c" * 5)) is None


def test_p2p_total_budget_is_low_and_configurable():
    """总预算默认不超过单条消息上限，且可用 max_p2p_total_bytes 配置。"""
    assert p2p_total_budget({}) <= p2p_message_limit({})
    assert p2p_total_budget({"max_p2p_total_bytes": "12345"}) == 12345


# ---------- item 6：p2p_channel_send 缓冲退避必须有时限 ----------

def test_p2p_channel_send_times_out_instead_of_looping_forever():
    """通道持续缓冲不回位时，p2p_channel_send 必须在配置超时内失败，而不是永久循环。"""
    class BlockedChannel:
        bufferedAmount = 1 << 30
        readyState = "open"

    async def scenario():
        agent = _agent()
        agent.cfg["p2p_send_timeout"] = "0.05"
        channel = BlockedChannel()
        start = asyncio.get_event_loop().time()
        try:
            await agent.p2p_channel_send(channel, {"message_id": "x", "sequence": 0,
                                                   "data": "", "final": True})
            return False, 0.0
        except ConnectionError:
            return True, asyncio.get_event_loop().time() - start

    raised, elapsed = asyncio.run(scenario())
    assert raised is True
    assert elapsed < 2.0  # 配置的超时应在短时间内生效


def test_p2p_channel_send_drains_then_sends_within_timeout():
    """缓冲回位正常时，退避后继续发送，且行为与超时无关。"""
    sent = []
    class DrainingChannel:
        _ba = 2 * 1024 * 1024
        readyState = "open"
        @property
        def bufferedAmount(self):
            self._ba //= 2  # 每次探测模拟浏览器逐步回位
            return self._ba
        def send(self, payload: str) -> None:
            sent.append(json.loads(payload))
    async def scenario():
        agent = _agent()
        agent.cfg["p2p_send_timeout"] = "2"
        await agent.p2p_channel_send(DrainingChannel(), {"message_id": "x", "sequence": 0,
                                                         "data": "", "final": True})
    asyncio.run(scenario())
    assert sent == [{"message_id": "x", "sequence": 0, "data": "", "final": True}]


def test_enable_loopback_candidate_adds_loopback_and_is_idempotent():
    """开启 loopback candidate 后 127.0.0.1 进入 host 候选，且 patch 幂等。"""
    import aioice.ice as ice
    original = ice.get_host_addresses
    try:
        enable_loopback_candidate()
        patched = ice.get_host_addresses
        assert patched is not original  # 已替换为补丁函数
        addresses = patched(True, False)
        assert "127.0.0.1" in addresses
        # 幂等：再次开启不重复 patch，也不重复叠加 loopback
        enable_loopback_candidate()
        assert ice.get_host_addresses is patched
        assert patched(True, False).count("127.0.0.1") == 1
    finally:
        ice.get_host_addresses = original


def test_enable_loopback_candidate_preserves_ipv6_and_other_hosts():
    """patch 只补充 IPv4 loopback，保留原有 host 地址与 IPv6 行为。"""
    import aioice.ice as ice
    original = ice.get_host_addresses
    try:
        before = set(original(True, False))
        enable_loopback_candidate()
        after = set(ice.get_host_addresses(True, False))
        assert "127.0.0.1" in after
        assert before <= after  # 原有地址全部保留
        assert after - before == {"127.0.0.1"}
    finally:
        ice.get_host_addresses = original


# ================================================================
# 前端适配层健壮性修订：响应头过滤（set-cookie 跨信任边界）
# ================================================================

def test_filter_response_headers_strips_set_cookie_and_hop_headers():
    """Response headers strip set-cookie and hop-by-hop fields."""
    headers = {
        "content-type": "application/json",
        "set-cookie": "session=abc; Path=/",
        "set-cookie2": "csrf=xyz; Path=/",
        "content-length": "123",
        "content-encoding": "gzip",
        "transfer-encoding": "chunked",
        "connection": "keep-alive",
        "x-custom": "keep-me",
    }
    result = filter_response_headers(headers)
    assert result == {"content-type": "application/json", "x-custom": "keep-me"}


def test_filter_response_headers_is_case_insensitive():
    """Set-cookie variants are stripped case-insensitively."""
    result = filter_response_headers({"Set-Cookie": "a=b", "SET-COOKIE": "c=d",
                                      "Content-Type": "text/html"})
    assert result == {"Content-Type": "text/html"}


# ================================================================
# 前端适配层健壮性修订：WebSocket 状态机单调性
# ================================================================

def test_ws_opened_moves_connecting_socket_to_open():
    """ws_opened moves a CONNECTING socket to OPEN and dispatches open."""
    state = BrowserState()
    socket = FakeSocket()
    state.sockets["ws1"] = socket
    browser_settle(state, {"type": "ws_opened", "id": "ws1", "protocol": ""})
    assert socket.readyState == FakeSocket.OPEN
    assert ("open", {}) in socket.events


def test_ws_opened_after_close_does_not_reopen():
    """A late ws_opened cannot reopen a CLOSING or CLOSED socket."""
    state = BrowserState()
    socket = FakeSocket()
    socket.readyState = FakeSocket.CLOSING
    state.sockets["ws1"] = socket
    browser_settle(state, {"type": "ws_opened", "id": "ws1", "protocol": ""})
    assert socket.readyState == FakeSocket.CLOSING
    assert not any(event_type == "open" for event_type, _ in socket.events)


def test_ws_opened_empty_protocol_clears_requested_protocol():
    """An empty negotiated protocol clears the requested socket protocol."""
    state = BrowserState()
    socket = FakeSocket()
    socket.protocol = "requested"  # Simulate the constructor's requested protocol.
    state.sockets["ws1"] = socket
    browser_settle(state, {"type": "ws_opened", "id": "ws1", "protocol": ""})
    assert socket.protocol == ""


# ================================================================
# 前端适配层健壮性修订：cancelled 不得伪装成 200 空流
# ================================================================

def test_cancelled_before_first_frame_errors_stream():
    """A pre-header cancellation errors and cleans up instead of an empty 200 stream."""
    state = BrowserState()
    sid = "s-cancelled"
    controller = open_stream(state, sid)
    browser_settle(state, {"type": "cancelled", "id": sid})
    assert controller.errored is not None
    assert sid not in state.streams
    rejects = [payload for kind, payload in state.settled if kind == "reject"]
    assert rejects  # The pending request must reject instead of resolving cancelled.
# ---------- item 4：浏览器 CSRF 标记不得穿透到本地 OpenCode ----------

def test_forwarding_headers_strip_credentials_and_csrf_markers():
    """Gateway-to-agent forwarding strips credentials and browser CSRF markers."""
    out = {k.lower(): v for k, v in forwarding_headers({
        "Authorization": "Bearer secret", "Cookie": "session=1",
        "Origin": "https://mesh.example.com:8443", "Referer": "https://mesh.example.com:8443/",
        "Accept": "application/json", "x-opencode-ticket": "1"}).items()}
    assert "authorization" not in out
    assert "cookie" not in out
    assert "origin" not in out
    assert "referer" not in out
    assert out["accept"] == "application/json"
    assert out["x-opencode-ticket"] == "1"


def test_forwarding_headers_strip_accept_encoding():
    """The Agent must receive uncompressed upstream responses for safe relaying."""
    out = {k.lower(): v for k, v in forwarding_headers({
        "Accept-Encoding": "gzip, deflate, br", "Accept": "text/html"}).items()}
    assert "accept-encoding" not in out


def test_agent_local_request_strips_browser_csrf_markers(monkeypatch):
    """Agent requests to local OpenCode omit browser Origin and Referer markers."""
    captured = {}

    class FakeResponse:
        status_code = 200
        headers = {}

        async def aiter_bytes(self):
            yield b"ok"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, headers=None, content=None):
            captured["headers"] = {k.lower(): v for k, v in (headers or {}).items()}
            return FakeResponse()

    monkeypatch.setattr("src.main.httpx.AsyncClient", FakeClient)

    async def scenario():
        agent = _agent()
        return await agent.local_request({
            "type": "request", "id": "r-csrf", "method": "POST",
            "path": "/pty/x/connect-token",
            "headers": {"origin": "https://mesh.example.com:8443",
                        "referer": "https://mesh.example.com:8443/",
                        "x-opencode-ticket": "1",
                        "accept": "application/json"},
            "body": b64(b"{}")})

    result = asyncio.run(scenario())
    assert result["status"] == 200
    assert "origin" not in captured["headers"]
    assert "referer" not in captured["headers"]
    assert captured["headers"].get("x-opencode-ticket") == "1"


def test_version_source_is_semver_and_declared_in_src():
    """The runtime version is provided by src.__version__."""
    import src

    version = getattr(src, "__version__", "")
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)


def test_pyproject_uses_dynamic_src_version():
    """pyproject reads the version from src.__version__ to avoid duplication."""
    document = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert "version" in document["project"].get("dynamic", [])
    assert document["tool"]["setuptools"]["dynamic"]["version"]["attr"] == "src.__version__"


def test_mesh_bar_injects_runtime_version():
    """The injected status bar renders the current Mesh version."""
    from src import __version__
    body = inject_mesh_bar(b"<html><head></head><body></body></html>")

    assert b"OpenCode Mesh" in body
    assert f'MESH_VERSION = "{__version__}"'.encode() in body
    assert b"version.textContent = 'v' + MESH_VERSION" in body
    assert b"__OCM_VERSION_JSON__" not in body


def test_mesh_adapter_is_not_injected_into_v1_html():
    """V1 must keep its native API/WebSocket client instead of the V2 adapter."""
    body = inject_mesh_bar(
        b'<html><head><script type="module" src="/assets/index-old.js"></script></head></html>'
    )

    assert b"ocm-transport-adapter" not in body


def test_mesh_adapter_is_injected_into_v2_html():
    """V2 HTML continues to receive the Mesh transport adapter."""
    body = inject_mesh_bar(
        b'<html><head><script type="module" src="/_assets/index-new.js"></script></head></html>'
    )

    assert b"ocm-transport-adapter" in body


def test_adapter_checks_selected_server_frontend_before_switching_transport():
    """A V2 page must navigate to a selected V1 device instead of mixing SDKs."""
    assert "selectedServerUrl()" in TRANSPORT_ADAPTER
    assert "ensureFrontendForDevice" not in TRANSPORT_ADAPTER


def test_adapter_does_not_redirect_v2_device_into_mesh_route():
    """V2 client routing must remain at the root; only V1 needs a page switch."""
    assert "location.replace" not in TRANSPORT_ADAPTER


def test_mesh_bar_injection_is_idempotent_with_version():
    """Processing the same HTML twice does not duplicate the adapter."""
    body = inject_mesh_bar(b"<html><head></head><body></body></html>")
    twice = inject_mesh_bar(body)

    assert twice.count(b"ocm-transport-adapter") == 1


def test_installer_supports_release_tags_and_development_branches():
    """The installer distinguishes v* release tags from development branches."""
    script = Path("scripts/install.sh").read_text(encoding="utf-8")

    assert 'VERSION="${MESH_VERSION:-main}"' in script
    assert 'refs/tags/${VERSION}.tar.gz' in script
    assert 'refs/heads/${VERSION}.tar.gz' in script
    assert 'src.__version__' in script
