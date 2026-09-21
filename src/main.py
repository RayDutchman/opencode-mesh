from __future__ import annotations
import argparse, asyncio, base64, contextlib, hmac, json, os, platform, re, secrets, socket, time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import uvicorn
import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from .p2p import P2PUnavailable, answer_offer
from .static_adapter import TRANSPORT_ADAPTER


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


def forwarding_headers(headers) -> dict[str, str]:
    """Never send gateway credentials across the Agent trust boundary."""
    blocked = {'authorization', 'cookie', 'proxy-authorization', 'host', 'content-length',
               'forwarded', 'x-forwarded-for', 'x-forwarded-proto', 'x-forwarded-host',
               'x-forwarded-port', 'x-real-ip'}
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


class Gateway:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.registry = Registry(cfg)
        self.pending: dict[str, asyncio.Future] = {}
        self.streams: dict[str, asyncio.Queue] = {}
        self.browser_ws: dict[str, WebSocket] = {}
        self.owners: dict[str, WebSocket] = {}
        self.p2p_answers: dict[str, asyncio.Future] = {}
        self.device_send_locks: dict[int, asyncio.Lock] = {}
        self.browser_send_locks: dict[str, asyncio.Lock] = {}
        self.auth_failures: dict[str, list[float]] = {}
        self.register_attempts: dict[str, list[float]] = {}
        self.app = FastAPI(title="OpenCode Mesh Gateway")
        self.routes()

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

        @app.on_event("shutdown")
        async def shutdown_bridges():
            for queue in self.streams.values():
                queue.put_nowait({"type": "stream_error", "error": "Gateway is shutting down"})
            for bridge in list(self.browser_ws.values()):
                with contextlib.suppress(Exception):
                    await bridge.close(code=1001)

        @app.get("/_mesh/devices")
        async def devices(req: Request):
            return {"devices": self.registry.public(), "default_device": self.resolve_default_device()}

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
            try:
                await self.send_to_device(agent_ws, {"type": "p2p_offer", "id": session_id,
                                                     "offer": data, "stun_servers": self.cfg.get("stun_servers") or DEFAULT_STUN_SERVERS})
                return JSONResponse(await asyncio.wait_for(future, 20))
            except Exception:
                return JSONResponse({"error": "P2P connection failed"}, status_code=502)
            finally:
                self.p2p_answers.pop(session_id, None)

        @app.websocket("/_mesh/ws/{path:path}")
        @app.websocket("/_mesh/device/{path:path}")
        @app.websocket("/api/pty/{path:path}")
        @app.websocket("/pty/{path:path}")
        async def browser_ws(client: WebSocket, path: str):
            if not self.check_auth(client):
                await client.close(code=4401)
                return
            explicit_device = None
            if client.url.path.startswith("/_mesh/device/"):
                try:
                    explicit_device, path = self.parse_device_route(client.url.path)
                    path = path.lstrip("/")
                except ValueError:
                    await client.close(code=4404)
                    return
            device_id = explicit_device or self.cfg.get("default_device")
            d = self.registry.devices.get(str(device_id))
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
                await self.send_to_device(agent_ws, open_item)
                while True:
                    msg = await client.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
                    if msg.get("text") is not None:
                        await self.send_to_device(agent_ws, {"type": "ws_data", "id": bridge_id,
                            "kind": "text", "data": msg["text"]})
                    elif msg.get("bytes") is not None:
                        await self.send_to_device(agent_ws, {"type": "ws_data", "id": bridge_id,
                            "kind": "bytes", "data": base64.b64encode(msg["bytes"]).decode()})
            except Exception:
                pass
            finally:
                try:
                    await self.send_to_device(agent_ws, {"type": "ws_close", "id": bridge_id})
                except Exception:
                    pass
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
                return JSONResponse({"error": "Device ownership proof required"}, status_code=403)
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
            d["ws"] = ws
            d["last_seen"] = int(time.time())
            try:
                while True:
                    msg = await ws.receive()
                    if msg.get("type") == "websocket.disconnect":
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
                                payload = base64.b64decode(item.get("data", "")) if binary else item.get("data", "")
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
                        q = self.streams.get(item.get("id", ""))
                        if q:
                            self.enqueue_stream(q, item)
                    elif item.get("type") in {"stream_end", "stream_error"}:
                        q = self.streams.get(item.get("id", ""))
                        if q:
                            self.enqueue_stream(q, item)
                    elif item.get("type") == "p2p_answer":
                        future = self.p2p_answers.get(item.get("id", ""))
                        if future and not future.done():
                            future.set_result(item.get("answer", {}))
            except (WebSocketDisconnect, Exception):
                pass
            finally:
                self.device_send_locks.pop(id(ws), None)
                if d.get("ws") is ws:
                    d.pop("ws", None)
                for request_id, owner in list(self.owners.items()):
                    if owner is not ws:
                        continue
                    future = self.pending.get(request_id)
                    if future and not future.done():
                        future.set_exception(ConnectionError("Device control connection lost"))
                    queue = self.streams.get(request_id)
                    if queue is not None:
                        queue.put_nowait({"type": "stream_error", "error": "Device control connection lost"})
                    bridge = self.browser_ws.get(request_id)
                    if bridge:
                        with contextlib.suppress(Exception):
                            await bridge.close(code=1011)
                    self.owners.pop(request_id, None)

        @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
        async def proxy(req: Request, path: str):
            explicit_device = None
            try:
                explicit_device, routed_path = self.parse_device_route("/" + path)
            except ValueError:
                return JSONResponse({"error": "Invalid device route"}, status_code=404)
            if explicit_device:
                path = routed_path.lstrip("/")
            elif path.startswith("_mesh/"):
                return JSONResponse({"error": "not found"}, status_code=404)

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
            if len(body) > int(self.cfg.get("max_request_bytes", 64 * 1024 * 1024)):
                return JSONResponse({"error": "Request body too large"}, status_code=413)
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
                await asyncio.wait_for(self.send_to_device(ws, item), timeout=10)
                result = await asyncio.wait_for(future, timeout=float(self.cfg.get("request_timeout", 120)))
                completed = True
            except Exception:
                self.pending.pop(request_id, None)
                return JSONResponse({"error": "Device connection failed"}, status_code=502)
            finally:
                self.pending.pop(request_id, None)
                self.owners.pop(request_id, None)
                if not completed:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(self.send_to_device(ws, {"type": "cancel", "id": request_id}), 2)
            headers = {k: v for k, v in result.get("headers", {}).items()
                       if k.lower() not in {"content-length", "transfer-encoding", "connection", "content-security-policy", "x-frame-options"}}
            body = base64.b64decode(result.get("body", ""))
            print(f"proxy response id={request_id} status={result.get('status')} encoded={len(result.get('body', ''))} decoded={len(body)}", flush=True)
            content_type = headers.get("content-type", headers.get("Content-Type", ""))
            if "text/html" in content_type.lower() and int(result.get("status", 502)) == 200:
                body = inject_mesh_bar(body)
            headers["content-length"] = str(len(body))
            return Response(body, status_code=int(result.get("status", 502)), headers=headers)

    async def stream_proxy(self, req: Request, d: dict[str, Any], ws: WebSocket,
                           path: str, request_id: str, body: bytes):
        if len(body) > int(self.cfg.get("max_request_bytes", 64 * 1024 * 1024)):
            return JSONResponse({"error": "Request body too large"}, status_code=413)
        q: asyncio.Queue = asyncio.Queue(maxsize=int(self.cfg.get("stream_queue_size", 2048)))
        self.streams[request_id] = q
        self.owners[request_id] = ws
        item = {"type": "stream_request", "id": request_id, "method": req.method,
                "path": "/" + path, "query": req.url.query, "headers": forwarding_headers(req.headers),
                "body": base64.b64encode(body).decode()}
        try:
            await self.send_to_device(ws, item)
            first = await asyncio.wait_for(q.get(), timeout=30)
            if first.get("type") == "stream_error":
                self.streams.pop(request_id, None)
                return JSONResponse({"error": first.get("error", "stream failed")}, status_code=502)
            headers = {k: v for k, v in first.get("headers", {}).items()
                       if k.lower() not in {"content-length", "content-encoding", "transfer-encoding", "connection"}}
            status = int(first.get("status", 200))
            async def body_iter():
                ended = False
                try:
                    while True:
                        msg = await q.get()
                        if msg.get("type") == "stream_end":
                            ended = True
                            break
                        if msg.get("type") == "stream_error":
                            raise ConnectionError(msg.get("error", "Event stream disconnected"))
                        if msg.get("type") == "stream_chunk":
                            yield base64.b64decode(msg.get("body", ""))
                finally:
                    self.streams.pop(request_id, None)
                    self.owners.pop(request_id, None)
                    if not ended:
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(self.send_to_device(ws, {"type": "cancel", "id": request_id}), 2)
            return StreamingResponse(body_iter(), status_code=status, headers=headers)
        except Exception:
            self.streams.pop(request_id, None)
            self.owners.pop(request_id, None)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.send_to_device(ws, {"type": "cancel", "id": request_id}), 2)
            return JSONResponse({"error": "Event stream connection failed"}, status_code=502)

    @staticmethod
    def enqueue_stream(queue: asyncio.Queue, item: dict[str, Any]) -> None:
        """Drop the oldest non-status event when the browser consumes slowly.

        Never evict the first frame: it carries the upstream status and headers, so
        losing it would make the gateway synthesize a wrong response.
        """
        try:
            queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass
        kept: list[Any] = []
        dropped = False
        while True:
            try:
                pending = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not dropped and not (isinstance(pending, dict) and "status" in pending):
                dropped = True
                continue
            kept.append(pending)
        if not dropped and kept:
            kept.pop(0)
        for pending in kept:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(pending)
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(item)


class Agent:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.app = FastAPI(title="OpenCode Mesh Agent")
        self.target = cfg["opencode_url"].rstrip("/")
        self.ws_queues: dict[str, asyncio.Queue] = {}
        self.p2p_peers: set[Any] = set()
        self.p2p_tasks: dict[tuple[int, str], asyncio.Task] = {}
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
            async with websockets.connect(url, additional_headers=headers, subprotocols=[],
                                          max_size=None, origin=self.target) as target:
                await self.control_message(control, {"type": "ws_opened", "id": item["id"]})
                async def to_target():
                    while True:
                        msg = await queue.get()
                        if msg.get("type") == "ws_close":
                            await target.close(code=int(msg.get("code", 1000)), reason=str(msg.get("reason", "")) or None)
                            return
                        if msg.get("type") == "ws_data":
                            data = base64.b64decode(msg["data"]) if msg.get("kind") == "bytes" else msg.get("data", "")
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
        url = self.target + item["path"]
        if item.get("query"): url += "?" + item["query"]
        headers = {k: v for k, v in item.get("headers", {}).items() if k.lower() not in {"host", "content-length", "authorization", "cookie"}}
        basic = self.cfg.get("opencode_basic_auth")
        auth = httpx.BasicAuth(str(basic["username"]), str(basic.get("password", ""))) if isinstance(basic, dict) else None
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10), auth=auth) as client:
                async with client.stream(item["method"], url, headers=headers, content=base64.b64decode(item.get("body", ""))) as r:
                    out_headers = {k: v for k, v in r.headers.items() if k.lower() not in {"content-encoding", "content-length", "transfer-encoding", "connection"}}
                    await self.stream_send(ws, {"type":"stream_chunk","id":item["id"],"status":r.status_code,"headers":out_headers})
                    async for chunk in r.aiter_bytes():
                        await self.stream_send(ws, {"type":"stream_chunk","id":item["id"],"body":base64.b64encode(chunk).decode()})
                    await self.stream_send(ws, {"type":"stream_end","id":item["id"]})
        except Exception as exc:
            with contextlib.suppress(Exception):
                await self.stream_send(ws, {"type":"stream_error","id":item["id"],"error":str(exc)})

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

    async def p2p_message(self, channel: Any, item: dict[str, Any]) -> None:
        """Handle HTTP/SSE requests sent by the browser over the WebRTC DataChannel."""
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
            return
        if item.get("type") == "ping":
            await self.p2p_send(channel, {"type": "pong", "t": item.get("t")})
            return
        if item.get("type") not in {"request", "stream_request", "ws_open"}:
            return

        encoded_body = item.get("body")
        if isinstance(encoded_body, str):
            limit = int(self.cfg.get("max_request_bytes", 64 * 1024 * 1024))
            if len(encoded_body) > limit * 4 // 3 + 4:
                await self.p2p_send(channel, {"type": "response", "id": item.get("id", ""), "status": 413,
                                              "headers": {},
                                              "body": base64.b64encode(json.dumps({"error": "Request body too large"}).encode()).decode()})
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
            await self.p2p_send(channel, {"type": "response", "id": item.get("id", ""), "status": 502,
                                          "headers": {}, "body": base64.b64encode(json.dumps({"error": str(exc)}).encode()).decode()})
        finally:
            self.p2p_tasks.pop(key, None)

    async def p2p_send(self, channel: Any, message: dict[str, Any]) -> None:
        """Split large responses to fit the DataChannel limit, avoiding P2P closure from large provider/file responses."""
        body = message.get("body")
        chunk_size = 32768
        if not isinstance(body, str) or len(body) <= chunk_size:
            await self.p2p_channel_send(channel, message)
            return
        if message.get("type") == "response":
            await self.p2p_channel_send(channel, {k: v for k, v in message.items() if k != "body"} | {
                "type": "response_start", "id": message.get("id", ""),
            })
            for offset in range(0, len(body), chunk_size):
                await self.p2p_channel_send(channel, {"type": "response_chunk", "id": message["id"],
                                         "body": body[offset:offset + chunk_size]})
            await self.p2p_channel_send(channel, {"type": "response_end", "id": message["id"]})
            return
        if message.get("type") == "stream_chunk":
            for offset in range(0, len(body), chunk_size):
                await self.p2p_channel_send(channel, {"type": "stream_chunk", "id": message["id"],
                                         "body": body[offset:offset + chunk_size]})
            return
        await self.p2p_channel_send(channel, message)

    async def p2p_channel_send(self, channel: Any, message: dict[str, Any]) -> None:
        """Await the DataChannel buffer serially to avoid overwhelming the browser channel with large responses."""
        payload = json.dumps(message)
        while getattr(channel, "bufferedAmount", 0) > 1024 * 1024:
            if channel.readyState != "open":
                raise ConnectionError("P2P channel closed")
            await asyncio.sleep(0.01)
        channel.send(payload)

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
        try:
            peer, answer = await answer_offer(
                item["offer"], receive, peer_closed,
                item.get("stun_servers") or [],
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
                await self.send_control(ws, {"type": "response", "id": item.get("id", ""), "status": 502,
                                          "headers": {}, "body": base64.b64encode(
                                              json.dumps({"error": str(exc)}).encode()).decode()})
            except Exception:
                pass

    async def local_request(self, item: dict[str, Any], timeout: float = 120) -> dict[str, Any]:
        url = self.target + item["path"]
        if item.get("query"):
            url += "?" + item["query"]
        headers = {k: v for k, v in item.get("headers", {}).items() if k.lower() not in {"host", "content-length", "authorization", "cookie"}}
        auth = None
        basic = self.cfg.get("opencode_basic_auth")
        if isinstance(basic, dict) and basic.get("username") is not None:
            auth = httpx.BasicAuth(str(basic["username"]), str(basic.get("password", "")))
        async with httpx.AsyncClient(timeout=timeout, auth=auth) as client:
            try:
                r = await client.request(item["method"], url, headers=headers, content=base64.b64decode(item.get("body", "")))
                encoded = base64.b64encode(r.content).decode()
                print(f"agent response id={item['id']} status={r.status_code} bytes={len(r.content)} encoded={len(encoded)}", flush=True)
                return {"type": "response", "id": item["id"], "status": r.status_code,
                        "headers": {k: v for k, v in r.headers.items()
                                    if k.lower() not in {"content-encoding", "content-length", "transfer-encoding", "connection"}},
                        "body": encoded}
            except Exception as exc:
                return {"type": "response", "id": item["id"], "status": 502, "headers": {},
                        "body": base64.b64encode(json.dumps({"error": str(exc)}).encode()).decode()}

    async def control_heartbeat(self, ws):
        interval = float(self.cfg.get("heartbeat_seconds", 15))
        while True:
            await asyncio.sleep(interval)
            await self.send_control(ws, {"type": "pong"})

    async def send_control(self, ws, message: dict[str, Any]) -> None:
        """Serialize all control-plane writes to avoid interleaving responses, heartbeats, and stream frames."""
        async with self.control_send_lock:
            await ws.send(json.dumps(message))

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
        reconnect = float(self.cfg.get("reconnect_seconds", 5))
        while True:
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    r = await client.post(gateway_url + "/_mesh/register", json={
                        "device_id": data["device_id"], "name": hostname(), "platform": platform.platform(),
                         "enroll_token": self.cfg["enroll_token"], "agent_token": data.get("agent_token", "")})
                    r.raise_for_status()
                    data.update(r.json())
                    private_json(state, data)
                async with websockets.connect(
                    f"{ws_url}/_mesh/agent/{data['device_id']}",
                    additional_headers={"X-Mesh-Agent-Token": data['agent_token']},
                    max_size=None, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
                    tasks = {}
                    heartbeat = asyncio.create_task(self.control_heartbeat(ws))
                    await ws.send(json.dumps({"type": "agent_hello"}))
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
                                await ws.send(json.dumps({"type": "pong"}))
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
            except Exception as exc:
                print(f"agent connection/register retry: {type(exc).__name__}: {exc}", flush=True)
                await asyncio.sleep(reconnect)



def inject_mesh_bar(body: bytes) -> bytes:
    """Inject only the transport adapter; the device list is handled by the native OpenCode Server UI."""
    adapter = TRANSPORT_ADAPTER.encode("utf-8")
    if b"</head>" in body and b"ocm-transport-adapter" not in body:
        body = body.replace(b"</head>", adapter + b"</head>", 1)
    return body




def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--mode", choices=["gateway", "agent"], required=True)
    args = parser.parse_args()
    cfg = load_json(args.config)
    if args.mode == "gateway":
        uvicorn.run(Gateway(cfg).app, host=cfg.get("listen_host", "127.0.0.1"), port=int(cfg.get("listen_port", 8090)), log_level="info", timeout_graceful_shutdown=5)
    else:
        agent = Agent(cfg)
        asyncio.run(agent.run())


if __name__ == "__main__":
    main()
