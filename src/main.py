from __future__ import annotations
import argparse, asyncio, base64, contextlib, hmac, json, os, platform, random, re, secrets, socket, time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import uvicorn
import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse, RedirectResponse
from . import __version__
from .frontend import ASSET_ROOT, adapt_entry, asset_prefix, parse_asset_route, legacy_server_redirect
from .p2p import (CHUNK_SIZE, CONNECTION_FAILED_REASON, CONTROL_SEND_TIMEOUT,
                   INVALID_ENCODING_REASON, INVALID_SEQUENCE_REASON,
                   PAYLOAD_TOO_LARGE_REASON, REQUEST_TOO_LARGE_REASON,
                   RESPONSE_TOO_LARGE_REASON, STREAM_OVERFLOW_REASON,
                   ChunkAssembler, FrameError, P2PUnavailable, ResponseSizeGuard,
                   answer_offer, decode_strict, decoded_size, frame, is_valid_base64,
                   iter_frames, p2p_message_limit, p2p_total_budget, read_bounded, request_limit,
                   response_limit, UPSTREAM_ERROR_REASON, ws_frame_limit)
from .static_adapter import TRANSPORT_ADAPTER


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (seconds only); return None when there is no valid value."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    return None


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 60.0,
                  jitter: float = 0.5, retry_after: float | None = None,
                  rng: random.Random | None = None) -> float:
    """Bounded exponential backoff: attempt starts at 1, jitter is added, and Retry-After is respected."""
    generator = rng or random.Random()
    exponent = max(0, int(attempt) - 1)
    delay = base * (2 ** exponent)
    delay += generator.uniform(0, delay * jitter)
    # Clamp after adding jitter so the final delay never exceeds cap.
    delay = min(cap, delay)
    if retry_after is not None:
        delay = max(delay, min(cap, float(retry_after)))
    return delay


def load_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def make_id() -> str:
    return secrets.token_hex(8)


def private_json(path: Path, value: Any) -> None:
    """Write credentials atomically with owner-only permissions from creation."""
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        os.fchmod(output.fileno(), 0o600)
        json.dump(value, output, ensure_ascii=False, indent=2)
    temporary.replace(path)


def harden_permissions(path: Path) -> None:
    """Tighten an existing credential file left behind with default umask."""
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def parse_server_route(path: str) -> tuple[str | None, str]:
    """Parse an OpenCode ``/server/<key>`` route and recover the device upstream path."""
    match = re.match(r"^/server/([^/]+)(/.*)?$", path)
    if not match:
        return None, path
    encoded = match.group(1)
    try:
        padded = encoded.replace("-", "+").replace("_", "/")
        padded += "=" * ((4 - len(padded) % 4) % 4)
        server = urlparse(base64.b64decode(padded, validate=True).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Invalid server route") from None
    if server.scheme not in {"http", "https"} or not server.netloc:
        raise ValueError("Invalid server URL")

    device_prefix = "/_mesh/device/"
    if not server.path.startswith(device_prefix):
        raise ValueError("Server route is not a Mesh device")
    device_id = server.path[len(device_prefix):].split("/", 1)[0]
    if not device_id or any(char in device_id for char in "/\\."):
        raise ValueError("Invalid server route device")
    return device_id, match.group(2) or "/"


def rewrite_device_html(body: bytes, device_id: str, bootstrap: bool = False) -> bytes:
    """Rewrite root-relative static assets in device HTML to device-scoped paths."""
    prefix = "/_mesh/device/" + quote(device_id, safe="")
    pattern = re.compile(rb"((?:src|href)\s*=\s*[\"'])/(?!/|_mesh/)", re.IGNORECASE)
    result = pattern.sub(lambda match: match.group(1) + prefix.encode("ascii") + b"/", body)
    if bootstrap:
        result = result.replace((prefix + '/_assets/').encode(), (asset_prefix(device_id) + '/_assets/').encode())
    return result


def forwarding_headers(headers) -> dict[str, str]:
    """Never send gateway credentials or browser CSRF markers across the Agent trust boundary."""
    blocked = {'authorization', 'cookie', 'proxy-authorization', 'host', 'content-length',
               'forwarded', 'x-forwarded-for', 'x-forwarded-proto', 'x-forwarded-host',
               'x-forwarded-port', 'x-real-ip', 'origin', 'referer', 'accept-encoding',
               'connection', 'keep-alive', 'proxy-connection', 'transfer-encoding',
               'te', 'trailer', 'upgrade'}
    # The request body is already decoded; hop-by-hop frame headers and headers named by Connection must not cross to the next hop.
    for key, value in headers.items():
        if key.lower() == 'connection':
            blocked.update(token.strip().lower() for token in value.split(','))
    return {k: v for k, v in headers.items() if k.lower() not in blocked}


def filter_response_headers(headers) -> dict[str, str]:
    """Strip hop-by-hop headers and headers forbidden from being written back.

    ``set-cookie`` is forbidden for the browser Response constructor.
    The frontend rebuilds responses with ``new Response(body, {headers})``
    and would throw a TypeError otherwise. This helper is used by both P2P
    and Relay paths so upstream cookies never cross the trust boundary.
    """
    blocked = {'content-encoding', 'content-length', 'transfer-encoding', 'connection',
               'set-cookie', 'set-cookie2'}
    return {k: v for k, v in headers.items() if k.lower() not in blocked}


DEFAULT_STUN_SERVERS = ["stun:stun.l.google.com:19302"]

OFFLINE_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenCode Mesh</title>
<style>
:root{color-scheme:light dark}
body{margin:0;font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:#fafafa;color:#111;display:flex;min-height:100vh;align-items:center;justify-content:center}
@media (prefers-color-scheme:dark){body{background:#080808;color:#fafafa}}
.card{width:min(520px,calc(100vw - 48px));padding:24px 28px;border:1px solid rgba(127,127,127,.3);border-radius:12px}
h1{font-size:15px;margin:0 0 6px}
p{margin:0 0 14px;opacity:.7}
ul{list-style:none;margin:0;padding:0}
li{display:flex;align-items:center;gap:8px;padding:6px 0;border-top:1px solid rgba(127,127,127,.2)}
.dot{width:8px;height:8px;border-radius:50%;background:#9ca3af}
.dot.on{background:#22c55e}
.name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.state{opacity:.6;font-size:12px}
</style>
</head>
<body>
<div class="card">
<h1>OpenCode Mesh</h1>
<p id="msg">No device is online. This page refreshes automatically.</p>
<ul id="list"></ul>
</div>
<script>
var target = __TARGET__;
var msg = document.getElementById('msg');
var list = document.getElementById('list');
function render(devices){
  list.textContent = '';
  devices.forEach(function(d){
    var li = document.createElement('li');
    var dot = document.createElement('span'); dot.className = 'dot' + (d.online ? ' on' : '');
    var name = document.createElement('span'); name.className = 'name'; name.textContent = d.name || d.device_id;
    var state = document.createElement('span'); state.className = 'state'; state.textContent = d.online ? 'online' : 'offline';
    li.append(dot, name, state); list.append(li);
  });
  var ready = target ? devices.some(function(d){ return d.device_id === target && d.online; }) : devices.some(function(d){ return d.online; });
  if (ready) location.reload();
}
function tick(){
  fetch('/_mesh/devices', {credentials:'same-origin', cache:'no-store'}).then(function(r){ return r.ok ? r.json() : null; }).then(function(data){
    if (!data) return;
    var devices = Array.isArray(data.devices) ? data.devices : [];
    msg.textContent = target ? 'Device is offline. This page refreshes automatically.' : 'No device is online. This page refreshes automatically.';
    render(devices);
  }).catch(function(){});
}
tick();
setInterval(tick, 3000);
</script>
</body>
</html>
"""


def hostname() -> str:
    return socket.gethostname() or platform.node() or "Unnamed device"


class Registry:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.path = Path(cfg.get("state_file", "./data/state.json"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.devices: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self):
        if self.path.exists():
            harden_permissions(self.path)
            try:
                self.devices = json.loads(self.path.read_text()).get("devices", {})
            except Exception:
                self.devices = {}

    def save(self):
        devices = {key: {k: v for k, v in value.items() if k != "ws"}
                   for key, value in self.devices.items()}
        private_json(self.path, {"devices": devices})

    def public(self):
        result = []
        for d in self.devices.values():
            result.append({k: v for k, v in d.items() if k not in {"auth_token", "ws"}} | {"online": bool(d.get("ws"))})
        return result


class StreamState:
    """Bounded stream buffer: overflow produces an explicit stream_error and closes, without evicting buffered fragments."""

    OVERFLOW_REASON = STREAM_OVERFLOW_REASON

    def __init__(self, maxsize: int):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.signal = asyncio.Event()
        self.pending_end: dict[str, Any] | None = None
        self.overflow_reason: str | None = None
        self.closed = False

    def push(self, item: dict[str, Any]) -> bool:
        if self.closed:
            return False
        try:
            self.queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            if item.get("type") in {"stream_end", "stream_error"}:
                self.pending_end = item
                self.closed = True
                self.signal.set()
                return True
            self.overflow_reason = self.OVERFLOW_REASON
            self.closed = True
            self.signal.set()
            return False

    def fail(self, reason: str) -> None:
        """Inject one terminal stream_error idempotently."""
        if self.closed:
            return
        self.closed = True
        self.pending_end = {"type": "stream_error", "error": reason}
        self.signal.set()


class Gateway:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.registry = Registry(cfg)
        self.pending: dict[str, asyncio.Future] = {}
        self.streams: dict[str, StreamState] = {}
        self.browser_ws: dict[str, WebSocket] = {}
        self.owners: dict[str, WebSocket] = {}
        self.p2p_answers: dict[str, asyncio.Future] = {}
        self.p2p_owners: dict[str, WebSocket] = {}
        self.device_send_locks: dict[int, asyncio.Lock] = {}
        self.browser_send_locks: dict[str, asyncio.Lock] = {}
        self.auth_failures: dict[str, list[float]] = {}
        self.register_attempts: dict[str, list[float]] = {}
        self.app = FastAPI(title="OpenCode Mesh Gateway", lifespan=self.lifespan)
        self.routes()

    @contextlib.asynccontextmanager
    async def lifespan(self, app: FastAPI):
        """Close active streams and browser bridges when the Gateway stops."""
        try:
            yield
        finally:
            for state in list(self.streams.values()):
                state.fail("Gateway is shutting down")
            for bridge in list(self.browser_ws.values()):
                with contextlib.suppress(Exception):
                    await bridge.close(code=1001)

    def check_auth(self, req: Request) -> bool:
        auth = self.cfg.get("auth", {})
        username = str(auth.get("username") or "")
        if not username or not auth.get("password"):
            return False
        password = str(auth.get("password") or "")
        expected = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        return hmac.compare_digest(req.headers.get("authorization", "").encode(), expected.encode())

    @staticmethod
    def is_online(device: dict[str, Any] | None, stale_after: float = 45) -> bool:
        """A device counts as online only while its control connection is fresh."""
        if not device or not device.get("ws"):
            return False
        last_seen = device.get("last_seen")
        return not last_seen or (time.time() - float(last_seen)) <= stale_after

    def choose_device(self) -> tuple[str, dict[str, Any]] | None:
        preferred = str(self.cfg.get("default_device") or "")
        candidates = []
        if preferred and preferred in self.registry.devices:
            candidates.append((preferred, self.registry.devices[preferred]))
        candidates.extend((k, v) for k, v in self.registry.devices.items() if k != preferred)
        for device_id, device in candidates:
            if self.is_online(device):
                return device_id, device
        return None

    async def send_to_device(self, ws: WebSocket, message: dict[str, Any]) -> None:
        """Serialize all Gateway writes to one Agent control connection."""
        key = id(ws)
        lock = self.device_send_locks.setdefault(key, asyncio.Lock())
        async with lock:
            await ws.send_text(json.dumps(message))

    async def send_control(self, ws: WebSocket, message: dict[str, Any],
                           timeout: float = CONTROL_SEND_TIMEOUT) -> None:
        """Bounded control frame send: timeout or a closed peer both raise a connection-level error."""
        try:
            await asyncio.wait_for(self.send_to_device(ws, message), timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                await ws.close(code=1011)
            raise ConnectionError("Agent control send timed out")
        except Exception as exc:
            # Writing to a closed socket raises RuntimeError/ConnectionError; normalize both to a connection-level error.
            with contextlib.suppress(Exception):
                await ws.close(code=1011)
            raise ConnectionError("Agent control connection closed") from exc

    async def attach_device(self, device: dict[str, Any], ws: WebSocket) -> None:
        """Bind a control connection; on a reconnect with the same device_id, close the old connection and clean up its pending state."""
        old = device.get("ws")
        if old is not None and old is not ws:
            # Fully close the old connection first to unblock its receive loop, then clean up the state it holds.
            with contextlib.suppress(Exception):
                await old.close(code=1011)
            await self.cleanup_device(old)
        device["ws"] = ws
        device["last_seen"] = int(time.time())

    async def cleanup_device(self, ws: WebSocket) -> None:
        """Idempotently clean up requests, streams, P2P answers, and browser bridges held by a control connection."""
        self.device_send_locks.pop(id(ws), None)
        for device in self.registry.devices.values():
            if device.get("ws") is ws:
                device.pop("ws", None)
        for request_id, owner in list(self.owners.items()):
            if owner is not ws:
                continue
            self.owners.pop(request_id, None)
            future = self.pending.pop(request_id, None)
            if future and not future.done():
                future.set_exception(ConnectionError("Device control connection lost"))
            state = self.streams.pop(request_id, None)
            if state is not None:
                state.fail("Device control connection lost")
            bridge = self.browser_ws.get(request_id)
            if bridge:
                with contextlib.suppress(Exception):
                    await bridge.close(code=1011)
        for session_id, owner in list(self.p2p_owners.items()):
            if owner is not ws:
                continue
            self.p2p_owners.pop(session_id, None)
            future = self.p2p_answers.pop(session_id, None)
            if future and not future.done():
                future.set_exception(ConnectionError("Device control connection lost"))

    @staticmethod
    def allow_rate(bucket: dict[str, list[float]], key: str, limit: int, window: float = 60) -> bool:
        now = time.monotonic()
        recent = [stamp for stamp in bucket.get(key, []) if now - stamp < window]
        if len(recent) >= limit:
            bucket[key] = recent
            return False
        recent.append(now)
        bucket[key] = recent
        return True

    async def send_browser(self, bridge_id: str, payload: Any, binary: bool) -> None:
        bridge = self.browser_ws.get(bridge_id)
        if not bridge:
            return
        lock = self.browser_send_locks.setdefault(bridge_id, asyncio.Lock())
        try:
            async with lock:
                if binary:
                    await bridge.send_bytes(payload)
                else:
                    await bridge.send_text(payload)
        except Exception:
            self.browser_ws.pop(bridge_id, None)

    async def close_browser(self, bridge_id: str, code: int) -> None:
        bridge = self.browser_ws.get(bridge_id)
        if not bridge:
            return
        lock = self.browser_send_locks.setdefault(bridge_id, asyncio.Lock())
        with contextlib.suppress(Exception):
            async with lock:
                await bridge.close(code=code)

    @staticmethod
    def wants_html(req: Request) -> bool:
        return "text/html" in req.headers.get("accept", "")

    def offline_response(self, req: Request, device_id: str | None, known: bool = False):
        """Browsers get a friendly page; API clients keep getting JSON."""
        if self.wants_html(req):
            return HTMLResponse(self.offline_page(device_id), headers={"Cache-Control": "no-store"})
        if device_id:
            return JSONResponse({"error": "Specified device offline or not found", "device_id": device_id},
                                status_code=503 if known else 404)
        return JSONResponse({"error": "Device offline or no device selected"}, status_code=503)

    @staticmethod
    def offline_page(device_id: str | None) -> str:
        return OFFLINE_PAGE.replace("__TARGET__", json.dumps(device_id))

    def resolve_default_device(self) -> str | None:
        """Resolve the default device: the configured default_device first, otherwise the first online device."""
        preferred = str(self.cfg.get("default_device") or "")
        if preferred and self.is_online(self.registry.devices.get(preferred)):
            return preferred
        selected = self.choose_device()
        return selected[0] if selected else None

    @staticmethod
    def parse_device_route(path: str) -> tuple[str | None, str]:
        """Parse a device virtual Server URL and preserve the original OpenCode path."""
        prefix = "/_mesh/device/"
        if not path.startswith(prefix):
            return None, path
        remainder = path[len(prefix):]
        device_id, separator, upstream = remainder.partition("/")
        if not device_id or any(char in device_id for char in "/\\."):
            raise ValueError("Invalid device route")
        return device_id, "/" + upstream

    def routes(self):
        app = self.app

        @app.middleware("http")
        async def basic_auth_middleware(req: Request, call_next):
            path = req.url.path
            if path == "/_mesh/register" or path.startswith("/_mesh/agent/") or path.startswith("/_mesh/deregister/"):
                return await call_next(req)
            if not self.check_auth(req):
                client_ip = req.client.host if req.client else "unknown"
                if not self.allow_rate(self.auth_failures, client_ip, 10):
                    return JSONResponse({"error": "too many authentication failures"}, status_code=429)
                return JSONResponse({"error": "unauthorized"}, status_code=401,
                                    headers={"WWW-Authenticate": 'Basic realm="OpenCode Mesh"'})
            return await call_next(req)

        @app.get("/_mesh/devices")
        async def devices(req: Request):
            return {"devices": self.registry.public(), "default_device": self.resolve_default_device(),
                    "configured_default_device": self.cfg.get("default_device")}

        @app.get("/_mesh/transport-manifest")
        async def transport_manifest(req: Request):
            device_id = req.query_params.get("device") or self.cfg.get("default_device") or ""
            device = self.registry.devices.get(str(device_id)) if device_id else None
            if not device:
                selected = self.choose_device()
                if selected:
                    device_id, device = selected
            return {
                "version": 1,
                "device_id": device_id or None,
                "server_url": f"{str(req.base_url).rstrip('/')}/_mesh/device/{quote(str(device_id), safe='')}" if device_id else None,
                "relay": str(req.base_url).rstrip("/"),
                "lan": self.cfg.get("lan_base_url"),
                "p2p": {"enabled": bool(device and device.get("ws")), "offer": "/_mesh/p2p/offer"},
                "stun_servers": self.cfg.get("stun_servers") or DEFAULT_STUN_SERVERS,
                "capabilities": {"http": True, "sse": True, "websocket": True, "pty": True},
            }

        @app.post("/_mesh/p2p/offer")
        async def p2p_offer(req: Request):
            client_ip = req.client.host if req.client else "unknown"
            if not self.allow_rate(self.register_attempts, f"p2p:{client_ip}", 20):
                return JSONResponse({"error": "too many P2P offers"}, status_code=429)
            data = await req.json()
            device_id = str(data.get("device_id") or self.cfg.get("default_device") or "")
            device = self.registry.devices.get(device_id) if device_id else None
            agent_ws = device.get("ws") if device else None
            if not agent_ws:
                return JSONResponse({"error": "Device offline"}, status_code=503)
            if len(json.dumps(data)) > int(self.cfg.get("max_p2p_offer_bytes", 1024 * 1024)):
                return JSONResponse({"error": "P2P signaling too large"}, status_code=413)
            session_id = secrets.token_urlsafe(18)
            future = asyncio.get_running_loop().create_future()
            self.p2p_answers[session_id] = future
            self.p2p_owners[session_id] = agent_ws
            try:
                await self.send_control(agent_ws, {"type": "p2p_offer", "id": session_id,
                                                   "offer": data, "stun_servers": self.cfg.get("stun_servers") or DEFAULT_STUN_SERVERS})
                return JSONResponse(await asyncio.wait_for(future, 20))
            except Exception:
                return JSONResponse({"error": "P2P connection failed"}, status_code=502)
            finally:
                self.p2p_answers.pop(session_id, None)
                self.p2p_owners.pop(session_id, None)

        @app.websocket("/_mesh/ws/{path:path}")
        @app.websocket("/_mesh/device/{path:path}")
        @app.websocket("/server/{path:path}")
        @app.websocket("/api/pty/{path:path}")
        @app.websocket("/pty/{path:path}")
        async def browser_ws(client: WebSocket, path: str):
            if not self.check_auth(client):
                await client.close(code=4401)
                return
            explicit_device = None
            if client.url.path.startswith("/server/"):
                try:
                    explicit_device, path = parse_server_route(client.url.path)
                    path = path.lstrip("/")
                except ValueError:
                    await client.close(code=4404)
                    return
            elif client.url.path.startswith("/_mesh/device/"):
                try:
                    explicit_device, path = self.parse_device_route(client.url.path)
                    path = path.lstrip("/")
                except ValueError:
                    await client.close(code=4404)
                    return
            if explicit_device:
                device_id = explicit_device
                d = self.registry.devices.get(device_id)
            else:
                selected = self.choose_device()
                device_id, d = selected if selected else (None, None)
            agent_ws = d.get("ws") if d else None
            if not agent_ws:
                await client.close(code=4403)
                return
            await client.accept()
            bridge_id = secrets.token_urlsafe(12)
            self.browser_ws[bridge_id] = client
            self.owners[bridge_id] = agent_ws
            query = client.query_params
            upstream_path = client.url.path
            if upstream_path.startswith("/_mesh/ws/"):
                upstream_path = "/" + path
            elif explicit_device:
                upstream_path = "/" + path
            open_item = {"type": "ws_open", "id": bridge_id, "path": upstream_path,
                         "query": str(query), "headers": forwarding_headers(client.headers)}
            try:
                await self.send_control(agent_ws, open_item)
                while True:
                    msg = await client.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
                    if msg.get("text") is not None:
                        await self.send_control(agent_ws, {"type": "ws_data", "id": bridge_id,
                            "kind": "text", "data": msg["text"]})
                    elif msg.get("bytes") is not None:
                        await self.send_control(agent_ws, {"type": "ws_data", "id": bridge_id,
                            "kind": "bytes", "data": base64.b64encode(msg["bytes"]).decode()})
            except Exception:
                pass
            finally:
                with contextlib.suppress(Exception):
                    await self.send_control(agent_ws, {"type": "ws_close", "id": bridge_id})
                self.browser_ws.pop(bridge_id, None)
                self.owners.pop(bridge_id, None)
                self.browser_send_locks.pop(bridge_id, None)
                try:
                    await client.close()
                except Exception:
                    pass

        @app.post("/_mesh/register")
        async def register(req: Request):
            client_ip = req.client.host if req.client else "unknown"
            if not self.allow_rate(self.register_attempts, f"register:{client_ip}", 20):
                return JSONResponse({"error": "too many registration attempts"}, status_code=429)
            data = await req.json()
            if not self.cfg.get("enroll_token") or not hmac.compare_digest(str(data.get("enroll_token", "")).encode(), str(self.cfg.get("enroll_token", "")).encode()):
                return JSONResponse({"error": "invalid enrollment token"}, status_code=403)
            device_id = str(data.get("device_id") or make_id())
            if not re.fullmatch(r'[A-Za-z0-9_-]{4,64}', device_id):
                return JSONResponse({"error": "Invalid device ID"}, status_code=400)
            existing = self.registry.devices.get(device_id)
            if existing and not hmac.compare_digest(str(data.get('agent_token', '')).encode(), str(existing.get('auth_token', '')).encode()):
                if not data.get("rotate_token"):
                    return JSONResponse({"error": "Device ownership proof required"}, status_code=403)
                # enroll_token was verified: allow rotating the persisted device token that is out of sync with Gateway state.
                existing["auth_token"] = secrets.token_urlsafe(32)
            d = self.registry.devices.setdefault(device_id, {"device_id": device_id})
            d.update({"name": data.get("name") or "Unnamed device", "platform": data.get("platform", "unknown"),
                      "service": "opencode", "updated_at": int(time.time())})
            d.setdefault("auth_token", secrets.token_urlsafe(32))
            self.registry.save()
            return {"device_id": device_id, "agent_token": d["auth_token"], "name": d["name"]}

        @app.delete("/_mesh/deregister/{device_id}")
        async def deregister(req: Request, device_id: str):
            token = req.headers.get("x-mesh-agent-token", "")
            d = self.registry.devices.get(device_id)
            if not d or not token or not hmac.compare_digest(token.encode(), str(d.get("auth_token", "")).encode()):
                return JSONResponse({"error": "unauthorized"}, status_code=403)
            if d.get("ws"):
                return JSONResponse({"error": "Stop the Agent before deregistration"}, status_code=409)
            self.registry.devices.pop(device_id, None)
            self.registry.save()
            return {"ok": True}

        @app.websocket("/_mesh/agent/{device_id}")
        async def agent(ws: WebSocket, device_id: str):
            d = self.registry.devices.get(device_id)
            if not d or not hmac.compare_digest(ws.headers.get("x-mesh-agent-token", "").encode(), d.get("auth_token", "").encode()):
                await ws.close(code=4403)
                return
            await ws.accept()
            await self.attach_device(d, ws)
            try:
                while True:
                    msg = await ws.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
                    if d.get("ws") is not ws:
                        # The old connection was replaced by a new one with the same device_id; never update last_seen or resolve requests on it.
                        break
                    if "text" not in msg:
                        continue
                    item = json.loads(msg["text"])
                    if item.get("type") == "pong":
                        d["last_seen"] = int(time.time())
                    elif item.get("type") == "agent_hello":
                        d["last_seen"] = int(time.time())
                    elif item.get("type") in {"ws_data", "ws_opened", "ws_closed", "ws_error"}:
                        bridge_id = item.get("id")
                        bridge = self.browser_ws.get(bridge_id)
                        if bridge:
                            # Never await a slow browser from the shared device receive loop.
                            if item.get("type") == "ws_data":
                                binary = item.get("kind") == "bytes"
                                try:
                                    payload = decode_strict(item.get("data", "")) if binary else item.get("data", "")
                                except Exception:
                                    asyncio.create_task(self.close_browser(bridge_id, 1011))
                                    continue
                                asyncio.create_task(self.send_browser(bridge_id, payload, binary))
                            elif item.get("type") == "ws_closed":
                                asyncio.create_task(self.close_browser(bridge_id, item.get("code", 1000)))
                            elif item.get("type") == "ws_error":
                                asyncio.create_task(self.close_browser(bridge_id, 1011))
                    elif item.get("type") == "response":
                        future = self.pending.pop(item.get("id", ""), None)
                        if future and not future.done():
                            future.set_result(item)
                    elif item.get("type") == "stream_chunk":
                        state = self.streams.get(item.get("id", ""))
                        if state is not None:
                            self.enqueue_stream(state, item)
                    elif item.get("type") in {"stream_end", "stream_error"}:
                        state = self.streams.get(item.get("id", ""))
                        if state is not None:
                            self.enqueue_stream(state, item)
                    elif item.get("type") == "p2p_answer":
                        future = self.p2p_answers.get(item.get("id", ""))
                        if future and not future.done():
                            future.set_result(item.get("answer", {}))
            except (WebSocketDisconnect, Exception):
                pass
            finally:
                await self.cleanup_device(ws)

        @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
        async def proxy(req: Request, path: str):
            explicit_device = None
            frontend_asset = ("/" + path).startswith(ASSET_ROOT)
            try:
                if frontend_asset:
                    explicit_device, routed_path = parse_asset_route("/" + path)
                elif path.startswith("server/"):
                    explicit_device, routed_path = parse_server_route("/" + path)
                else:
                    explicit_device, routed_path = self.parse_device_route("/" + path)
            except ValueError:
                if req.method in {'GET', 'HEAD'} and self.wants_html(req):
                    target = legacy_server_redirect('/' + path, str(req.base_url).rstrip('/'),
                                                    self.cfg.get('default_device') or self.resolve_default_device())
                    if target:
                        return RedirectResponse(target + ('?' + req.url.query if req.url.query else ''))
                return JSONResponse({"error": "Invalid device route"}, status_code=404)
            if explicit_device:
                path = routed_path.lstrip("/")
            elif path.startswith("_mesh/"):
                return JSONResponse({"error": "not found"}, status_code=404)

            if not explicit_device and (path == 'api' or path.startswith('api/')):
                return JSONResponse({"error": "Explicit device required", "reason": "device_required"}, status_code=400)

            if explicit_device:
                # An explicitly routed device must never be substituted by another one.
                d = self.registry.devices.get(explicit_device)
                if d and d.get("ws") and not self.is_online(d):
                    with contextlib.suppress(Exception):
                        await d["ws"].close(code=1011)
                if not self.is_online(d):
                    return self.offline_response(req, explicit_device, known=bool(d))
                device_id, ws = explicit_device, d["ws"]
            else:
                # The configured default is a preference, not a hard requirement:
                # fall back to any other online device, and show a friendly page when none is online.
                selected = self.choose_device()
                if not selected:
                    return self.offline_response(req, None)
                device_id, d = selected
                ws = d["ws"]
            request_id = secrets.token_urlsafe(12)
            body = await req.body()
            if len(body) > request_limit(self.cfg):
                return JSONResponse({"error": REQUEST_TOO_LARGE_REASON,
                                     "reason": REQUEST_TOO_LARGE_REASON}, status_code=413)
            if "text/event-stream" in req.headers.get("accept", "") or path in {"event", "global/event", "api/event"} or (path.startswith("api/session/") and path.endswith("/event")):
                return await self.stream_proxy(req, d, ws, path, request_id, body)

            item = {"type": "request", "id": request_id, "method": req.method, "path": "/" + path,
                    "query": req.url.query, "headers": forwarding_headers(req.headers),
                    "body": base64.b64encode(body).decode()}
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self.pending[request_id] = future
            self.owners[request_id] = ws
            completed = False
            try:
                print(f"proxy request id={request_id} path=/{path}", flush=True)
                await self.send_control(ws, item, timeout=10)
                result = await asyncio.wait_for(future, timeout=float(self.cfg.get("request_timeout", 120)))
                completed = True
            except Exception:
                self.pending.pop(request_id, None)
                return JSONResponse({"error": CONNECTION_FAILED_REASON,
                                     "reason": CONNECTION_FAILED_REASON}, status_code=502)
            finally:
                self.pending.pop(request_id, None)
                self.owners.pop(request_id, None)
                if not completed:
                    with contextlib.suppress(Exception):
                        await self.send_control(ws, {"type": "cancel", "id": request_id}, timeout=2)
            headers = {k: v for k, v in result.get("headers", {}).items()
                       if k.lower() not in {"content-length", "transfer-encoding", "connection", "content-security-policy", "x-frame-options"}}
            encoded_body = result.get("body", "")
            guard = ResponseSizeGuard(response_limit(self.cfg))
            try:
                guard.add_encoded(encoded_body)
            except FrameError as exc:
                return JSONResponse({"error": str(exc), "reason": str(exc)}, status_code=502)
            try:
                body = decode_strict(encoded_body)
            except Exception:
                return JSONResponse({"error": INVALID_ENCODING_REASON,
                                     "reason": INVALID_ENCODING_REASON}, status_code=502)
            print(f"proxy response id={request_id} status={result.get('status')} encoded={len(result.get('body', ''))} decoded={len(body)}", flush=True)
            content_type = headers.get("content-type", headers.get("Content-Type", ""))
            if "text/html" in content_type.lower() and int(result.get("status", 502)) == 200:
                body = rewrite_device_html(body, device_id, bootstrap=True)
                body = inject_mesh_bar(body)
                headers['cache-control'] = 'no-store'
            if frontend_asset and int(result.get('status', 502)) == 200:
                try:
                    body = adapt_entry('/' + path, body, device_id)
                except ValueError as exc:
                    return JSONResponse({'error': str(exc)}, status_code=502)
                # The new namespace isolates the old versions' immutable cache; the entry adapter applies only within that namespace.
                headers.pop('etag', None)
            headers["content-length"] = str(len(body))
            return Response(body, status_code=int(result.get("status", 502)), headers=headers)

    async def stream_proxy(self, req: Request, d: dict[str, Any], ws: WebSocket,
                           path: str, request_id: str, body: bytes):
        if len(body) > request_limit(self.cfg):
            return JSONResponse({"error": REQUEST_TOO_LARGE_REASON,
                                 "reason": REQUEST_TOO_LARGE_REASON}, status_code=413)
        state = StreamState(int(self.cfg.get("stream_queue_size", 2048)))
        self.streams[request_id] = state
        self.owners[request_id] = ws
        messages = self.stream_messages(state)
        guard = ResponseSizeGuard(response_limit(self.cfg))
        item = {"type": "stream_request", "id": request_id, "method": req.method,
                "path": "/" + path, "query": req.url.query, "headers": forwarding_headers(req.headers),
                "body": base64.b64encode(body).decode()}
        try:
            await self.send_control(ws, item)
            first = await asyncio.wait_for(messages.__anext__(), timeout=30)
            if first.get("type") == "stream_error":
                await messages.aclose()
                self.streams.pop(request_id, None)
                self.owners.pop(request_id, None)
                reason = first.get("reason") or first.get("error", "stream failed")
                # Invalid request encoding is a protocol error: return 400 instead of 502.
                status_code = 400 if reason == INVALID_ENCODING_REASON else 502
                return JSONResponse({"error": reason, "reason": reason}, status_code=status_code)
            first_body = ""
            if first.get("type") == "stream_chunk":
                # The first frame's body (if any) also counts toward the limit so a single oversized
                # fragment cannot bypass it; it must be relayed verbatim.
                first_body = first.get("body") or ""
                guard.add_encoded(first_body)
            headers = filter_response_headers(first.get("headers", {}))
            status = int(first.get("status", 200))
            async def body_iter():
                ended = False
                try:
                    if first_body:
                        yield decode_strict(first_body)
                    async for msg in messages:
                        if msg.get("type") == "stream_end":
                            ended = True
                            break
                        if msg.get("type") == "stream_error":
                            raise ConnectionError(msg.get("error", "Event stream disconnected"))
                        if msg.get("type") == "stream_chunk":
                            encoded = msg.get("body") or ""
                            # The FrameError raised on overflow carries a stable reason and must propagate
                            # untouched, not be masked by the cancel in finally or a later disconnect error.
                            guard.add_encoded(encoded)
                            yield decode_strict(encoded)
                finally:
                    await messages.aclose()
                    self.streams.pop(request_id, None)
                    self.owners.pop(request_id, None)
                    if not ended:
                        with contextlib.suppress(Exception):
                            await self.send_control(ws, {"type": "cancel", "id": request_id}, timeout=2)
            return StreamingResponse(body_iter(), status_code=status, headers=headers)
        except FrameError as exc:
            await messages.aclose()
            self.streams.pop(request_id, None)
            self.owners.pop(request_id, None)
            with contextlib.suppress(Exception):
                await self.send_control(ws, {"type": "cancel", "id": request_id}, timeout=2)
            return JSONResponse({"error": str(exc), "reason": str(exc)}, status_code=502)
        except Exception:
            await messages.aclose()
            self.streams.pop(request_id, None)
            self.owners.pop(request_id, None)
            with contextlib.suppress(Exception):
                await self.send_control(ws, {"type": "cancel", "id": request_id}, timeout=2)
            return JSONResponse({"error": CONNECTION_FAILED_REASON,
                                 "reason": CONNECTION_FAILED_REASON}, status_code=502)

    @staticmethod
    def enqueue_stream(state: StreamState, item: dict[str, Any]) -> bool:
        """Push one frame into the bounded stream buffer; overflow yields an explicit stream_error instead of evicting old fragments."""
        return state.push(item)

    @staticmethod
    async def stream_messages(state: StreamState):
        """Consume the bounded stream buffer: normal drain, overflow, or a terminal frame each end exactly once."""
        while True:
            while True:
                try:
                    yield state.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            if state.pending_end is not None:
                terminal = state.pending_end
                state.pending_end = None
                yield terminal
                return
            if state.overflow_reason is not None:
                yield {"type": "stream_error", "error": state.overflow_reason,
                       "reason": state.overflow_reason}
                return
            state.signal.clear()
            getter = asyncio.ensure_future(state.queue.get())
            waiter = asyncio.ensure_future(state.signal.wait())
            try:
                done, pending = await asyncio.wait({getter, waiter},
                                                   return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (getter, waiter):
                    if not task.done():
                        task.cancel()
                for task in (getter, waiter):
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            if getter in done and not getter.cancelled():
                yield getter.result()


class Agent:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.app = FastAPI(title="OpenCode Mesh Agent")
        self.target = cfg["opencode_url"].rstrip("/")
        self.ws_queues: dict[str, asyncio.Queue] = {}
        self.p2p_peers: set[Any] = set()
        self.p2p_tasks: dict[tuple[int, str], asyncio.Task] = {}
        self.p2p_assemblers: dict[int, ChunkAssembler] = {}
        self.p2p_sequence: dict[tuple[int, str], int] = {}
        self.control_send_lock = asyncio.Lock()
        self.routes()

    def routes(self):
        @self.app.get("/_mesh/health")
        async def health():
            return {"ok": True, "target": self.target}

    async def local_ws(self, item: dict[str, Any], control):
        bridge_id = item["id"]
        queue = asyncio.Queue()
        self.ws_queues[bridge_id] = queue
        url = self.target.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + item["path"]
        if item.get("query"):
            url += "?" + item["query"]
        headers = {k: v for k, v in item.get("headers", {}).items()
                    if k.lower() not in {"host", "content-length", "sec-websocket-key", "sec-websocket-version",
                                        "sec-websocket-extensions", "connection", "upgrade", "origin", "cookie", "authorization", "sec-websocket-protocol"}}
        basic = self.cfg.get("opencode_basic_auth")
        auth = None
        if isinstance(basic, dict):
            auth = (str(basic.get("username", "")), str(basic.get("password", "")))
            headers["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        try:
            async with websockets.connect(url, additional_headers=headers,
                                          subprotocols=item.get("protocols") or None,
                                          max_size=None, origin=self.target) as target:
                await self.control_message(control, {"type": "ws_opened", "id": item["id"],
                                                     "protocol": target.subprotocol or ""})
                async def to_target():
                    while True:
                        msg = await queue.get()
                        if msg.get("type") == "ws_close":
                            await target.close(code=int(msg.get("code", 1000)), reason=str(msg.get("reason", "")) or None)
                            return
                        if msg.get("type") == "ws_data":
                            data = decode_strict(msg["data"]) if msg.get("kind") == "bytes" else msg.get("data", "")
                            await target.send(data)
                async def from_target():
                    async for raw in target:
                        if isinstance(raw, bytes):
                            payload = {"type": "ws_data", "id": item["id"], "kind": "bytes",
                                       "data": base64.b64encode(raw).decode()}
                        else:
                            payload = {"type": "ws_data", "id": item["id"], "kind": "text", "data": raw}
                        await self.control_message(control, payload)
                done, pending = await asyncio.wait(
                    [asyncio.create_task(to_target()), asyncio.create_task(from_target())],
                    return_when=asyncio.FIRST_COMPLETED)
                for task in pending: task.cancel()
                await asyncio.gather(*done, *pending, return_exceptions=True)
                code = target.close_code or 1000
                if code in {1005, 1006, 1015}: code = 1011
                await self.control_message(control, {"type": "ws_closed", "id": item["id"], "code": code})
        except Exception as exc:
            try:
                await self.control_message(control, {"type": "ws_error", "id": item["id"], "error": str(exc)})
            except Exception:
                pass
        finally:
            self.ws_queues.pop(bridge_id, None)

    async def local_stream(self, item: dict[str, Any], ws):
        if not is_valid_base64(item.get("body") or ""):
            # Invalid encoding is a protocol error: return a stable stream_error reason instead of a later decode exception.
            await self.stream_send(ws, {"type": "stream_error", "id": item["id"],
                                        "error": INVALID_ENCODING_REASON,
                                        "reason": INVALID_ENCODING_REASON})
            return
        url = self.target + item["path"]
        if item.get("query"): url += "?" + item["query"]
        # Drop browser CSRF markers: local OpenCode validates Origin/Referer against
        # its own origin, which never matches the Mesh gateway origin.
        headers = forwarding_headers(item.get("headers", {}))
        headers["accept-encoding"] = "identity"
        basic = self.cfg.get("opencode_basic_auth")
        auth = httpx.BasicAuth(str(basic["username"]), str(basic.get("password", ""))) if isinstance(basic, dict) else None
        guard = ResponseSizeGuard(response_limit(self.cfg))
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10), auth=auth, follow_redirects=True) as client:
                body = decode_strict(item.get("body", ""))
                async with client.stream(item["method"], url, headers=headers, content=body) as r:
                    out_headers = filter_response_headers(r.headers)
                    await self.stream_send(ws, {"type":"stream_chunk","id":item["id"],"status":r.status_code,"headers":out_headers})
                    async for chunk in r.aiter_bytes():
                        # Per-fragment cumulative limit: both a single oversized chunk and an over-budget stream end with a stable stream_error.
                        guard.add_bytes(chunk)
                        await self.stream_send(ws, {"type":"stream_chunk","id":item["id"],"body":base64.b64encode(chunk).decode()})
                    await self.stream_send(ws, {"type":"stream_end","id":item["id"]})
        except FrameError:
            with contextlib.suppress(Exception):
                await self.stream_send(ws, {"type":"stream_error","id":item["id"],
                                            "error":RESPONSE_TOO_LARGE_REASON,
                                            "reason":RESPONSE_TOO_LARGE_REASON})
        except Exception as exc:
            reason = CONNECTION_FAILED_REASON if isinstance(
                exc, (ConnectionError, httpx.ConnectError, httpx.TimeoutException)
            ) else UPSTREAM_ERROR_REASON
            with contextlib.suppress(Exception):
                await self.stream_send(ws, {"type":"stream_error","id":item["id"],
                                            "error":str(exc), "reason":reason})

    async def stream_send(self, ws, message: dict[str, Any]) -> None:
        if isinstance(ws, WebSocket):
            await self.send_control(ws, message)
        else:
            await ws.send(json.dumps(message))

    async def control_message(self, control, message: dict[str, Any]) -> None:
        if isinstance(control, WebSocket):
            await self.send_control(control, message)
        else:
            await control.send(json.dumps(message))

    async def _p2p_error(self, channel: Any, message_id: str, status: int, reason: str) -> None:
        """Reply with a protocol error response carrying a stable reason in the uniform envelope."""
        payload = json.dumps({"error": reason, "reason": reason}).encode()
        await self.p2p_send(channel, {"type": "response", "id": message_id, "status": status,
                                      "headers": {"content-type": "application/json"},
                                      "body": base64.b64encode(payload).decode()})

    async def p2p_message(self, channel: Any, item: dict[str, Any]) -> None:
        """Handle HTTP/SSE requests sent by the browser over the WebRTC DataChannel."""
        if "message_id" in item and "sequence" in item and "final" in item:
            # Fragment envelopes from the browser: reassemble per channel into a complete request before dispatching.
            assembler = self.p2p_assemblers.setdefault(
                id(channel), ChunkAssembler(limit=p2p_message_limit(self.cfg),
                                            budget=p2p_total_budget(self.cfg)))
            outcome = assembler.feed(item)
            message_id = str(item.get("message_id", ""))
            if outcome is None:
                return
            if outcome == "accepted":
                try:
                    assembled = assembler.result(message_id)
                    try:
                        restored = json.loads(assembled.decode("utf-8"))
                    except Exception:
                        await self._p2p_error(channel, message_id, 400, INVALID_ENCODING_REASON)
                        return
                    # HTTP requests must keep the outer transport ID consistent with the inner logical ID;
                    # WebSocket data frames use a fresh transport ID per frame and are routed by socket ID.
                    if restored.get("type") not in {"ws_data", "ws_close"} and str(restored.get("id", "")) != message_id:
                        await self._p2p_error(channel, message_id, 400, "message_id mismatch")
                        return
                    await self.p2p_message(channel, restored)
                finally:
                    # Keep a tombstone until TTL after completion to block replays of the same message_id.
                    assembler.complete(message_id)
            else:
                # Error tombstones last until TTL: replaying the same message_id keeps returning the same stable error.
                reason = assembler.reason_of(message_id) or "reassembly error"
                # Invalid encoding and out-of-order/replay are protocol errors (400); overflow and buffer exhaustion are capacity errors (413).
                status = 400 if reason in {INVALID_ENCODING_REASON, INVALID_SEQUENCE_REASON} else 413
                await self._p2p_error(channel, message_id, status, reason)
            return
        key = (id(channel), str(item.get("id", "")))
        if item.get("type") in {"ws_data", "ws_close"}:
            queue = self.ws_queues.get(item.get("id", ""))
            if queue:
                await queue.put(item)
            return
        if item.get("type") == "cancel":
            task = self.p2p_tasks.get(key)
            if task:
                task.cancel()
            # A cancel must also release this message_id's partial assembly.
            assembler = self.p2p_assemblers.get(id(channel))
            if assembler is not None:
                assembler.discard(str(item.get("id", "")))
            return
        if item.get("type") == "ping":
            await self.p2p_send(channel, {"type": "pong", "t": item.get("t")})
            return
        if item.get("type") not in {"request", "stream_request", "ws_open"}:
            return

        encoded_body = item.get("body")
        if isinstance(encoded_body, str):
            # Judge limits by the exact decoded length so base64 3-byte boundary rounding cannot admit an oversized request; invalid encoding is 400 directly.
            if not is_valid_base64(encoded_body):
                await self._p2p_error(channel, item.get("id", ""), 400, INVALID_ENCODING_REASON)
                return
            if decoded_size(encoded_body) > request_limit(self.cfg):
                await self._p2p_error(channel, item.get("id", ""), 413, REQUEST_TOO_LARGE_REASON)
                return

        class ChannelWriter:
            async def send(self, payload: str):
                await self_outer.p2p_send(channel, json.loads(payload))

        self_outer = self

        class P2PControl:
            async def send(self, payload: str):
                await self_outer.p2p_send(channel, json.loads(payload))

        if item["type"] == "request":
            async def run_request():
                result = await self.local_request(item, timeout=float(self.cfg.get("request_timeout", 120)))
                await self.p2p_send(channel, result)
        elif item["type"] == "stream_request":
            async def run_request():
                await self.local_stream(item, ChannelWriter())
        else:
            async def run_request():
                await self.local_ws(item, P2PControl())

        task = asyncio.create_task(run_request())
        self.p2p_tasks[key] = task
        try:
            await task
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self.p2p_send(channel, {"type": "cancelled", "id": item.get("id", "")})
        except Exception as exc:
            reason = CONNECTION_FAILED_REASON if isinstance(exc, ConnectionError) else UPSTREAM_ERROR_REASON
            await self.p2p_send(channel, {"type": "response", "id": item.get("id", ""), "status": 502,
                                          "headers": {"content-type": "application/json"},
                                          "body": base64.b64encode(json.dumps({"error": str(exc),
                                                                               "reason": reason}).encode()).decode()})
        finally:
            self.p2p_tasks.pop(key, None)

    def _p2p_next_sequence(self, channel: Any, message_id: str) -> int:
        key = (id(channel), message_id)
        value = self.p2p_sequence.get(key, 0)
        self.p2p_sequence[key] = value + 1
        return value

    async def p2p_send(self, channel: Any, message: dict[str, Any]) -> None:
        """Send responses and stream lifecycle messages in a uniform {message_id, sequence, data, final} envelope.

        Stream protocol: header frames (status/headers), data frames (stream_chunk),
        and terminal frames (stream_end/stream_error/cancelled) are all envelope
        frames; terminal frames have final=True and carry type/error metadata on
        the first frame, so the browser never needs special handling for bare messages.
        """
        message_type = message.get("type")
        message_id = str(message.get("id", ""))
        key = (id(channel), message_id)
        metadata = {k: v for k, v in message.items() if k not in {"body", "data"}}
        # Stream lifecycle terminal frames: a single frame with final=True carrying type/error metadata.
        if message_type in {"stream_end", "stream_error", "cancelled", "cancel"}:
            sequence = self.p2p_sequence.pop(key, 0)
            terminal = frame(message_id, sequence, b"", True)
            terminal.update(metadata)
            await self.p2p_channel_send(channel, terminal)
            return
        # Other control/WS messages (ws_open/ws_data/ws_closed/ws_error/pong etc.):
        # always send the full JSON as a data fragment so the frontend never has to inspect bare messages.
        if message_type not in {"response", "stream_chunk"}:
            payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
            await self._p2p_send_payload(channel, message_id, message_type, payload, {},
                                         streaming=False)
            return
        body = message.get("body")
        if not isinstance(body, str):
            if message_type == "response":
                # A response missing its body is a protocol error: raise, never send a hanging final=False frame.
                raise FrameError("response body must be a base64-encoded string")
            # Header frames without a body (stream_chunk) also use the envelope so the frontend handles them uniformly.
            sequence = self._p2p_next_sequence(channel, message_id)
            header = frame(message_id, sequence, b"", True)
            header.update(metadata)
            await self.p2p_channel_send(channel, header)
            return
        try:
            payload = decode_strict(body)
        except Exception:
            sequence = self.p2p_sequence.pop(key, 0)
            terminal = frame(message_id, sequence, b"", True)
            terminal.update({"type": "stream_error", "id": message_id,
                             "error": INVALID_ENCODING_REASON,
                             "reason": INVALID_ENCODING_REASON})
            await self.p2p_channel_send(channel, terminal)
            return
        await self._p2p_send_payload(channel, message_id, message_type, payload, metadata,
                                     streaming=(message_type == "stream_chunk"))

    async def _p2p_send_payload(self, channel: Any, message_id: str, message_type: str,
                                payload: bytes, metadata: dict[str, Any],
                                streaming: bool) -> None:
        """Send a payload in the envelope; while streaming, reuse an increasing sequence for the same message_id."""
        key = (id(channel), message_id)
        for index, chunk in enumerate(iter_frames(message_id, payload, CHUNK_SIZE)):
            frame_message = dict(chunk)
            if streaming:
                frame_message["sequence"] = self._p2p_next_sequence(channel, message_id)
                # Every stream_chunk call is one deliverable logical message; across multiple frames
                # only the last frame is final=True so the browser can tell message boundaries apart on the same message_id.
            if index == 0 and metadata:
                frame_message.update(metadata)
            await self.p2p_channel_send(channel, frame_message)
        if not streaming:
            self.p2p_sequence.pop(key, None)

    async def p2p_channel_send(self, channel: Any, message: dict[str, Any]) -> None:
        """Await the DataChannel buffer serially within a configured timeout.

        If the buffer does not fall back below the threshold within
        `p2p_send_timeout`, the channel is considered stalled: close it and raise
        ConnectionError instead of backing off forever.
        """
        payload = json.dumps(message)
        timeout = float(self.cfg.get("p2p_send_timeout", 10.0))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while getattr(channel, "bufferedAmount", 0) > 1024 * 1024:
            if channel.readyState != "open":
                raise ConnectionError("P2P channel closed")
            if loop.time() >= deadline:
                with contextlib.suppress(Exception):
                    getattr(channel, "close", lambda: None)()
                raise ConnectionError("P2P channel send timed out")
            await asyncio.sleep(0.01)
        channel.send(payload)

    async def reset_p2p_state(self) -> None:
        """When the control connection is rebuilt, close old P2P peers and clear tasks/assemblers/sequence/ws queues."""
        tasks = list(self.p2p_tasks.values())
        for task in tasks:
            task.cancel()
        self.p2p_tasks.clear()
        self.p2p_assemblers.clear()
        self.p2p_sequence.clear()
        self.ws_queues.clear()
        peers = list(self.p2p_peers)
        self.p2p_peers.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for peer in peers:
            with contextlib.suppress(Exception):
                await peer.close()

    async def handle_p2p_offer(self, item: dict[str, Any], control) -> None:
        limit = int(self.cfg.get("max_p2p_peers", 4))
        if len(self.p2p_peers) >= limit:
            with contextlib.suppress(Exception):
                await self.send_control(control, {"type": "p2p_answer", "id": item["id"],
                                                  "answer": {"error": "too many P2P sessions"}})
            return
        peer_holder: dict[str, Any] = {}
        channels: set[int] = set()
        async def receive(channel, message):
            channels.add(id(channel))
            await self.p2p_message(channel, message)
        async def peer_closed():
            peer = peer_holder.get("peer")
            if peer:
                self.p2p_peers.discard(peer)
            for key, task in list(self.p2p_tasks.items()):
                if key[0] in channels:
                    task.cancel()
            for channel_id in channels:
                self.p2p_assemblers.pop(channel_id, None)
            for sequence_key in [k for k in self.p2p_sequence if k[0] in channels]:
                self.p2p_sequence.pop(sequence_key, None)
        try:
            peer, answer = await answer_offer(
                item["offer"], receive, peer_closed,
                item.get("stun_servers") or [],
                loopback_candidate=bool(self.cfg.get("p2p_loopback_candidate", True)),
            )
            peer_holder["peer"] = peer
            self.p2p_peers.add(peer)
            await self.send_control(control, {"type": "p2p_answer", "id": item["id"], "answer": answer})
        except (P2PUnavailable, Exception):
            with contextlib.suppress(Exception):
                await self.send_control(control, {"type": "p2p_answer", "id": item["id"],
                                                  "answer": {"error": "P2P negotiation failed"}})

    async def handle_request(self, item: dict[str, Any], ws):
        try:
            result = await self.local_request(item, timeout=float(self.cfg.get("request_timeout", 120)))
            await self.send_control(ws, result)
        except Exception as exc:
            try:
                reason = CONNECTION_FAILED_REASON if isinstance(exc, ConnectionError) else UPSTREAM_ERROR_REASON
                await self.send_control(ws, {"type": "response", "id": item.get("id", ""), "status": 502,
                                          "headers": {"content-type": "application/json"},
                                          "body": base64.b64encode(
                                              json.dumps({"error": str(exc),
                                                          "reason": reason}).encode()).decode()})
            except Exception:
                pass

    async def local_request(self, item: dict[str, Any], timeout: float = 120) -> dict[str, Any]:
        if not is_valid_base64(item.get("body") or ""):
            # Invalid base64 is a protocol error: return 400 instead of letting decode_strict raise and become a 502.
            payload = json.dumps({"error": INVALID_ENCODING_REASON,
                                  "reason": INVALID_ENCODING_REASON}).encode()
            return {"type": "response", "id": item["id"], "status": 400, "headers": {"content-type": "application/json"},
                    "body": base64.b64encode(payload).decode()}
        url = self.target + item["path"]
        if item.get("query"):
            url += "?" + item["query"]
        # Drop browser CSRF markers: local OpenCode validates Origin/Referer against
        # its own origin, which never matches the Mesh gateway origin.
        headers = forwarding_headers(item.get("headers", {}))
        headers["accept-encoding"] = "identity"
        auth = None
        basic = self.cfg.get("opencode_basic_auth")
        if isinstance(basic, dict) and basic.get("username") is not None:
            auth = httpx.BasicAuth(str(basic["username"]), str(basic.get("password", "")))
        limit = response_limit(self.cfg)
        async with httpx.AsyncClient(timeout=timeout, auth=auth, follow_redirects=True) as client:
            try:
                body = decode_strict(item.get("body", ""))
                async with client.stream(item["method"], url, headers=headers, content=body) as r:
                    content = await read_bounded(r.aiter_bytes(), limit)
                encoded = base64.b64encode(content).decode()
                print(f"agent response id={item['id']} status={r.status_code} bytes={len(content)} encoded={len(encoded)}", flush=True)
                return {"type": "response", "id": item["id"], "status": r.status_code,
                        "headers": filter_response_headers(r.headers),
                        "body": encoded}
            except FrameError as exc:
                return {"type": "response", "id": item["id"], "status": 502, "headers": {"content-type": "application/json"},
                        "body": base64.b64encode(json.dumps({"error": str(exc),
                                                             "reason": str(exc)}).encode()).decode()}
            except Exception as exc:
                reason = CONNECTION_FAILED_REASON if isinstance(
                    exc, (ConnectionError, httpx.ConnectError, httpx.TimeoutException)
                ) else UPSTREAM_ERROR_REASON
                return {"type": "response", "id": item["id"], "status": 502, "headers": {"content-type": "application/json"},
                        "body": base64.b64encode(json.dumps({"error": str(exc),
                                                             "reason": reason}).encode()).decode()}

    async def control_heartbeat(self, ws):
        interval = float(self.cfg.get("heartbeat_seconds", 15))
        while True:
            await asyncio.sleep(interval)
            try:
                await self.send_control(ws, {"type": "pong"})
            except Exception:
                # Heartbeat timeout/failure: close the connection so run() rebuilds instead of blocking forever.
                with contextlib.suppress(Exception):
                    await ws.close(code=1011)
                return

    async def send_control(self, ws, message: dict[str, Any],
                           timeout: float = CONTROL_SEND_TIMEOUT) -> None:
        """Bounded control send: serialize writes; timeout or a closed peer both raise a connection-level error to trigger a rebuild."""
        try:
            async with self.control_send_lock:
                await asyncio.wait_for(ws.send(json.dumps(message)), timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                await ws.close(code=1011)
            raise ConnectionError("Agent control send timed out")
        except Exception as exc:
            # Writing to a closed socket raises RuntimeError/ConnectionError; normalize both to a connection-level error.
            with contextlib.suppress(Exception):
                await ws.close(code=1011)
            raise ConnectionError("Agent control connection closed") from exc

    async def run(self):
        gateway_url = str(self.cfg.get("gateway_url", "")).rstrip("/")
        scheme = urlparse(gateway_url).scheme
        if scheme not in {"https", "wss"} and not self.cfg.get("allow_insecure_gateway"):
            raise SystemExit("gateway_url must use https:// (set allow_insecure_gateway for local testing)")
        state = Path(self.cfg.get("state_file", "./data/agent-state.json"))
        state.parent.mkdir(parents=True, exist_ok=True)
        if state.exists():
            harden_permissions(state)
        data = json.loads(state.read_text()) if state.exists() else {"device_id": make_id()}
        private_json(state, data)
        ws_url = gateway_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
        attempt = 0
        while True:
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    r = await client.post(gateway_url + "/_mesh/register", json={
                        "device_id": data["device_id"], "name": self.cfg.get("device_name") or hostname(), "platform": platform.platform(),
                         "enroll_token": self.cfg["enroll_token"], "agent_token": data.get("agent_token", ""),
                         "rotate_token": bool(data.pop("rotate_token", False))})
                    if r.status_code == 429:
                        attempt += 1
                        delay = backoff_delay(attempt, retry_after=parse_retry_after(r.headers.get("Retry-After")))
                        print(f"agent register rate limited, retry in {delay:.1f}s", flush=True)
                        await asyncio.sleep(delay)
                        continue
                    if r.status_code == 403 and "ownership" in r.text.lower():
                        # The locally persisted token is out of sync with Gateway state: rotate the identity with enroll_token and retry.
                        data["rotate_token"] = True
                        attempt += 1
                        delay = backoff_delay(attempt)
                        print(f"agent identity recovery scheduled in {delay:.1f}s", flush=True)
                        await asyncio.sleep(delay)
                        continue
                    r.raise_for_status()
                    data.update(r.json())
                    private_json(state, data)
                attempt = 0
                async with websockets.connect(
                    f"{ws_url}/_mesh/agent/{data['device_id']}",
                    additional_headers={"X-Mesh-Agent-Token": data['agent_token']},
                    max_size=None, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
                    tasks = {}
                    await self.reset_p2p_state()
                    heartbeat = asyncio.create_task(self.control_heartbeat(ws))
                    await self.send_control(ws, {"type": "agent_hello"})
                    try:
                        async for raw in ws:
                            item = json.loads(raw)
                            handler = {"request": self.handle_request, "stream_request": self.local_stream,
                                       "ws_open": self.local_ws}.get(item.get("type"))
                            if handler:
                                request_id = item["id"]
                                previous = tasks.get(request_id)
                                if previous: previous.cancel()
                                task = asyncio.create_task(handler(item, ws))
                                tasks[request_id] = task
                                def finish(completed, key=request_id):
                                    if tasks.get(key) is completed:
                                        tasks.pop(key, None)
                                    if not completed.cancelled():
                                        completed.exception()
                                task.add_done_callback(finish)
                            elif item.get("type") == "cancel":
                                task = tasks.get(item.get("id"))
                                if task: task.cancel()
                            elif item.get("type") == "p2p_offer":
                                p2p_key = f"p2p:{item.get('id')}"
                                previous = tasks.get(p2p_key)
                                if previous: previous.cancel()
                                task = asyncio.create_task(self.handle_p2p_offer(item, ws))
                                tasks[p2p_key] = task
                                def finish_p2p(completed, key=p2p_key):
                                    if tasks.get(key) is completed:
                                        tasks.pop(key, None)
                                    if not completed.cancelled():
                                        completed.exception()
                                task.add_done_callback(finish_p2p)
                            elif item.get("type") == "ping":
                                await self.send_control(ws, {"type": "pong"})
                            elif item.get("type") in {"ws_data", "ws_close"}:
                                q = self.ws_queues.get(item.get("id", ""))
                                if q:
                                    await q.put(item)
                    finally:
                        heartbeat.cancel()
                        remaining = list(tasks.values())
                        for task in remaining: task.cancel()
                        await asyncio.gather(*remaining, return_exceptions=True)
                        with contextlib.suppress(asyncio.CancelledError):
                            await heartbeat
                        # Connection dropped/rebuild: close old P2P peers and clear all associated state.
                        await self.reset_p2p_state()
            except Exception as exc:
                attempt += 1
                delay = backoff_delay(attempt)
                print(f"agent connection/register retry: {type(exc).__name__}: {exc}; retry in {delay:.1f}s", flush=True)
                await asyncio.sleep(delay)



def inject_mesh_bar(body: bytes) -> bytes:
    """Inject the adapter only into V2 pages; V1 pages keep the native transport implementation."""
    # V1 uses /assets, V2 uses /_assets. Their APIs, event streams, and WebSocket
    # protocols differ; injecting the V2 adapter into V1 would make health checks return HTML and break the whole frontend.
    if b"/assets/" in body and b"/_assets/" not in body:
        return body
    adapter = TRANSPORT_ADAPTER.replace(
        "__OCM_VERSION_JSON__", json.dumps(__version__, ensure_ascii=True)
    ).encode("utf-8")
    if b"</head>" in body and b"ocm-transport-adapter" not in body:
        body = body.replace(b"</head>", adapter + b"</head>", 1)
    return body




def resolve_agent_config(path: str | Path, cfg: dict[str, Any], instance: str | None) -> dict[str, Any]:
    """The shared configuration stores only manual settings; identity paths are derived from the install directory and instance name."""
    if "agents" not in cfg:
        if instance is not None:
            raise ValueError("--instance requires a shared agents configuration")
        return dict(cfg)
    name = "default" if instance is None else instance
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("Invalid agent instance name")
    agents = cfg["agents"]
    if not isinstance(agents, dict) or name not in agents or not isinstance(agents[name], dict):
        raise ValueError("Agent instance not configured")
    if "state_file" in cfg or "state_file" in agents[name]:
        raise ValueError("Shared configuration manages identity paths automatically")
    result = {key: value for key, value in cfg.items() if key != "agents"}
    result.update(agents[name])
    filename = "agent-state.json" if name == "default" else f"agent-state-{name}.json"
    result["state_file"] = str(Path(path).resolve().parent.parent / "data" / filename)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--mode", choices=["gateway", "agent"], required=True)
    parser.add_argument("--instance", help="Select an agent from the shared configuration")
    args = parser.parse_args()
    cfg = load_json(args.config)
    if args.mode == "gateway":
        if args.instance is not None:
            parser.error("--instance is only supported in agent mode")
        # ws_max_size must cover the application request limit (base64 expansion + JSON overhead);
        # otherwise the default 16MiB would disconnect large responses before the application limit applies.
        uvicorn.run(Gateway(cfg).app, host=cfg.get("listen_host", "127.0.0.1"), port=int(cfg.get("listen_port", 8090)),
                    log_level="info", timeout_graceful_shutdown=5, ws_max_size=ws_frame_limit(cfg))
    else:
        try:
            cfg = resolve_agent_config(args.config, cfg, args.instance)
        except ValueError as exc:
            parser.error(str(exc))
        agent = Agent(cfg)
        asyncio.run(agent.run())


if __name__ == "__main__":
    main()
