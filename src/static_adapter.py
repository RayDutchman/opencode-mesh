from __future__ import annotations


TRANSPORT_ADAPTER = r"""
<script id="ocm-transport-adapter">
(() => {
  const nativeFetch = window.fetch.bind(window);
  const nativeWebSocket = window.WebSocket;
  const nativeEventSource = window.EventSource;
  const enc = new TextEncoder();
  const dec = new TextDecoder();
  const b64 = bytes => { let s = ''; for (const b of bytes) s += String.fromCharCode(b); return btoa(s); };
  const unb64 = text => Uint8Array.from(atob(text || ''), c => c.charCodeAt(0));
  const timeout = (promise, ms) => Promise.race([promise, new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), ms))]);

  const state = { manifest: null, pc: null, channel: null, ready: null, pending: new Map(), streams: new Map(), sockets: new Map(), closed: false, deviceId: null, routeDeviceId: undefined, generation: 0, reconnectTimer: null, reconnectDelay: 1000 };

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
    if (!match) return null;
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
      const devices = (Array.isArray(payload.devices) ? payload.devices : []).filter(device => device.online);
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
    const entry = state.pending.get(id);
    if (!entry) return;
    state.pending.delete(id);
    if (entry.timer) clearTimeout(entry.timer);
    entry.reject(error instanceof Error ? error : new Error(String(error)));
  }

  function failTransport(error) {
    state.closed = true;
    for (const [id, entry] of state.pending) {
      if (entry.timer) clearTimeout(entry.timer);
      entry.reject(error);
      state.pending.delete(id);
    }
    for (const stream of state.streams.values()) stream.controller.error(error);
    state.streams.clear();
    for (const socket of state.sockets.values()) {
      socket.readyState = MeshWebSocket.CLOSED;
      socket.dispatch('error', error);
      socket.dispatch('close', { code: 1011, reason: error.message });
    }
    state.sockets.clear();
  }

  function settle(message) {
    const socket = state.sockets.get(message.id);
    if (socket) {
      if (message.type === 'ws_opened') {
        socket.readyState = MeshWebSocket.OPEN;
        socket.dispatch('open', {});
      } else if (message.type === 'ws_data') {
        socket.dispatch('message', { data: message.kind === 'bytes' ? unb64(message.data) : (message.data || '') });
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
      entry.resolve(message);
      return;
    }
    const stream = state.streams.get(message.id);
    if (stream) {
      if (message.type === 'stream_chunk' && message.status) {
        state.pending.delete(message.id);
        if (entry.timer) clearTimeout(entry.timer);
        entry.resolve(message);
      }
      if (message.type === 'stream_chunk' && message.body) stream.controller.enqueue(unb64(message.body));
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

  function scheduleReconnect() {
    if (state.reconnectTimer) return;
    const generation = state.generation;
    failTransport(new Error('P2P disconnected; request outcome may be unknown'));
    state.pc = null; state.channel = null; state.closed = true;
    state.reconnectTimer = setTimeout(() => {
      state.reconnectTimer = null;
      if (generation !== state.generation) return;
      state.ready = connectP2P(state.deviceId)
        .then(() => { state.reconnectDelay = 1000; })
        .catch(() => { state.reconnectDelay = Math.min(state.reconnectDelay * 2, 30000); scheduleReconnect(); });
    }, state.reconnectDelay);
  }

  async function connectP2P(deviceId) {
    state.closed = false;
    const manifestUrl = deviceId ? `/_mesh/transport-manifest?device=${encodeURIComponent(deviceId)}` : '/_mesh/transport-manifest';
    const manifestResponse = await nativeFetch(manifestUrl, { credentials: 'same-origin' });
    if (!manifestResponse.ok) throw new Error('transport manifest unavailable');
    state.manifest = await manifestResponse.json();
    state.deviceId = state.manifest.device_id || null;
    if (!state.manifest.p2p || !state.manifest.p2p.enabled || !window.RTCPeerConnection) throw new Error('p2p unavailable');
    const pc = new RTCPeerConnection({ iceServers: (state.manifest.stun_servers || []).map(urls => ({ urls })) });
    const channel = pc.createDataChannel('opencode-mesh', { ordered: true });
    channel.onclose = () => scheduleReconnect();
    channel.onerror = () => scheduleReconnect();
    channel.onmessage = event => { try { settle(JSON.parse(typeof event.data === 'string' ? event.data : dec.decode(event.data))); } catch (_) {} };
    state.pc = pc; state.channel = channel;
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await timeout(new Promise(resolve => { if (pc.iceGatheringState === 'complete') resolve(); else pc.onicegatheringstatechange = () => { if (pc.iceGatheringState === 'complete') resolve(); }; }), 15000);
    const answerResponse = await nativeFetch(state.manifest.p2p.offer, { method: 'POST', credentials: 'same-origin', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ type: pc.localDescription.type, sdp: pc.localDescription.sdp, device_id: state.manifest.device_id }) });
    const answer = await answerResponse.json();
    if (!answerResponse.ok || answer.error) throw new Error(answer.error || 'p2p answer failed');
    await pc.setRemoteDescription(answer);
    await timeout(new Promise((resolve, reject) => { if (channel.readyState === 'open') resolve(); else { channel.onopen = resolve; channel.onerror = reject; } }), 10000).catch(() => { throw new Error('p2p channel timeout'); });
  }

  async function reconnectForDevice() {
    const deviceId = currentDeviceId();
    if (deviceId === state.routeDeviceId) return;
    state.generation += 1;
    if (state.reconnectTimer) { clearTimeout(state.reconnectTimer); state.reconnectTimer = null; }
    if (state.pc) { try { state.pc.close(); } catch (_) {} }
    state.pc = null; state.channel = null; state.manifest = null;
    failTransport(new Error('device switched'));
    state.closed = false;
    state.routeDeviceId = deviceId;
    state.ready = connectP2P(deviceId).catch(() => { scheduleReconnect(); return null; });
  }

  for (const name of ['pushState', 'replaceState']) {
    const original = history[name].bind(history);
    history[name] = (...args) => { const result = original(...args); reconnectForDevice(); return result; };
  }
  window.addEventListener('popstate', reconnectForDevice);

  async function send(message) {
    if (!state.channel || state.channel.readyState !== 'open') throw new Error('p2p channel unavailable');
    const payload = JSON.stringify(message);
    while (state.channel.bufferedAmount > 1024 * 1024) {
      await new Promise(resolve => setTimeout(resolve, 10));
      if (!state.channel || state.channel.readyState !== 'open') throw new Error('p2p channel unavailable');
    }
    state.channel.send(payload);
  }

  async function p2pFetch(input, init = {}) {
    const request = new Request(typeof input === 'string' || input instanceof URL ? new URL(input, location.href) : input, init);
    let { path, query } = requestPath(request);
    const requestedDevice = virtualDeviceId(path);
    if (requestedDevice && requestedDevice !== state.manifest?.device_id) throw new Error('different device uses Relay');
    path = devicePath(path);
    const id = crypto.randomUUID();
    const body = new Uint8Array(await request.arrayBuffer());
    const headers = headersObject(request.headers);
    const accept = (headers.accept || '').toLowerCase();
    if (accept.includes('text/event-stream') || path === '/event' || path === '/global/event' || path === '/api/event' || path.endsWith('/event')) {
      const first = new Promise((resolve, reject) => {
        const entry = { resolve, reject };
        entry.timer = setTimeout(() => rejectEntry(id, new Error('stream headers timeout')), 30000);
        state.pending.set(id, entry);
      });
      const stream = new ReadableStream({ start(controller) { state.streams.set(id, { controller }); }, cancel() { state.streams.delete(id); state.pending.delete(id); send({ type: 'cancel', id }); } });
      try { await send({ type: 'stream_request', id, method: request.method, path, query, headers, body: b64(body) }); }
      catch (error) { state.pending.delete(id); state.streams.delete(id); throw error; }
      request.signal.addEventListener('abort', () => { const s = state.streams.get(id); if (s) s.controller.error(new DOMException('Aborted', 'AbortError')); rejectEntry(id, new DOMException('Aborted', 'AbortError')); state.streams.delete(id); send({ type: 'cancel', id }).catch(() => {}); }, { once: true });
      const meta = await first;
      return new Response(stream, { status: meta.status, headers: meta.headers });
    }
    const result = await new Promise((resolve, reject) => {
      const entry = { resolve, reject };
      entry.timer = setTimeout(() => rejectEntry(id, new Error('request timeout')), 120000);
      state.pending.set(id, entry);
      send({ type: 'request', id, method: request.method, path, query, headers, body: b64(body) }).catch(error => rejectEntry(id, error));
      if (request.signal) request.signal.addEventListener('abort', () => { rejectEntry(id, new DOMException('Aborted', 'AbortError')); send({ type: 'cancel', id }).catch(() => {}); }, { once: true });
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
      this.protocol = Array.isArray(protocols) ? protocols[0] || '' : (protocols || '');
      this.readyState = MeshWebSocket.CONNECTING;
      this.bufferedAmount = 0;
      this._listeners = new Map();
      this.id = crypto.randomUUID();
      state.sockets.set(this.id, this);
      Promise.resolve(state.ready).then(() => {
        if (this.readyState !== MeshWebSocket.CONNECTING) return;
        if (!state.channel || state.channel.readyState !== 'open') return this.fail(new Error('P2P unavailable'));
        send({ type: 'ws_open', id: this.id, path: devicePath(url.pathname), query: url.search.slice(1), headers: {} }).catch(error => this.fail(error));
      });
    }
    addEventListener(type, fn) { if (!this._listeners.has(type)) this._listeners.set(type, new Set()); this._listeners.get(type).add(fn); }
    removeEventListener(type, fn) { this._listeners.get(type)?.delete(fn); }
    dispatch(type, event) { this['on' + type]?.(event); for (const fn of this._listeners.get(type) || []) fn.call(this, event); }
    fail(error) { state.sockets.delete(this.id); this.readyState = MeshWebSocket.CLOSED; this.dispatch('error', error); this.dispatch('close', { code: 1011, reason: error.message }); }
    send(data) {
      if (this.readyState !== MeshWebSocket.OPEN) throw new Error('WebSocket is not open');
      const bytes = typeof data === 'string' ? null : new Uint8Array(data instanceof ArrayBuffer ? data : data.buffer);
      send({ type: 'ws_data', id: this.id, kind: bytes ? 'bytes' : 'text', data: bytes ? b64(bytes) : data }).catch(error => this.fail(error));
    }
    close(code = 1000, reason = '') {
      if (this.readyState === MeshWebSocket.CLOSED) return;
      this.readyState = MeshWebSocket.CLOSING;
      send({ type: 'ws_close', id: this.id, code, reason }).catch(() => {});
    }
  }

  state.ready = connectP2P(currentDeviceId()).catch(() => null);
  window.__ocmTransport = state;
  syncNativeServers();

  window.fetch = async (input, init) => {
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url, location.href);
    if (url.origin !== location.origin || (url.pathname.startsWith('/_mesh/') && !url.pathname.startsWith('/_mesh/device/')) || (virtualDeviceId(url.pathname) && virtualDeviceId(url.pathname) !== state.manifest?.device_id)) return nativeFetch(input, init);
    await Promise.race([state.ready, new Promise(resolve => setTimeout(resolve, 1200))]);
    if (state.channel && state.channel.readyState === 'open') return p2pFetch(input, init);
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
  class MeshEventSource {
    static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
    constructor(input, options) {
      this.url = new URL(input, location.href).href;
      this.withCredentials = !!(options && options.withCredentials);
      this.readyState = MeshEventSource.CONNECTING;
      this._listeners = new Map();
      this._controller = new AbortController();
      this.start();
    }
    addEventListener(type, fn) { if (!this._listeners.has(type)) this._listeners.set(type, new Set()); this._listeners.get(type).add(fn); }
    removeEventListener(type, fn) { this._listeners.get(type)?.delete(fn); }
    dispatch(type, event) { this['on' + type]?.(event); for (const fn of this._listeners.get(type) || []) fn.call(this, event); }
    async start() {
      try {
        const response = await window.fetch(this.url, { headers: { accept: 'text/event-stream' }, signal: this._controller.signal });
        if (!response.ok) throw new Error('SSE status ' + response.status);
        this.readyState = MeshEventSource.OPEN; this.dispatch('open', { type: 'open' });
        const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
        while (true) {
          const item = await reader.read(); if (item.done) break;
          buffer += decoder.decode(item.value, { stream: true });
          const records = buffer.split(/\r?\n\r?\n/); buffer = records.pop() || '';
          for (const record of records) {
            const lines = record.split(/\r?\n/); let event = 'message', id = '', data = [];
            for (const line of lines) { if (line.startsWith('event:')) event = line.slice(6).trim(); else if (line.startsWith('id:')) id = line.slice(3).trim(); else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, '')); }
            if (data.length) this.dispatch(event, { type: event, data: data.join('\n'), lastEventId: id, origin: location.origin });
          }
        }
        if (this.readyState !== MeshEventSource.CLOSED) { this.readyState = MeshEventSource.CLOSED; this.dispatch('error', new Error('SSE closed')); }
      } catch (error) {
        if (this.readyState !== MeshEventSource.CLOSED) { this.readyState = MeshEventSource.CLOSED; this.dispatch('error', error); }
      }
    }
    close() { this.readyState = MeshEventSource.CLOSED; this._controller.abort(); }
  }
  window.EventSource = class extends MeshEventSource {
    constructor(input, options) {
      const url = new URL(input, location.href);
      if (url.host !== location.host || !state.channel || state.channel.readyState !== 'open') return new nativeEventSource(input, options);
      super(input, options);
    }
  };
  window.addEventListener('beforeunload', () => { if (state.pc) state.pc.close(); });
})();
</script>
"""
