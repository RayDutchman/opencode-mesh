from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import string
import time
from typing import Any, AsyncIterator, Awaitable, Callable


# Bounded protocol constants: control frame timeout, request/response limits, chunk size.
CONTROL_SEND_TIMEOUT = 5.0
MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
# Backward-compatible config name: only an explicit max_p2p_message_bytes overrides; otherwise inherit the request limit.
MAX_P2P_MESSAGE_BYTES = 1024 * 1024
CHUNK_SIZE = 32768
# Stable error reasons shared by Relay and P2P; the frontend uses reason to decide whether a retry is possible.
REQUEST_TOO_LARGE_REASON = "request exceeds max_request_bytes limit"
RESPONSE_TOO_LARGE_REASON = "response exceeds max_response_bytes limit"
PAYLOAD_TOO_LARGE_REASON = "reassembled payload exceeds max_p2p_message_bytes limit"
INVALID_ENCODING_REASON = "invalid base64 encoding"
STREAM_OVERFLOW_REASON = "stream buffer overflow"
# Protocol errors on the fragmented path (out-of-order, replay) and buffer exhaustion
# are classified by separate stable constants.
INVALID_SEQUENCE_REASON = "invalid frame sequence"
ASSEMBLY_BUDGET_REASON = "reassembly byte budget exceeded"
# Upstream failures: connection-level failures and other transport errors are classified
# separately to avoid misleading callers.
CONNECTION_FAILED_REASON = "connection failed"
UPSTREAM_ERROR_REASON = "upstream request failed"
WS_FRAME_FLOOR_BYTES = 16 * 1024 * 1024

_B64_ALPHABET = frozenset(string.ascii_letters + string.digits + "+/")


class P2PUnavailable(RuntimeError):
    """WebRTC implementation is not installed in this environment."""


class FrameError(RuntimeError):
    """A fragment envelope violated the bounded reassembly contract (overflow, out-of-order, duplicate, or invalid encoding)."""


def request_limit(cfg: dict[str, Any]) -> int:
    """Sole accessor for the effective request body limit (default 64MiB)."""
    return int(cfg.get("max_request_bytes", MAX_REQUEST_BYTES))


def response_limit(cfg: dict[str, Any]) -> int:
    """Sole accessor for the effective response body limit (default 64MiB)."""
    return int(cfg.get("max_response_bytes", MAX_RESPONSE_BYTES))


def p2p_message_limit(cfg: dict[str, Any]) -> int:
    """Per-message P2P limit; without explicit config it matches the request limit so Relay/P2P do not diverge."""
    if cfg.get("max_p2p_message_bytes") is not None:
        return int(cfg["max_p2p_message_bytes"])
    return request_limit(cfg)


def p2p_total_budget(cfg: dict[str, Any]) -> int:
    """Global cap on the cumulative buffered bytes of all parallel chunk assemblies.

    Defaults to the per-message limit (avoiding a 64MiB x max_assemblies x peer
    amplification); max_p2p_total_bytes can tighten or loosen it explicitly.
    """
    if cfg.get("max_p2p_total_bytes") is not None:
        return int(cfg["max_p2p_total_bytes"])
    return p2p_message_limit(cfg)


def ws_frame_limit(cfg: dict[str, Any]) -> int:
    """uvicorn ws_max_size: covers the application limit (base64 expansion + JSON overhead), never below 16MiB."""
    return max(WS_FRAME_FLOOR_BYTES, request_limit(cfg) * 2)


def is_valid_base64(encoded: str) -> bool:
    """Validate standard base64 charset and padding strictly, without allocating a decoded result."""
    if encoded == "":
        return True
    if not isinstance(encoded, str) or len(encoded) % 4 != 0:
        return False
    if encoded.endswith("=="):
        padding = 2
    elif encoded.endswith("="):
        padding = 1
    else:
        padding = 0
    core = encoded[:len(encoded) - padding] if padding else encoded
    if "=" in core:
        return False
    return all(char in _B64_ALPHABET for char in core)


def decode_strict(encoded: str) -> bytes:
    """Strictly decode standard base64; invalid characters, malformed padding, or bad length raise ValueError."""
    return base64.b64decode(encoded or "", validate=True)


class ResponseSizeGuard:
    """Accumulate decoded bytes and raise a stable FrameError when the limit is exceeded.

    Use decoded_size before base64 expansion so oversized responses never build a full copy.
    """

    def __init__(self, limit: int):
        self.limit = int(limit)
        self.total = 0

    def add_encoded(self, encoded: str) -> int:
        if not is_valid_base64(encoded):
            raise FrameError(INVALID_ENCODING_REASON)
        size = decoded_size(encoded)
        if self.total + size > self.limit:
            raise FrameError(RESPONSE_TOO_LARGE_REASON)
        self.total += size
        return size

    def add_bytes(self, raw: bytes) -> int:
        if self.total + len(raw) > self.limit:
            raise FrameError(RESPONSE_TOO_LARGE_REASON)
        self.total += len(raw)
        return len(raw)


def decoded_size(encoded: str) -> int:
    """Compute the exact decoded byte count of a standard base64 string without allocating.

    Used to judge size limits before base64 expansion: encoded length alone cannot
    distinguish 3-byte boundaries (e.g. 8192 and 8193 bytes encode to the same length).
    """
    if not encoded:
        return 0
    full, remainder = divmod(len(encoded), 4)
    if encoded.endswith("=="):
        padding = 2
    elif encoded.endswith("="):
        padding = 1
    else:
        padding = 0
    if remainder:
        return full * 3 + (remainder - 1)
    return full * 3 - padding


def frame(message_id: str, sequence: int, data: bytes, final: bool) -> dict[str, Any]:
    """Build the uniform fragment envelope {message_id, sequence, data, final}."""
    return {"message_id": str(message_id), "sequence": int(sequence),
            "data": base64.b64encode(data or b"").decode("ascii"), "final": bool(final)}


def iter_frames(message_id: str, data: bytes, chunk_size: int = CHUNK_SIZE):
    """Split bytes into fragment envelopes; the last envelope has final=True (empty data still yields one frame)."""
    payload = data or b""
    if len(payload) <= chunk_size:
        yield frame(message_id, 0, payload, True)
        return
    sequence = 0
    for offset in range(0, len(payload), chunk_size):
        piece = payload[offset:offset + chunk_size]
        yield frame(message_id, sequence, piece, offset + chunk_size >= len(payload))
        sequence += 1


class ChunkAssembler:
    """Bounded chunk reassembler isolated per message_id.

    Contract: sequences for the same message_id must increase strictly from 0;
    duplicates, gaps, overflow, repeated final, and invalid encoding all produce
    a bounded error state, and different message_ids never interfere.

    Resource bounds: incomplete assemblies are constrained by `max_assemblies`
    (count, evicts the oldest) and `ttl` (time, purged when expired); the
    cumulative buffered bytes of all parallel assemblies are constrained by the
    global `budget` (defaults to the per-message limit), exceeded by returning
    the stable ASSEMBLY_BUDGET_REASON error; `trace` is for tests/debugging only
    and defaults to off so it cannot grow unboundedly.
    """

    def __init__(self, limit: int = MAX_RESPONSE_BYTES, trace: bool = False,
                 max_assemblies: int = 64, ttl: float | None = 300.0,
                 clock: Callable[[], float] | None = None,
                 budget: int | None = None):
        self.limit = int(limit)
        self.trace_enabled = bool(trace)
        self.max_assemblies = max(1, int(max_assemblies))
        self.ttl = ttl
        self.clock = clock or time.monotonic
        # Global byte budget: cap on the sum of buffered bytes across parallel assemblies (genuinely low and globally bounded).
        self.budget = int(budget) if budget is not None else int(limit)
        self._used_bytes = 0
        self._assemblies: dict[str, dict[str, Any]] = {}
        # Arrival frame trace: (message_id, sequence), recorded only while trace_enabled.
        self.trace: list[tuple[str | None, int | None]] = []

    def _purge(self, now: float, incoming: str | None = None) -> None:
        if self.ttl is not None:
            expired = [key for key, asm in self._assemblies.items()
                       if now - asm["updated"] > self.ttl]
            for key in expired:
                self._used_bytes -= self._assemblies.pop(key, {})["total"]
        if incoming is not None and incoming not in self._assemblies:
            while len(self._assemblies) >= self.max_assemblies:
                oldest = min(self._assemblies, key=lambda key: self._assemblies[key]["updated"])
                self._used_bytes -= self._assemblies.pop(oldest)["total"]

    def _free(self, asm: dict[str, Any]) -> None:
        """Release this assembly's buffered bytes and remove them from the global budget."""
        self._used_bytes -= asm["total"]
        asm["total"] = 0
        asm["parts"] = []

    def feed(self, chunk: dict[str, Any]) -> str | None:
        """Feed one frame; return "accepted"/"error", or None (keep waiting)."""
        message_id = chunk.get("message_id")
        sequence = chunk.get("sequence")
        if self.trace_enabled:
            self.trace.append((message_id, sequence))
        now = self.clock()
        self._purge(now, incoming=message_id)
        asm = self._assemblies.setdefault(
            message_id, {"parts": [], "total": 0, "accepted": False, "error": None,
                         "reason": None, "next": 0, "updated": now})
        asm["updated"] = now
        # Error/completed tombstones last until TTL: replays of the same message_id are always rejected, never reassembled.
        if asm["error"] is not None:
            return "error"
        if asm["accepted"]:
            asm["error"] = "frame after completion"
            asm["reason"] = INVALID_SEQUENCE_REASON
            self._free(asm)
            return "error"
        if sequence != asm["next"]:
            asm["error"] = f"unexpected sequence {sequence}, expected {asm['next']}"
            asm["reason"] = INVALID_SEQUENCE_REASON
            self._free(asm)
            return "error"
        encoded = chunk.get("data") or ""
        if not is_valid_base64(encoded):
            asm["error"] = INVALID_ENCODING_REASON
            asm["reason"] = INVALID_ENCODING_REASON
            self._free(asm)
            return "error"
        # Check the limit against the decoded length first, then decode, so oversized fragments never build a full copy.
        size = decoded_size(encoded)
        if asm["total"] + size > self.limit:
            asm["error"] = PAYLOAD_TOO_LARGE_REASON
            asm["reason"] = PAYLOAD_TOO_LARGE_REASON
            self._free(asm)
            return "error"
        # Global byte budget: when this fragment would push the cumulative buffer past the total budget, reject with a stable error.
        if self._used_bytes + size > self.budget:
            asm["error"] = ASSEMBLY_BUDGET_REASON
            asm["reason"] = ASSEMBLY_BUDGET_REASON
            self._free(asm)
            return "error"
        asm["parts"].append(decode_strict(encoded))
        asm["total"] += size
        self._used_bytes += size
        asm["next"] = sequence + 1
        if chunk.get("final"):
            asm["accepted"] = True
            return "accepted"
        return None

    def complete(self, message_id: str) -> None:
        """Mark the assembly complete and release its fragment memory, but keep a tombstone until TTL to block same-id replays.

        Callers must read result() first; later frames for this message_id are treated as post-completion replays.
        """
        asm = self._assemblies.get(message_id)
        if asm is None:
            return
        self._free(asm)
        asm["accepted"] = True

    def discard(self, message_id: str) -> None:
        asm = self._assemblies.pop(message_id, None)
        if asm is not None:
            self._used_bytes -= asm["total"]

    def pending_count(self) -> int:
        """Number of incomplete/unreleased assemblies, used for resource-boundary assertions."""
        return len(self._assemblies)

    def result(self, message_id: str) -> bytes:
        asm = self._assemblies.get(message_id)
        if not asm or asm.get("error") is not None or not asm.get("accepted"):
            return b""
        return b"".join(asm["parts"])

    def total_of(self, message_id: str) -> int:
        return self._assemblies.get(message_id, {}).get("total", 0)

    def is_accepted(self, message_id: str) -> bool:
        return bool(self._assemblies.get(message_id, {}).get("accepted"))

    def error_of(self, message_id: str):
        return self._assemblies.get(message_id, {}).get("error")

    def reason_of(self, message_id: str):
        """Return the stable error kind for this message_id (so callers can distinguish 400/413)."""
        return self._assemblies.get(message_id, {}).get("reason")


async def read_bounded(chunks: AsyncIterator[bytes], limit: int) -> bytes:
    """Stream the response body and fail immediately past the limit, avoiding an oversized copy."""
    buffer = bytearray()
    async for chunk in chunks:
        if not chunk:
            continue
        if len(buffer) + len(chunk) > limit:
            raise FrameError(RESPONSE_TOO_LARGE_REASON)
        buffer.extend(chunk)
    return bytes(buffer)


def load_aiortc():
    try:
        from aiortc import RTCPeerConnection, RTCSessionDescription
    except ImportError as exc:
        raise P2PUnavailable("aiortc is not installed, falling back to Relay") from exc
    return RTCPeerConnection, RTCSessionDescription


def enable_loopback_candidate() -> None:
    """Make aiortc/aioice publish 127.0.0.1 as a host candidate.

    aioice's get_host_addresses excludes loopback by default (ip.ip != "127.0.0.1").
    In WSL mirrored and similar setups the host browser can only reach this Agent
    over the shared loopback; the other host candidates (LAN/Docker/link-local)
    are unreachable from the host, so ICE would always fail and fall back to Relay.
    This adds 127.0.0.1 in-process and idempotently.

    Harmless for remote browsers: there 127.0.0.1 points at the browser itself, so
    the candidate is just one more inevitably failing pair and other candidates
    are unaffected.
    """
    try:
        import aioice.ice as ice
    except ImportError:
        return
    original = ice.get_host_addresses
    if getattr(original, "_ocm_loopback_patched", False):
        return

    def patched(use_ipv4: bool, use_ipv6: bool) -> list[str]:
        addresses = list(original(use_ipv4, use_ipv6))
        if use_ipv4 and "127.0.0.1" not in addresses:
            addresses.append("127.0.0.1")
        return addresses

    patched._ocm_loopback_patched = True
    ice.get_host_addresses = patched


async def wait_ice_complete(peer: Any, timeout: float = 15) -> None:
    async def wait():
        while peer.iceGatheringState != "complete":
            await asyncio.sleep(0.05)
    await asyncio.wait_for(wait(), timeout)


def encode_body(body: bytes) -> str:
    return base64.b64encode(body).decode("ascii")


def decode_body(body: str) -> bytes:
    return decode_strict(body)


async def answer_offer(
    offer: dict[str, str],
    on_message: Callable[[Any, dict[str, Any]], Awaitable[None]],
    on_close: Callable[[], Awaitable[None]],
    stun_servers: list[str] | None = None,
    loopback_candidate: bool = True,
) -> tuple[Any, dict[str, str]]:
    """Receive a browser offer on the Agent side and return an answer with ICE candidates."""
    if loopback_candidate:
        enable_loopback_candidate()
    RTCPeerConnection, RTCSessionDescription = load_aiortc()
    configuration = None
    if stun_servers:
        from aiortc import RTCConfiguration, RTCIceServer
        configuration = RTCConfiguration([RTCIceServer(urls=stun_servers)])
    peer = RTCPeerConnection(configuration=configuration)
    closed = False
    dispatches: set[asyncio.Task] = set()

    async def close_once() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        for task in list(dispatches):
            task.cancel()
        dispatches.clear()
        await on_close()
        with contextlib.suppress(Exception):
            await peer.close()

    @peer.on("datachannel")
    def on_datachannel(channel):
        print(f"p2p datachannel open label={channel.label}", flush=True)
        @channel.on("message")
        def on_message_event(raw):
            async def dispatch():
                if isinstance(raw, bytes):
                    raw_value = raw.decode("utf-8")
                else:
                    raw_value = raw
                await on_message(channel, json.loads(raw_value))
            task = asyncio.create_task(dispatch())
            dispatches.add(task)
            task.add_done_callback(dispatches.discard)

        @channel.on("close")
        def on_channel_close():
            print("p2p datachannel closed", flush=True)
            asyncio.create_task(close_once())

    @peer.on("connectionstatechange")
    async def on_state_change():
        print(f"p2p connection state={peer.connectionState}", flush=True)
        if peer.connectionState in {"failed", "closed", "disconnected"}:
            await close_once()

    try:
        await peer.setRemoteDescription(RTCSessionDescription(sdp=offer["sdp"], type=offer["type"]))
        answer = await peer.createAnswer()
        await peer.setLocalDescription(answer)
        await wait_ice_complete(peer)
    except Exception:
        await close_once()
        raise
    local = peer.localDescription
    return peer, {"type": local.type, "sdp": local.sdp}
