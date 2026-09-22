from __future__ import annotations


TRANSPORT_ADAPTER = r"""
<script id="ocm-transport-adapter">
(() => {
  const MESH_VERSION = __OCM_VERSION_JSON__;
  const nativeFetch = window.fetch.bind(window);
  const nativeWebSocket = window.WebSocket;
  const nativeEventSource = window.EventSource;
  const enc = new TextEncoder();
  const dec = new TextDecoder();
  const b64 = bytes => { let s = ''; for (const b of bytes) s += String.fromCharCode(b); return btoa(s); };
  const unb64 = text => Uint8Array.from(atob(text || ''), c => c.charCodeAt(0));
  const timeout = (promise, ms) => Promise.race([promise, new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), ms))]);
  const makeId = () => {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') return globalThis.crypto.randomUUID();
    return 'ocm-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
  };
  const CHUNK_SIZE = 32768;
  const MAX_P2P_BYTES = 64 * 1024 * 1024;
  // P2P request bodies expand through base64 and JSON; larger bodies use Relay.
  const MAX_P2P_BODY = 32 * 1024 * 1024;
  // Bound backpressure waits to match the Agent's default send timeout.
  const SEND_TIMEOUT_MS = 10000;
  // Bound the whole P2P setup, including manifest and answer fetches.
  const CONNECT_TIMEOUT_MS = 40000;
  // Reconnect an SSE stream that receives no bytes, including heartbeat comments.
  const SSE_IDLE_MS = 45000;
  // Sanity cap: a real round-trip is far below this; anything larger is a clock-jump artifact (lock screen / background timer freeze) and must be discarded.
  const RTT_MAX_MS = 10000;

  const state = { manifest: null, pc: null, channel: null, ready: null, pending: new Map(), streams: new Map(), sockets: new Map(), incoming: new Map(), closed: false, deviceId: null, routeDeviceId: undefined, generation: 0, reconnectTimer: null, reconnectDelay: 1000, devices: [], defaultDevice: null, rtt: null, pingSent: null, pingTimer: null, relayRtt: null, p2pSendTail: Promise.resolve() };

  const BAR_CSS = `
  #ocm-mesh-bar{display:flex;align-items:center;gap:8px;height:36px;padding:0 10px;font-size:13px;line-height:20px;flex:0 0 auto;border-bottom:1px solid var(--v2-border-border-base);background:var(--v2-background-bg-layer-01);color:var(--v2-text-text-muted);-webkit-user-select:none;user-select:none}
  #ocm-mesh-bar .ocm-title{font-weight:600;color:var(--v2-text-text-base)}
  #ocm-mesh-bar .ocm-version{font-size:11px;color:var(--v2-text-text-faint);white-space:nowrap}
  #ocm-mesh-bar .ocm-device{border:1px solid var(--v2-border-border-base);border-radius:6px;padding:2px 8px;background:var(--v2-background-bg-layer-02);color:var(--v2-text-text-base);max-width:40vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #ocm-mesh-bar .ocm-device[data-offline="true"]{color:var(--v2-text-text-faint)}
  #ocm-mesh-bar .ocm-transport{margin-left:auto;display:flex;align-items:center;gap:6px;color:var(--v2-text-text-base)}
  #ocm-mesh-bar .ocm-dot{width:8px;height:8px;border-radius:9999px;background:#22c55e}
  #ocm-mesh-bar .ocm-dot[data-kind="relay"]{background:#3b82f6}
  #root{height:calc(100dvh - 36px)}
  `;

  function ensureBarStyle() {
    if (document.getElementById('ocm-mesh-bar-style')) return;
    const style = document.createElement('style');
    style.id = 'ocm-mesh-bar-style';
    style.textContent = BAR_CSS;
    document.head.appendChild(style);
  }

  function ensureBar() {
    let bar = document.getElementById('ocm-mesh-bar');
    if (bar || !document.body) return bar;
    ensureBarStyle();
    bar = document.createElement('div');
    bar.id = 'ocm-mesh-bar';
    const title = document.createElement('span');
    title.className = 'ocm-title';
    title.textContent = 'OpenCode Mesh';
    const version = document.createElement('span');
    version.className = 'ocm-version';
    version.textContent = 'v' + MESH_VERSION;
    const device = document.createElement('span');
    device.className = 'ocm-device';
    const transport = document.createElement('span');
    transport.className = 'ocm-transport';
    bar.append(title, version, device, transport);
    document.body.insertBefore(bar, document.body.firstChild);
    return bar;
  }

  function currentDeviceInfo() {
    const routeId = currentDeviceId();
    const id = routeId || (state.manifest && state.manifest.device_id) || state.defaultDevice;
    const device = state.devices.find(item => item.device_id === id);
    return { name: (device && device.name) || id || 'no device', online: device ? !!device.online : undefined };
  }

  function transportInfo() {
    if (state.channel && state.channel.readyState === 'open') {
      return { kind: 'p2p', label: state.rtt != null ? 'P2P ' + state.rtt + 'ms' : 'P2P' };
    }
    return { kind: 'relay', label: state.relayRtt != null ? 'Relay ' + state.relayRtt + 'ms' : 'Relay' };
  }

  async function measureRelayRtt() {
    if (state.channel && state.channel.readyState === 'open') return;
    if (document.hidden) return;
    const deviceId = currentDeviceId();
    const base = deviceId ? '/_mesh/device/' + encodeURIComponent(deviceId) : '';
    const started = Date.now();
    try {
      const response = await nativeFetch(base + '/global/health', { credentials: 'same-origin', cache: 'no-store' });
      const elapsed = Date.now() - started;
      if (response.ok && !document.hidden && elapsed >= 0 && elapsed <= RTT_MAX_MS) { state.relayRtt = elapsed; renderBar(); }
    } catch (_) {}
  }

  function renderBar() {
    const bar = ensureBar();
    if (!bar) return;
    const info = currentDeviceInfo();
    const transport = transportInfo();
    const device = bar.querySelector('.ocm-device');
    device.textContent = info.name;
    device.dataset.offline = String(info.online === false);
    bar.querySelector('.ocm-transport').innerHTML =
      '<span class="ocm-dot" data-kind="' + transport.kind + '"></span><span>' + transport.label + '</span>';
  }

  const requestPath = input => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    return { path: url.pathname, query: url.search.slice(1) };
  };
  const virtualDeviceId = path => {
    const match = path.match(/^\/_mesh\/device\/([^/]+)(\/.*)?$/);
    return match ? decodeURIComponent(match[1]) : null;
  };
  const devicePath = path => {
    const match = path.match(/^\/_mesh\/device\/[^/]+(\/.*)?$/);
    return match ? (match[1] || '/') : path;
  };
  const encodeServer = value => btoa(unescape(encodeURIComponent(value))).replace(/=+$/g, '').replace(/\+/g, '-').replace(/\//g, '_');
  const decodeServer = value => decodeURIComponent(escape(atob(value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - value.length % 4) % 4))));
  const serverTabUrl = deviceId => `${location.origin}/_mesh/device/${encodeURIComponent(deviceId)}`;
  const currentDeviceId = () => {
    const match = location.pathname.match(/^\/server\/([^/]+)\//);
    if (!match) return virtualDeviceId(location.pathname);
    try {
      const server = new URL(decodeServer(match[1]));
      if (server.origin !== location.origin) return null;
      return virtualDeviceId(server.pathname);
    } catch (_) { return null; }
  };
  const readJson = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key) || 'null') ?? fallback; } catch (_) { return fallback; } };
  const headersObject = headers => {
    const result = {};
    new Headers(headers || {}).forEach((value, key) => { result[key] = value; });
    return result;
  };
  const readBody = async body => {
    if (body == null) return new Uint8Array();
    if (body instanceof Uint8Array) return body;
    if (body instanceof ArrayBuffer) return new Uint8Array(body);
    if (typeof body === 'string') return enc.encode(body);
    return new Uint8Array(await new Response(body).arrayBuffer());
  };

  async function syncNativeServers() {
    try {
      const response = await nativeFetch('/_mesh/devices', { credentials: 'same-origin' });
      if (!response.ok) return;
      const payload = await response.json();
      state.devices = Array.isArray(payload.devices) ? payload.devices : [];
      state.defaultDevice = payload.default_device || null;
      renderBar();
      const devices = state.devices.filter(device => device.online);
      const primaryId = payload.default_device || (devices[0] && devices[0].device_id);
      const signature = devices.map(device => `${device.device_id}:${device.name}`).sort().join('|') + '#' + primaryId;
      if (localStorage.getItem('ocm.native-servers.signature') === signature) return;
      const store = readJson('opencode.global.dat:server', { list: [], projects: {}, lastProject: {}, recentlyClosed: {} });
      store.list = devices.map(device => device.device_id === primaryId
        ? { type: 'http', displayName: device.name || device.device_id, http: { url: location.origin } }
        : { type: 'http', displayName: device.name || device.device_id, http: { url: serverTabUrl(device.device_id) } });
      localStorage.setItem('opencode.global.dat:server', JSON.stringify(store));
      localStorage.setItem('ocm.native-servers.signature', signature);
      location.reload();
    } catch (_) {}
  }

  function rejectEntry(id, error) {
    // Finish an in-flight request or stream and notify the Agent to cancel it.
    const err = error instanceof Error ? error : new Error(String(error));
    const entry = state.pending.get(id);
    if (entry) {
      state.pending.delete(id);
      if (entry.timer) clearTimeout(entry.timer);
      entry.reject(err);
    }
    const stream = state.streams.get(id);
    if (stream) {
      state.streams.delete(id);
      stream.controller.error(err);
    }
    send({ type: 'cancel', id }).catch(() => {});
  }

  function failTransport(error) {
    state.closed = true;
    if (state.pingTimer) { clearInterval(state.pingTimer); state.pingTimer = null; }
    state.rtt = null; state.pingSent = null;
    renderBar();
    for (const [id, entry] of state.pending) {
      if (entry.timer) clearTimeout(entry.timer);
      entry.reject(error);
      state.pending.delete(id);
    }
    for (const stream of state.streams.values()) stream.controller.error(error);
    state.streams.clear();
    state.incoming.clear();
    for (const socket of state.sockets.values()) {
      socket.readyState = MeshWebSocket.CLOSED;
      socket.dispatch('error', error);
      socket.dispatch('close', { code: 1011, reason: error.message });
    }
    state.sockets.clear();
  }

  function settleMessage(message) {
    if (message.type === 'pong') {
      if (state.pingSent != null && message.t === state.pingSent) {
        const rtt = Date.now() - state.pingSent;
        state.pingSent = null;
        if (rtt >= 0 && rtt <= RTT_MAX_MS) { state.rtt = rtt; renderBar(); }
      }
      return;
    }
    const socket = state.sockets.get(message.id);
    if (socket) {
      if (message.type === 'ws_opened') {
        // The state machine is monotonic: only CONNECTING can become OPEN.
        if (socket.readyState === MeshWebSocket.CONNECTING) {
          socket.readyState = MeshWebSocket.OPEN;
          // An unnegotiated protocol must clear the constructor's requested value.
          if ('protocol' in message) socket.protocol = message.protocol || '';
          socket.dispatch('open', {});
        }
      } else if (message.type === 'ws_data') {
        let data;
        if (message.kind === 'bytes') {
          const bytes = unb64(message.data);
          data = socket.binaryType === 'arraybuffer' ? bytes.buffer : new Blob([bytes]);
        } else {
          data = message.data || '';
        }
        socket.dispatch('message', { data });
      } else if (message.type === 'ws_closed' || message.type === 'ws_error') {
        socket.readyState = MeshWebSocket.CLOSED;
        if (message.type === 'ws_error') socket.dispatch('error', new Error(message.error || 'WebSocket failed'));
        socket.dispatch('close', { code: message.code || 1011, reason: message.error || '' });
        state.sockets.delete(message.id);
      }
      return;
    }
    const entry = state.pending.get(message.id);
    if (!entry) return;
    if (message.type === 'response_start') {
      entry.response = { status: message.status, headers: message.headers || {}, chunks: [] };
      return;
    }
    if (message.type === 'response_chunk') {
      if (entry.response) entry.response.chunks.push(message.body || '');
      return;
    }
    if (message.type === 'response_end') {
      state.pending.delete(message.id);
      if (entry.timer) clearTimeout(entry.timer);
      if (!entry.response) return entry.reject(new Error('incomplete response'));
      entry.resolve({ type: 'response', id: message.id, status: entry.response.status, headers: entry.response.headers, body: entry.response.chunks.join('') });
      return;
    }
    if (message.type === 'response' || message.type === 'cancelled') {
      state.pending.delete(message.id);
      if (entry.timer) clearTimeout(entry.timer);
      if (message.type === 'cancelled' && state.streams.has(message.id)) {
        // A pre-header cancellation must error and clean up, not become an empty 200 stream.
        const abortError = new DOMException('Request cancelled', 'AbortError');
        const stream = state.streams.get(message.id);
        state.streams.delete(message.id);
        stream.controller.error(abortError);
        entry.reject(abortError);
        return;
      }
      entry.resolve(message);
      return;
    }
    const stream = state.streams.get(message.id);
    if (stream) {
      if (message.type === 'stream_chunk' && message.status) {
        if (!entry.resolved) {
          entry.resolved = true;
          if (entry.timer) clearTimeout(entry.timer);
          entry.resolve(message);
        }
      }
      if (message.type === 'stream_chunk' && message.body !== undefined) stream.controller.enqueue(unb64(message.body));
      if (message.type === 'stream_end' || message.type === 'stream_error') {
        state.streams.delete(message.id);
        state.pending.delete(message.id);
        if (entry.timer) clearTimeout(entry.timer);
        if (message.type === 'stream_error') entry.reject(new Error(message.error || 'stream failed'));
        if (message.type === 'stream_error') stream.controller.error(new Error(message.error || 'stream failed'));
        else stream.controller.close();
      }
    }
  }

  function settle(message) {
    if (!message || typeof message !== 'object') return;
    if (!('message_id' in message) || !('sequence' in message) || !('final' in message)) {
      settleMessage(message);
      return;
    }
    const id = String(message.message_id || '');
    let entry = state.incoming.get(id);
    if (!entry) {
      entry = { next: 0, chunks: [], kind: null, meta: null };
      state.incoming.set(id, entry);
    }
    if (message.sequence !== entry.next) {
      state.incoming.delete(id);
      const pending = state.pending.get(id);
      if (pending) rejectEntry(id, new Error('invalid frame sequence'));
      return;
    }
    entry.next += 1;
    if (message.type) {
      entry.kind = message.type;
      entry.meta = { ...message };
      delete entry.meta.message_id;
      delete entry.meta.sequence;
      delete entry.meta.data;
      delete entry.meta.final;
    }
    entry.chunks.push(unb64(message.data || ''));
    if (!message.final) return;
    const bytes = new Uint8Array(entry.chunks.reduce((size, chunk) => size + chunk.length, 0));
    let offset = 0;
    for (const chunk of entry.chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    const meta = entry.meta ? { ...entry.meta } : null;
    if (meta) {
      meta.id = meta.id || id;
      meta.body = b64(bytes);
      settleMessage(meta);
      if (entry.kind === 'stream_chunk') {
        entry.chunks = [];
        entry.meta = null;
        return;
      }
      state.incoming.delete(id);
      return;
    }
    if (entry.kind === 'stream_chunk') {
      settleMessage({ type: 'stream_chunk', id, body: b64(bytes) });
      entry.chunks = [];
      return;
    }
    try {
      const decoded = JSON.parse(dec.decode(bytes));
      settleMessage(decoded);
    } catch (_) {
      state.incoming.delete(id);
      const pending = state.pending.get(id);
      if (pending) rejectEntry(id, new Error('invalid P2P message'));
      return;
    }
    state.incoming.delete(id);
  }

  function scheduleReconnect() {
    if (state.reconnectTimer) return;
    const generation = state.generation;
    failTransport(new Error('P2P disconnected; request outcome may be unknown'));
    state.pc = null; state.channel = null; state.closed = true;
    state.reconnectTimer = setTimeout(() => {
      state.reconnectTimer = null;
      if (generation !== state.generation) return;
      state.ready = connectP2P(state.routeDeviceId)
        .then(() => { state.reconnectDelay = 1000; })
        .catch(() => { state.reconnectDelay = Math.min(state.reconnectDelay * 2, 30000); scheduleReconnect(); });
    }, state.reconnectDelay);
  }

  async function connectP2P(deviceId) {
    state.closed = false;
    // Each device switch increments generation; old attempts invalidate themselves.
    const myGeneration = state.generation;
    const abandoned = () => state.generation !== myGeneration;
    // Bound setup stages without their own timeout so sockets cannot stay CONNECTING.
    const controller = new AbortController();
    const totalTimer = setTimeout(() => controller.abort(), CONNECT_TIMEOUT_MS);
    let pc = null;
    const dispose = () => { clearTimeout(totalTimer); if (pc) { try { pc.close(); } catch (_) {} } };
    try {
      const manifestUrl = deviceId ? `/_mesh/transport-manifest?device=${encodeURIComponent(deviceId)}` : '/_mesh/transport-manifest';
      const manifestResponse = await nativeFetch(manifestUrl, { credentials: 'same-origin', signal: controller.signal });
      if (abandoned()) return dispose();
      if (!manifestResponse.ok) throw new Error('transport manifest unavailable');
      const manifest = await manifestResponse.json();
      if (abandoned()) return dispose();
      state.manifest = manifest;
      state.deviceId = manifest.device_id || null;
      if (!manifest.p2p || !manifest.p2p.enabled || !window.RTCPeerConnection) throw new Error('p2p unavailable');
      pc = new RTCPeerConnection({ iceServers: (manifest.stun_servers || []).map(urls => ({ urls })) });
      const channel = pc.createDataChannel('opencode-mesh', { ordered: true });
      // Only current-generation events from the current channel affect global state.
      const alive = () => !abandoned() && state.channel === channel;
      channel.onclose = () => { if (alive()) scheduleReconnect(); };
      channel.onerror = () => { if (alive()) scheduleReconnect(); };
      channel.onmessage = event => { if (!alive()) return; try { settle(JSON.parse(typeof event.data === 'string' ? event.data : dec.decode(event.data))); } catch (_) {} };
      state.pc = pc; state.channel = channel;
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await timeout(new Promise(resolve => { if (pc.iceGatheringState === 'complete') resolve(); else pc.onicegatheringstatechange = () => { if (pc.iceGatheringState === 'complete') resolve(); }; }), 15000);
      const answerResponse = await nativeFetch(manifest.p2p.offer, { method: 'POST', credentials: 'same-origin', signal: controller.signal, headers: { 'content-type': 'application/json' }, body: JSON.stringify({ type: pc.localDescription.type, sdp: pc.localDescription.sdp, device_id: manifest.device_id }) });
      const answer = await answerResponse.json();
      if (abandoned()) return dispose();
      if (!answerResponse.ok || answer.error) throw new Error(answer.error || 'p2p answer failed');
      await pc.setRemoteDescription(answer);
      await timeout(new Promise((resolve, reject) => { if (channel.readyState === 'open') resolve(); else { channel.onopen = resolve; channel.onerror = reject; } }), 10000).catch(() => { throw new Error('p2p channel timeout'); });
      if (abandoned()) return dispose();
      // Restore the guarded handler after waiting for open to catch later errors.
      channel.onerror = () => { if (alive()) scheduleReconnect(); };
      startPing();
      renderBar();
      clearTimeout(totalTimer);
    } catch (error) {
      dispose();
      throw error;
    }
  }

  function startPing() {
    if (state.pingTimer) clearInterval(state.pingTimer);
    state.pingTimer = setInterval(() => {
      if (!state.channel || state.channel.readyState !== 'open') return;
      state.pingSent = Date.now();
      send({ type: 'ping', t: state.pingSent }).catch(() => {});
    }, 5000);
  }

  async function reconnectForDevice() {
    const deviceId = currentDeviceId();
    if (deviceId === state.routeDeviceId) return;
    state.generation += 1;
    if (state.reconnectTimer) { clearTimeout(state.reconnectTimer); state.reconnectTimer = null; }
    // Remove old handlers before close to prevent duplicate reconnect scheduling.
    const oldChannel = state.channel;
    if (oldChannel) { oldChannel.onclose = null; oldChannel.onerror = null; oldChannel.onmessage = null; }
    if (state.pc) { try { state.pc.close(); } catch (_) {} }
    state.pc = null; state.channel = null; state.manifest = null;
    failTransport(new Error('device switched'));
    state.closed = false;
    state.routeDeviceId = deviceId;
    state.ready = connectP2P(deviceId).then(() => { state.reconnectDelay = 1000; }).catch(() => { scheduleReconnect(); return null; });
  }

  for (const name of ['pushState', 'replaceState']) {
    const original = history[name].bind(history);
    history[name] = (...args) => { const result = original(...args); reconnectForDevice(); return result; };
  }
  window.addEventListener('popstate', reconnectForDevice);

  async function send(message) {
    // Snapshot the channel so one message cannot split across reconnect sessions.
    const channel = state.channel;
    if (!channel || channel.readyState !== 'open') throw new Error('p2p channel unavailable');
    const messageId = ['ws_data', 'ws_close'].includes(message.type) ? makeId() : String(message.id || makeId());
    const payload = enc.encode(JSON.stringify(message.id ? message : { ...message, id: messageId }));
    if (payload.length > MAX_P2P_BYTES) throw new Error('P2P message too large');
    for (let offset = 0, sequence = 0; offset < payload.length || sequence === 0; offset += CHUNK_SIZE, sequence += 1) {
      const chunk = payload.slice(offset, offset + CHUNK_SIZE);
      const envelope = { message_id: messageId, sequence, data: b64(chunk), final: offset + CHUNK_SIZE >= payload.length };
      const deadline = Date.now() + SEND_TIMEOUT_MS;
      while (channel.bufferedAmount > 1024 * 1024) {
        if (channel.readyState !== 'open') throw new Error('p2p channel unavailable');
        if (Date.now() >= deadline) {
          // Close a stalled channel to trigger reconnect and Relay fallback.
          try { channel.close(); } catch (_) {}
          throw new Error('P2P channel send timed out');
        }
        await new Promise(resolve => setTimeout(resolve, 10));
      }
      channel.send(JSON.stringify(envelope));
    }
  }

  async function orderedP2PSend(task) {
    const previous = state.p2pSendTail;
    let release;
    state.p2pSendTail = new Promise(resolve => { release = resolve; });
    await previous;
    try { return await task(); }
    finally { release(); }
  }

  async function p2pFetch(input, init = {}) {
    const request = new Request(typeof input === 'string' || input instanceof URL ? new URL(input, location.href) : input, init);
    let { path, query } = requestPath(request);
    const requestedDevice = virtualDeviceId(path);
    if (requestedDevice && requestedDevice !== state.manifest?.device_id) throw new Error('different device uses Relay');
    path = devicePath(path);
    const id = makeId();
    const sizeProbe = new Uint8Array(await request.clone().arrayBuffer());
    // Fall back to Relay for requests larger than the P2P payload limit.
    if (sizeProbe.length > MAX_P2P_BODY) return nativeFetch(input, init);
    const headers = headersObject(request.headers);
    const accept = (headers.accept || '').toLowerCase();
    if (accept.includes('text/event-stream') || path === '/event' || path === '/global/event' || path === '/api/event' || path.endsWith('/event')) {
      const first = new Promise((resolve, reject) => {
        const entry = { resolve, reject };
        entry.timer = setTimeout(() => rejectEntry(id, new Error('stream headers timeout')), 30000);
        state.pending.set(id, entry);
      });
      const stream = new ReadableStream({ start(controller) { state.streams.set(id, { controller }); }, cancel() { state.streams.delete(id); state.pending.delete(id); send({ type: 'cancel', id }).catch(() => {}); } });
      request.signal.addEventListener('abort', () => { rejectEntry(id, new DOMException('Aborted', 'AbortError')); }, { once: true });
      if (request.signal && request.signal.aborted) rejectEntry(id, new DOMException('Aborted', 'AbortError'));
      try {
        await orderedP2PSend(async () => {
          const body = new Uint8Array(await request.arrayBuffer());
          await send({ type: 'stream_request', id, method: request.method, path, query, headers, body: b64(body) });
        });
      }
      catch (error) { rejectEntry(id, error); throw error; }
      const meta = await first;
      return new Response(stream, { status: meta.status, headers: meta.headers });
    }
    const result = await new Promise((resolve, reject) => {
      const entry = { resolve, reject };
      entry.timer = setTimeout(() => rejectEntry(id, new Error('request timeout')), 120000);
      state.pending.set(id, entry);
      orderedP2PSend(async () => {
        const body = new Uint8Array(await request.arrayBuffer());
        await send({ type: 'request', id, method: request.method, path, query, headers, body: b64(body) });
      }).catch(error => rejectEntry(id, error));
      if (request.signal) request.signal.addEventListener('abort', () => { rejectEntry(id, new DOMException('Aborted', 'AbortError')); }, { once: true });
    });
    if (result.type === 'cancelled') throw new DOMException('Request cancelled', 'AbortError');
    const bytes = unb64(result.body);
    return new Response([204, 205, 304].includes(result.status) || request.method === 'HEAD' ? null : bytes, { status: result.status || 502, headers: result.headers || {} });
  }

  class MeshWebSocket {
    static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
    constructor(input, protocols) {
      const url = new URL(input, location.href);
      this.url = url.href.replace(/^http/, 'ws');
      this.protocol = '';
      this.readyState = MeshWebSocket.CONNECTING;
      this.binaryType = 'blob';
      this._listeners = new Map();
      // Serialize frames so text cannot overtake a queued binary frame.
      this._sendQueue = Promise.resolve();
       this.id = makeId();
      state.sockets.set(this.id, this);
      Promise.resolve(state.ready).then(() => {
        if (this.readyState !== MeshWebSocket.CONNECTING) return;
        if (!state.channel || state.channel.readyState !== 'open') return this.fail(new Error('P2P unavailable'));
        send({ type: 'ws_open', id: this.id, path: devicePath(url.pathname), query: url.search.slice(1), headers: {}, protocols: Array.isArray(protocols) ? protocols : (protocols ? [protocols] : []) }).catch(error => this.fail(error));
      });
    }
    addEventListener(type, fn) { if (!this._listeners.has(type)) this._listeners.set(type, new Set()); this._listeners.get(type).add(fn); }
    removeEventListener(type, fn) { this._listeners.get(type)?.delete(fn); }
    dispatch(type, event) { this['on' + type]?.(event); for (const fn of this._listeners.get(type) || []) fn.call(this, event); }
    // Expose shared DataChannel backpressure to callers.
    get bufferedAmount() { return state.channel && state.channel.readyState === 'open' ? state.channel.bufferedAmount : 0; }
    fail(error) { state.sockets.delete(this.id); this.readyState = MeshWebSocket.CLOSED; this.dispatch('error', error); this.dispatch('close', { code: 1011, reason: error.message }); }
    send(data) {
      if (this.readyState !== MeshWebSocket.OPEN) throw new Error('WebSocket is not open');
       const toBytes = async value => {
         if (value instanceof Blob) return new Uint8Array(await value.arrayBuffer());
         if (value instanceof ArrayBuffer) return new Uint8Array(value);
         if (ArrayBuffer.isView(value)) return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
         throw new TypeError('unsupported WebSocket data type');
       };
       // Serialize all frames so text cannot overtake a queued binary frame.
       this._sendQueue = this._sendQueue.then(async () => {
         if (typeof data === 'string') {
           await send({ type: 'ws_data', id: this.id, kind: 'text', data });
           return;
         }
         const bytes = await toBytes(data);
         await send({ type: 'ws_data', id: this.id, kind: 'bytes', data: b64(bytes) });
       }).catch(error => { this.fail(error); });
    }
    close(code = 1000, reason = '') {
      if (this.readyState === MeshWebSocket.CLOSED) return;
      this.readyState = MeshWebSocket.CLOSING;
      send({ type: 'ws_close', id: this.id, code, reason }).catch(() => {});
    }
  }

  state.routeDeviceId = currentDeviceId();
  state.ready = connectP2P(state.routeDeviceId).catch(() => { scheduleReconnect(); return null; });
  window.__ocmTransport = state;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', renderBar, { once: true });
  else renderBar();
  let relayTick = 0;
  setInterval(() => { renderBar(); if (++relayTick % 5 === 0) measureRelayRtt(); }, 2000);
  setTimeout(measureRelayRtt, 1500);
  syncNativeServers();

  window.fetch = async (input, init) => {
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url, location.href);
    if (url.origin !== location.origin || (url.pathname.startsWith('/_mesh/') && !url.pathname.startsWith('/_mesh/device/')) || (virtualDeviceId(url.pathname) && virtualDeviceId(url.pathname) !== state.manifest?.device_id)) return nativeFetch(input, init);
    if (state.channel && state.channel.readyState === 'open') return p2pFetch(input, init);
    // Only wait for the initial P2P attempt; never add latency while running on Relay.
    if (state.pc && !state.closed && !state.reconnectTimer) {
      await Promise.race([state.ready, new Promise(resolve => setTimeout(resolve, 1200))]);
      if (state.channel && state.channel.readyState === 'open') return p2pFetch(input, init);
    }
    return nativeFetch(input, init);
  };
  window.WebSocket = class extends MeshWebSocket {
    constructor(input, protocols) {
      const url = new URL(input, location.href);
      if (url.host !== location.host || (virtualDeviceId(url.pathname) && virtualDeviceId(url.pathname) !== state.manifest?.device_id) || !state.channel || state.channel.readyState !== 'open') {
        return new nativeWebSocket(input, protocols);
      }
      super(input, protocols);
    }
  };
  const makeEventTarget = () => {
    const target = { _listeners: new Map() };
    target.addEventListener = (type, fn) => { if (!target._listeners.has(type)) target._listeners.set(type, new Set()); target._listeners.get(type).add(fn); };
    target.removeEventListener = (type, fn) => { target._listeners.get(type)?.delete(fn); };
    target.dispatch = (type, event = {}) => { target['on' + type]?.(event); for (const fn of target._listeners.get(type) || []) fn.call(target, event); };
    return target;
  };
  class MeshXMLHttpRequest {
    static UNSENT = 0; static OPENED = 1; static HEADERS_RECEIVED = 2; static LOADING = 3; static DONE = 4;
    constructor() {
      this.readyState = MeshXMLHttpRequest.UNSENT;
      this.status = 0; this.statusText = ''; this.response = null; this.responseText = '';
      this.responseType = ''; this.responseURL = ''; this.timeout = 0;
      this.upload = makeEventTarget();
      this._listeners = new Map(); this._headers = {}; this._controller = null; this._aborted = false; this._timedOut = false;
    }
    addEventListener(type, fn) { if (!this._listeners.has(type)) this._listeners.set(type, new Set()); this._listeners.get(type).add(fn); }
    removeEventListener(type, fn) { this._listeners.get(type)?.delete(fn); }
    dispatch(type, event = {}) { this['on' + type]?.(event); for (const fn of this._listeners.get(type) || []) fn.call(this, event); }
    open(method, url, async = true) {
      if (async === false) throw new Error('synchronous XMLHttpRequest is not supported by Mesh');
      this.method = method; this.url = new URL(url, location.href).href; this.readyState = MeshXMLHttpRequest.OPENED; this.dispatch('readystatechange');
    }
    setRequestHeader(name, value) { this._headers[name] = value; }
    getAllResponseHeaders() { return this._responseHeaders || ''; }
    getResponseHeader(name) { return this._responseHeadersMap?.get(name.toLowerCase()) || null; }
    abort() { this._aborted = true; this._controller?.abort(); if (this.readyState !== MeshXMLHttpRequest.DONE) { this.readyState = MeshXMLHttpRequest.DONE; this.dispatch('abort'); this.dispatch('loadend'); } }
    async send(body = null) {
      if (this.readyState !== MeshXMLHttpRequest.OPENED) throw new Error('InvalidStateError');
      this._controller = new AbortController();
      this._timedOut = false;
      let timeoutTimer = null;
      if (this.timeout > 0) timeoutTimer = setTimeout(() => { this._timedOut = true; this._controller.abort(); }, this.timeout);
      try {
        const response = await window.fetch(this.url, { method: this.method, headers: this._headers, body, credentials: 'same-origin', signal: this._controller.signal });
        if (this._aborted) return;
        this.status = response.status; this.statusText = response.statusText; this.responseURL = response.url;
        this._responseHeadersMap = new Headers(response.headers);
        this._responseHeaders = [...this._responseHeadersMap].map(([k, v]) => `${k}: ${v}\r\n`).join('');
        this.readyState = MeshXMLHttpRequest.HEADERS_RECEIVED; this.dispatch('readystatechange');
        const total = Number(response.headers.get('content-length')) || 0;
        const chunks = []; let loaded = 0;
        if (response.body) {
          this.readyState = MeshXMLHttpRequest.LOADING; this.dispatch('readystatechange');
          const reader = response.body.getReader();
          while (true) {
            const item = await reader.read(); if (item.done) break;
            chunks.push(item.value); loaded += item.value.length;
            this.dispatch('progress', { type: 'progress', lengthComputable: total > 0, loaded, total, target: this });
          }
        }
        if (this._aborted) return;
        const bytes = new Uint8Array(loaded);
        let offset = 0; for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
        if (this.responseType === 'arraybuffer') this.response = bytes.buffer;
        else if (this.responseType === 'blob') this.response = new Blob([bytes], { type: response.headers.get('content-type') || '' });
        else if (this.responseType === 'json') this.response = JSON.parse(dec.decode(bytes));
        else { this.responseText = dec.decode(bytes); this.response = this.responseText; }
        this.readyState = MeshXMLHttpRequest.DONE; this.dispatch('readystatechange'); this.dispatch('load', { type: 'load', target: this }); this.dispatch('loadend', { type: 'loadend', target: this });
      } catch (error) {
        if (this._aborted) return;
        this.readyState = MeshXMLHttpRequest.DONE; this.dispatch('readystatechange');
        if (this._timedOut) this.dispatch('timeout', { type: 'timeout', target: this });
        else this.dispatch('error', error);
        this.dispatch('loadend', { type: 'loadend', target: this });
      } finally {
        if (timeoutTimer) clearTimeout(timeoutTimer);
      }
    }
  }
  window.XMLHttpRequest = MeshXMLHttpRequest;
  class MeshEventSource {
    static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
    constructor(input, options) {
      this.url = new URL(input, location.href).href;
      this.withCredentials = !!(options && options.withCredentials);
      this.readyState = MeshEventSource.CONNECTING;
      this._listeners = new Map();
      this._controller = new AbortController();
      this._retryDelay = 1000;
      this.lastEventId = '';
      this._retryTimer = null;
      this._idleTimedOut = false;
      this.start();
    }
    addEventListener(type, fn) { if (!this._listeners.has(type)) this._listeners.set(type, new Set()); this._listeners.get(type).add(fn); }
    removeEventListener(type, fn) { this._listeners.get(type)?.delete(fn); }
    dispatch(type, event) { this['on' + type]?.(event); for (const fn of this._listeners.get(type) || []) fn.call(this, event); }
    async start() {
      while (this.readyState !== MeshEventSource.CLOSED) {
        try {
          this._controller = new AbortController();
          const headers = { accept: 'text/event-stream' };
          if (this.lastEventId) headers['Last-Event-ID'] = this.lastEventId;
          const response = await window.fetch(this.url, { headers, signal: this._controller.signal });
          if (!response.ok) {
            const error = new Error('SSE status ' + response.status);
            if (response.status === 401 || response.status === 403) throw Object.assign(error, { permanent: true });
            throw error;
          }
          this.readyState = MeshEventSource.OPEN; this._retryDelay = 1000; this.dispatch('open', { type: 'open' });
          const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
          // Abort a half-dead stream whose read remains pending without any bytes.
          let idleTimer = setTimeout(() => { this._idleTimedOut = true; this._controller.abort(); }, SSE_IDLE_MS);
          const armIdle = () => { clearTimeout(idleTimer); idleTimer = setTimeout(() => { this._idleTimedOut = true; this._controller.abort(); }, SSE_IDLE_MS); };
          try {
            while (true) {
              const item = await reader.read(); if (item.done) break;
              armIdle();
              buffer += decoder.decode(item.value, { stream: true });
              const records = buffer.split(/\r?\n\r?\n/); buffer = records.pop() || '';
              for (const record of records) {
                const lines = record.split(/\r?\n/); let event = 'message', id = '', data = [];
                for (const line of lines) { if (line.startsWith('event:')) event = line.slice(6).trim(); else if (line.startsWith('id:')) id = line.slice(3).trim(); else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, '')); }
                if (id) this.lastEventId = id;
                if (data.length) this.dispatch(event, { type: event, data: data.join('\n'), lastEventId: this.lastEventId, origin: location.origin });
              }
            }
          } finally { clearTimeout(idleTimer); }
          throw new Error('SSE closed');
        } catch (error) {
          const idleTimedOut = this._idleTimedOut; this._idleTimedOut = false;
          if (this.readyState === MeshEventSource.CLOSED) return;
          if (error.name === 'AbortError' && !idleTimedOut) return;
          this.readyState = MeshEventSource.CONNECTING;
          this.dispatch('error', error);
          if (error.permanent) { this.readyState = MeshEventSource.CLOSED; return; }
          await new Promise(resolve => { this._retryTimer = setTimeout(resolve, this._retryDelay); });
          this._retryTimer = null;
          this._retryDelay = Math.min(this._retryDelay * 2, 30000);
        }
      }
    }
    close() { this.readyState = MeshEventSource.CLOSED; if (this._retryTimer) clearTimeout(this._retryTimer); this._controller.abort(); }
  }
  window.EventSource = class extends MeshEventSource {
    constructor(input, options) {
      const url = new URL(input, location.href);
      if (url.host !== location.host) return new nativeEventSource(input, options);
      super(input, options);
    }
  };
  function resetRttAfterBackground() {
    state.rtt = null; state.relayRtt = null; state.pingSent = null;
    renderBar();
    if (state.channel && state.channel.readyState === 'open') {
      state.pingSent = Date.now();
      send({ type: 'ping', t: state.pingSent }).catch(() => { state.pingSent = null; });
    }
    measureRelayRtt();
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) resetRttAfterBackground(); });
  window.addEventListener('pageshow', event => { if (event.persisted) resetRttAfterBackground(); });

  window.addEventListener('beforeunload', () => { if (state.pc) state.pc.close(); });
})();
</script>
"""
