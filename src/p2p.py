from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import string
import time
from typing import Any, AsyncIterator, Awaitable, Callable


# 有界协议常量：控制帧超时、请求/响应上限、分片大小。
CONTROL_SEND_TIMEOUT = 5.0
MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
# 兼容旧配置名：仅当显式设置 max_p2p_message_bytes 时覆盖，否则继承请求上限。
MAX_P2P_MESSAGE_BYTES = 1024 * 1024
CHUNK_SIZE = 32768
# 稳定错误原因：Relay 与 P2P 两端共用，前端按 reason 判断是否可重试。
REQUEST_TOO_LARGE_REASON = "request exceeds max_request_bytes limit"
RESPONSE_TOO_LARGE_REASON = "response exceeds max_response_bytes limit"
PAYLOAD_TOO_LARGE_REASON = "reassembled payload exceeds max_p2p_message_bytes limit"
INVALID_ENCODING_REASON = "invalid base64 encoding"
STREAM_OVERFLOW_REASON = "stream buffer overflow"
# 分片路径的协议错误（乱序、重放）与缓冲区资源不足，分别使用稳定常量归类。
INVALID_SEQUENCE_REASON = "invalid frame sequence"
ASSEMBLY_BUDGET_REASON = "reassembly byte budget exceeded"
# 上游失败：连接级失败与其它传输层错误分别归类，避免误导。
CONNECTION_FAILED_REASON = "connection failed"
UPSTREAM_ERROR_REASON = "upstream request failed"
WS_FRAME_FLOOR_BYTES = 16 * 1024 * 1024

_B64_ALPHABET = frozenset(string.ascii_letters + string.digits + "+/")


class P2PUnavailable(RuntimeError):
    """WebRTC implementation is not installed in this environment."""


class FrameError(RuntimeError):
    """分片信封违反有界重组契约（超限、乱序、重复或编码非法）。"""


def request_limit(cfg: dict[str, Any]) -> int:
    """请求体有效上限的唯一读取入口（默认 64MiB）。"""
    return int(cfg.get("max_request_bytes", MAX_REQUEST_BYTES))


def response_limit(cfg: dict[str, Any]) -> int:
    """响应体有效上限的唯一读取入口（默认 64MiB）。"""
    return int(cfg.get("max_response_bytes", MAX_RESPONSE_BYTES))


def p2p_message_limit(cfg: dict[str, Any]) -> int:
    """P2P 单条消息上限；未显式配置时与请求上限一致，避免 Relay/P2P 分叉。"""
    if cfg.get("max_p2p_message_bytes") is not None:
        return int(cfg["max_p2p_message_bytes"])
    return request_limit(cfg)


def p2p_total_budget(cfg: dict[str, Any]) -> int:
    """所有并行分片装配累计缓冲字节的全局上限。

    默认与单条消息上限一致（避免 64MiB × max_assemblies × peer 的放大）；
    可用 max_p2p_total_bytes 显式收紧或放宽。
    """
    if cfg.get("max_p2p_total_bytes") is not None:
        return int(cfg["max_p2p_total_bytes"])
    return p2p_message_limit(cfg)


def ws_frame_limit(cfg: dict[str, Any]) -> int:
    """uvicorn ws_max_size：覆盖应用层上限（base64 展开 + JSON 开销），不低于 16MiB。"""
    return max(WS_FRAME_FLOOR_BYTES, request_limit(cfg) * 2)


def is_valid_base64(encoded: str) -> bool:
    """不分配解码结果，严格校验标准 base64 字符集与填充。"""
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
    """严格解码标准 base64；非法字符、错误填充或长度都抛 ValueError。"""
    return base64.b64decode(encoded or "", validate=True)


class ResponseSizeGuard:
    """累计解码后字节数并在超过上限时抛出稳定 FrameError。

    在 base64 展开前先用 decoded_size 判断，避免为超限响应构造完整副本。
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
    """不分配解码结果，精确计算标准 base64 字符串的解码字节数。

    用于在 base64 展开前判断大小上限——仅凭编码长度无法区分 3 字节边界
    （如 8192 与 8193 字节会编码成同样长度）。
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
    """构造统一分片信封 {message_id, sequence, data, final}。"""
    return {"message_id": str(message_id), "sequence": int(sequence),
            "data": base64.b64encode(data or b"").decode("ascii"), "final": bool(final)}


def iter_frames(message_id: str, data: bytes, chunk_size: int = CHUNK_SIZE):
    """把一段字节拆分为分片信封，最后一个信封 final=True（空数据也有一帧）。"""
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
    """按 message_id 隔离的有界分片重组器。

    契约：同一 message_id 的 sequence 必须从 0 严格递增；重复、跳号、超限、
    重复 final 以及非法编码都产生有界错误；不同 message_id 互不影响。

    资源边界：残缺装配受 `max_assemblies`（数量，超出淘汰最旧）与 `ttl`
    （时间，过期清理）约束；所有并行装配的累计缓冲字节受全局 `budget`
    （默认与单条消息上限一致）约束，超出时返回 ASSEMBLY_BUDGET_REASON 稳定错误；
    `trace` 仅用于测试/调试，默认关闭以避免无界增长。
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
        # 全局字节预算：所有并行装配 buffered 字节之和的上限（真正低且全局有界）。
        self.budget = int(budget) if budget is not None else int(limit)
        self._used_bytes = 0
        self._assemblies: dict[str, dict[str, Any]] = {}
        # 到达帧轨迹：(message_id, sequence)，仅 trace_enabled 时记录。
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
        """释放该装配已缓冲的字节，把它从全局预算中剔除。"""
        self._used_bytes -= asm["total"]
        asm["total"] = 0
        asm["parts"] = []

    def feed(self, chunk: dict[str, Any]) -> str | None:
        """送入一帧，返回 "accepted"/"error" 或 None（继续等待）。"""
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
        # 错误/已完成墓碑保留到 TTL：同一 message_id 的重放一律拒绝，不重新装配。
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
        # 先按解码后长度判断上限，再解码，避免为超限分片构造完整副本。
        size = decoded_size(encoded)
        if asm["total"] + size > self.limit:
            asm["error"] = PAYLOAD_TOO_LARGE_REASON
            asm["reason"] = PAYLOAD_TOO_LARGE_REASON
            self._free(asm)
            return "error"
        # 全局字节预算：本次分片会导致累计缓冲超过总预算时，拒绝并稳定报错。
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
        """标记装配完成并释放分片内存，但保留墓碑直到 TTL，阻止同 id 重放。

        调用方须先读取 result()；之后该 message_id 的帧一律视为完成后的重放。
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
        """当前未完成/未释放的装配数量，用于资源边界断言。"""
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
        """返回该 message_id 的稳定错误种类（供上层区分 400/413）。"""
        return self._assemblies.get(message_id, {}).get("reason")


async def read_bounded(chunks: AsyncIterator[bytes], limit: int) -> bytes:
    """流式读取响应体并在超过上限时立即报错，避免构造超限副本。"""
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
    """让 aiortc/aioice 把 127.0.0.1 作为 host candidate 发布。

    aioice 的 get_host_addresses 默认排除 loopback（ip.ip != "127.0.0.1"）。
    但在 WSL mirrored 等场景下，宿主机浏览器只能通过共享 loopback 到达本机
    Agent，其余 host candidate（LAN/Docker/链路本地）对宿主机都不可达，
    导致 ICE 永远失败、只能走 Relay。这里在进程内幂等地补充 127.0.0.1。

    对远程浏览器无害：远端浏览器的 127.0.0.1 指向它自己，该候选只是
    多一个必然失败的 candidate pair，其余候选不受影响。
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
