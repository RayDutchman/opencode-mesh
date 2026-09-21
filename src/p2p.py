from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from typing import Any, Awaitable, Callable


class P2PUnavailable(RuntimeError):
    """当前环境没有安装 WebRTC 实现。"""


def load_aiortc():
    try:
        from aiortc import RTCPeerConnection, RTCSessionDescription
    except ImportError as exc:
        raise P2PUnavailable("aiortc 未安装，暂时使用 Relay") from exc
    return RTCPeerConnection, RTCSessionDescription


async def wait_ice_complete(peer: Any, timeout: float = 15) -> None:
    async def wait():
        while peer.iceGatheringState != "complete":
            await asyncio.sleep(0.05)
    await asyncio.wait_for(wait(), timeout)


def encode_body(body: bytes) -> str:
    return base64.b64encode(body).decode("ascii")


def decode_body(body: str) -> bytes:
    return base64.b64decode(body or "")


async def answer_offer(
    offer: dict[str, str],
    on_message: Callable[[Any, dict[str, Any]], Awaitable[None]],
    on_close: Callable[[], Awaitable[None]],
    stun_servers: list[str] | None = None,
) -> tuple[Any, dict[str, str]]:
    """在 Agent 侧接收浏览器 offer，并返回包含 ICE candidate 的 answer。"""
    RTCPeerConnection, RTCSessionDescription = load_aiortc()
    configuration = None
    if stun_servers:
        from aiortc import RTCConfiguration, RTCIceServer
        configuration = RTCConfiguration([RTCIceServer(urls=stun_servers)])
    peer = RTCPeerConnection(configuration=configuration)
    closed = False

    async def close_once() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
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
            asyncio.create_task(dispatch())

        @channel.on("close")
        def on_channel_close():
            print("p2p datachannel closed", flush=True)
            asyncio.create_task(close_once())

    @peer.on("connectionstatechange")
    async def on_state_change():
        print(f"p2p connection state={peer.connectionState}", flush=True)
        if peer.connectionState in {"failed", "closed", "disconnected"}:
            await close_once()

    await peer.setRemoteDescription(RTCSessionDescription(sdp=offer["sdp"], type=offer["type"]))
    answer = await peer.createAnswer()
    await peer.setLocalDescription(answer)
    await wait_ice_complete(peer)
    local = peer.localDescription
    return peer, {"type": local.type, "sdp": local.sdp}
