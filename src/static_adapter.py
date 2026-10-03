from __future__ import annotations


TRANSPORT_ADAPTER = r"""
<script id="ocm-transport-adapter">
(() => {
  const MESH_VERSION = __OCM_VERSION_JSON__;
  const nativeFetch = window.fetch.bind(window);
  const nativeWebSocket = window.WebSocket;
  const NativeURL = window.URL;
  // The V2 SDK builds URLs from absolute /api paths; keep the explicit device scope while the base URL is not yet lost.
  window.URL = class extends NativeURL {
    constructor(input, base) {
      super(input, base);
      if (base === undefined || typeof input !== 'string' || !input.startsWith('/api/')) return;
      const target = new NativeURL(base);
      if (target.origin !== location.origin || this.origin !== target.origin) return;
      const match = target.pathname.match(/^\/_mesh\/device\/[^/]+\/?$/);
      if (match) this.pathname = target.pathname.replace(/\/$/, '') + this.pathname;
    }
  };
  const enc = new TextEncoder();
  const dec = new TextDecoder();
  const b64 = bytes => { let s = ''; for (const b of bytes) s += String.fromCharCode(b); return btoa(s); };
  const unb64 = text => Uint8Array.from(atob(text || ''), c => c.charCodeAt(0));
  // Wait for a local (non-fetch) stage while the 40s deadline can still interrupt it.
  const waitWithDeadline = (promise, ms, label, controller) => new Promise((resolve, reject) => {
    let timer;
    const onAbort = () => { clearTimeout(timer); controller.signal.removeEventListener('abort', onAbort); reject(new Error(label + ' aborted')); };
    if (controller.signal.aborted) { onAbort(); return; }
    controller.signal.addEventListener('abort', onAbort, { once: true });
    timer = setTimeout(() => { controller.signal.removeEventListener('abort', onAbort); reject(new Error(label + ' timeout')); }, ms);
    promise.then(
      value => { clearTimeout(timer); controller.signal.removeEventListener('abort', onAbort); resolve(value); },
      error => { clearTimeout(timer); controller.signal.removeEventListener('abort', onAbort); reject(error); }
    );
  });
  const makeId = () => {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') return globalThis.crypto.randomUUID();
    return 'ocm-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
  };
  const CHUNK_SIZE = 32768;
  const MAX_P2P_BYTES = 64 * 1024 * 1024;
  // Keep decoded assembly payloads within the P2P per-message budget. This does
  // not account for the DataChannel JSON/base64 string or the final contiguous copy.
  // The entry cap also bounds unknown IDs before an Agent response is associated with work.
  const MAX_INCOMING_MESSAGES = 128;
  const INCOMING_ASSEMBLY_TTL_MS = 120000;
  // P2P request bodies expand through base64 and JSON; larger bodies use Relay.
  const MAX_P2P_BODY = 32 * 1024 * 1024;
  // Bound backpressure waits to match the Agent's default send timeout.
  const SEND_TIMEOUT_MS = 10000;
  // Bound the whole P2P setup, including manifest and answer fetches.
  const CONNECT_TIMEOUT_MS = 40000;
  // Hints are debounced so an online/connection-change storm collapses into one attempt.
  const NETWORK_DEBOUNCE_MS = 300;
  // After a hint-driven attempt a new hint is ignored until the cooldown lapses.
  const NETWORK_COOLDOWN_MS = 5000;
  // A foreground resume only retries when the last attempt is older than this.
  const FOREGROUND_RETRY_MS = 15000;
  // Sanity cap: a real round-trip is far below this; anything larger is a clock-jump artifact (lock screen / background timer freeze) and must be discarded.
  const RTT_MAX_MS = 10000;
  // A foreground resume verifies the old open P2P channel with a probe ping and
  // only keeps the channel when a pong matches inside this window.
  const PROBE_TIMEOUT_MS = 3000;
  // A root handoff is an explicit device boundary, not a default preference.
  // Do not let bootstrap's pre-existing selection start traffic before discovery
  // and the V2 probe have accepted that requested device.
  const rootHandoffDevice = location.pathname === '/' ? new URLSearchParams(location.search).get('mesh_device') : null;
  // Only a recent `/_mesh/devices` snapshot may refuse requests locally. The
  // poll runs every 5s, so this tolerates three missed polls (background timer
  // throttling, a stalled tab) before an offline verdict stops being trusted.
  const DEVICE_SNAPSHOT_TTL_MS = 15000;
  const OFFLINE_BODY_ERROR = 'Specified device offline or not found';

  const state = { manifest: null, pc: null, channel: null, ready: null, pending: new Map(), streams: new Map(), sockets: new Map(), incoming: new Map(), incomingBytes: 0, incomingTombstones: new Map(), closed: false, deviceId: null, routeDeviceId: undefined, generation: 0, reconnectTimer: null, reconnectDelay: 1000, networkTimer: null, lastNetworkAttempt: null, lastAttemptTime: null, activeController: null, isInitialAttempt: false, devices: [], defaultDevice: null, deviceSnapshotAt: null, rtt: null, pingSent: null, pingTimer: null, relayRtt: null, p2pSendTail: Promise.resolve(), probing: false, probe: null, transportStarted: false, handoffPending: !!rootHandoffDevice };

  const BAR_CSS = `
  #ocm-mesh-bar{display:flex;align-items:center;gap:8px;height:36px;padding:0 10px;font-size:13px;line-height:20px;flex:0 0 auto;border-bottom:1px solid var(--v2-border-border-base);background:var(--v2-background-bg-layer-01);color:var(--v2-text-text-muted);-webkit-user-select:none;user-select:none}
  #ocm-mesh-bar .ocm-title{font-weight:600;color:var(--v2-text-text-base)}
  #ocm-mesh-bar .ocm-version{font-size:11px;color:var(--v2-text-text-faint);white-space:nowrap}
  #ocm-mesh-bar .ocm-device-menu-wrap{position:relative;min-width:0}
  #ocm-mesh-bar .ocm-device-menu-button{display:flex;align-items:center;gap:6px;min-width:0;border:1px solid var(--v2-border-border-base);border-radius:6px;padding:2px 8px;background:var(--v2-background-bg-layer-02);color:var(--v2-text-text-base);font:inherit;line-height:20px;cursor:pointer}
  #ocm-mesh-bar .ocm-device-menu-button[data-offline="true"]{color:var(--v2-text-text-faint)}
  #ocm-mesh-bar .ocm-device-menu-button:focus-visible,#ocm-device-menu .ocm-device-menu-item:focus-visible{outline:2px solid var(--v2-border-border-base);outline-offset:2px}
  #ocm-mesh-bar .ocm-device-menu-label{max-width:40vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #ocm-device-menu{position:fixed;z-index:2147483646;min-width:0;max-width:calc(100vw - 16px);max-height:calc(100dvh - 16px);overflow:auto;padding:4px;background:var(--v2-background-bg-layer-01);color:var(--v2-text-text-base);font-family:inherit;font-size:13px;font-weight:440;line-height:20px;border-radius:6px;box-shadow:var(--v2-elevation-floating,0 8px 16px rgba(0,0,0,.04),0 4px 8px rgba(0,0,0,.08),0 0 0 .5px rgba(0,0,0,.12))}
  #ocm-device-menu .ocm-device-menu-item{display:flex;align-items:center;gap:8px;width:100%;min-width:0;border:0;border-radius:6px;padding:6px 8px;background:transparent;color:inherit;font-family:inherit;font-size:13px;font-weight:440;line-height:20px;text-align:left;cursor:pointer;transition:background 120ms}
  #ocm-device-menu .ocm-device-menu-item:hover:not(:disabled),#ocm-device-menu .ocm-device-menu-item:focus-visible{background:var(--v2-overlay-simple-overlay-hover)}
  #ocm-device-menu .ocm-device-menu-item[aria-current="true"]{background:var(--v2-background-bg-layer-03)}
  #ocm-device-menu .ocm-device-menu-item:disabled{cursor:default;opacity:.65}
  #ocm-device-menu .ocm-device-menu-name{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #ocm-device-menu .ocm-device-menu-status{margin-left:auto;color:var(--v2-text-text-faint);font-size:11px;white-space:nowrap}
  #ocm-device-menu .ocm-device-menu-loading,#ocm-device-menu .ocm-device-menu-error{padding:6px 8px;color:var(--v2-text-text-faint);font-family:inherit;font-size:13px;font-weight:440;line-height:20px}
  #ocm-device-menu .ocm-device-menu-dot{width:6px;height:6px;flex:0 0 6px;border-radius:9999px;background:var(--ocm-device-unknown,rgba(127,127,127,.65))}
  #ocm-device-menu .ocm-device-menu-dot[data-state="online"]{background:var(--icon-success-base,var(--ocm-device-online,#12c905))}
  #ocm-device-menu .ocm-device-menu-dot[data-state="offline"]{background:var(--icon-critical-base,var(--ocm-device-offline,#fc533a))}
  #ocm-device-menu .ocm-device-menu-dot[data-state="unavailable"]{background:var(--icon-critical-base,var(--ocm-device-offline,#fc533a))}
  @media (prefers-color-scheme:light){:root{--ocm-device-online:#7add71;--ocm-device-offline:#ed4831}}
  :root[data-color-scheme="light"]{--ocm-device-online:#7add71;--ocm-device-offline:#ed4831}
  :root[data-color-scheme="dark"]{--ocm-device-online:#12c905;--ocm-device-offline:#fc533a}
  #ocm-mesh-bar .ocm-transport{margin-left:auto;display:flex;align-items:center;gap:6px;color:var(--v2-text-text-base)}
  #ocm-mesh-bar .ocm-dot{width:6px;height:6px;border-radius:9999px;background:var(--icon-success-base,var(--ocm-device-online,#12c905))}
  #ocm-mesh-bar .ocm-dot[data-kind="relay"]{background:#3b82f6}
  #ocm-mesh-bar .ocm-dot[data-health="unknown"]{background:var(--ocm-device-unknown,rgba(127,127,127,.65))}
  #ocm-mesh-bar .ocm-dot[data-health="unavailable"],#ocm-mesh-bar .ocm-dot[data-health="offline"]{background:var(--icon-critical-base,var(--ocm-device-offline,#fc533a))}
  /* Pre-script fallback only. The app declares its own h-dvh, which measures the
     layout viewport, so Mesh states the box height itself. This is not a reliable
     correction on its own -- a device readout showed this declaration not taking
     effect there at all -- and applyVisibleViewportHeight() below overrules it once
     the script runs. An installed WebAPK also reports no safe-area inset here: its
     window covers the full available height, so there is nothing to subtract. */
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
    const deviceWrap = document.createElement('span');
    deviceWrap.className = 'ocm-device-menu-wrap';
    const device = document.createElement('button');
    device.type = 'button';
    device.className = 'ocm-device-menu-button';
    device.setAttribute('aria-haspopup', 'menu');
    device.setAttribute('aria-expanded', 'false');
    const deviceLabel = document.createElement('span');
    deviceLabel.className = 'ocm-device-menu-label';
    device.appendChild(deviceLabel);
    deviceWrap.appendChild(device);
    const transport = document.createElement('span');
    transport.className = 'ocm-transport';
    bar.append(title, version, deviceWrap, transport);
    document.body.insertBefore(bar, document.body.firstChild);
    device.addEventListener('click', toggleDeviceMenu);
    return bar;
  }

  let deviceMenu = { open: false, generation: 0, timer: null, devices: [], hasRenderedDevices: false, renderedCurrentDeviceId: undefined };
  let deviceStatus = { request: null, timer: null };

  function deviceMenuButton() { return ensureBar()?.querySelector('.ocm-device-menu-button'); }

  function closeDeviceMenu({ focus = false } = {}) {
    if (!deviceMenu.open) return;
    deviceMenu.open = false;
    deviceMenu.generation += 1;
    clearTimeout(deviceMenu.timer);
    deviceMenu.timer = null;
    document.getElementById('ocm-device-menu')?.remove();
    const button = deviceMenuButton();
    button?.setAttribute('aria-expanded', 'false');
    if (focus) button?.focus();
  }

  function positionDeviceMenu(panel) {
    const rect = deviceMenuButton()?.getBoundingClientRect();
    if (!rect) return;
    const width = Math.min(360, Math.max(0, window.innerWidth - 16));
    panel.style.position = 'fixed';
    panel.style.width = width + 'px';
    panel.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8)) + 'px';
    panel.style.top = Math.max(8, Math.min(rect.bottom + 6, window.innerHeight - 8)) + 'px';
  }

  function ensureDeviceMenuPanel() {
    let panel = document.getElementById('ocm-device-menu');
    if (panel) return panel;
    const wrap = ensureBar()?.querySelector('.ocm-device-menu-wrap');
    if (!wrap) return null;
    panel = document.createElement('div');
    panel.id = 'ocm-device-menu';
    panel.tabIndex = -1;
    panel.setAttribute('role', 'menu');
    wrap.appendChild(panel);
    return panel;
  }

  // A control-online legacy Agent has no upstream observation. It may be tried,
  // but is never presented as healthy until the Gateway reports that result.
  function deviceHealth(item) { return item?.upstream_health === undefined ? 'unknown' : item.upstream_health; }
  function canAttemptDevice(item) {
    const health = deviceHealth(item);
    return item?.online === true && (health === 'healthy' || health === 'unknown');
  }

  function deviceMenuItems() {
    return [...(document.getElementById('ocm-device-menu')?.querySelectorAll('.ocm-device-menu-item') || [])].filter(item => !item.disabled);
  }

  function navigateDeviceMenu(event) {
    const items = deviceMenuItems();
    if (!items.length) return;
    let index = items.indexOf(document.activeElement);
    if (event.key === 'ArrowDown') index = (index + 1 + items.length) % items.length;
    else if (event.key === 'ArrowUp') index = (index - 1 + items.length) % items.length;
    else if (event.key === 'Home') index = 0;
    else if (event.key === 'End') index = items.length - 1;
    else return;
    event.preventDefault();
    items[index].focus();
  }

  function renderDeviceMenu(kind = 'devices') {
    if (!deviceMenu.open) return;
    const panel = ensureDeviceMenuPanel();
    if (!panel) return;
    const focusedDeviceId = document.activeElement === panel
      ? panel.dataset.focusDeviceId : document.activeElement?.dataset?.ocmDeviceId;
    delete panel.dataset.focusDeviceId;
    const children = [];
    if (kind !== 'devices') {
      const message = document.createElement('div');
      message.className = kind === 'loading' ? 'ocm-device-menu-loading' : 'ocm-device-menu-error';
      message.textContent = kind === 'loading' ? 'Loading devices…' : 'Device status unavailable.';
      children.push(message);
    } else {
      const current = activeDeviceId();
      deviceMenu.hasRenderedDevices = true;
      deviceMenu.renderedCurrentDeviceId = current;
      for (const item of deviceMenu.devices) {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'ocm-device-menu-item';
        row.setAttribute('role', 'menuitem');
        row.dataset.ocmDeviceId = item.device_id;
        const online = item.online === true;
        const health = deviceHealth(item);
        const canAttempt = canAttemptDevice(item);
        const available = item.available === true;
        row.disabled = !canAttempt;
        if (item.device_id === current) row.setAttribute('aria-current', 'true');
        const dot = document.createElement('span');
        dot.className = 'ocm-device-menu-dot';
        dot.dataset.state = available ? 'online' : online ? (health === 'unknown' ? 'unknown' : 'unavailable') : item.online === false ? 'offline' : 'unknown';
        const name = document.createElement('span');
        name.className = 'ocm-device-menu-name';
        name.textContent = item.name || item.device_id || 'Unnamed device';
        const status = document.createElement('span');
        status.className = 'ocm-device-menu-status';
        status.textContent = !online ? (item.online === false ? 'Agent offline' : 'Unknown')
          : health === 'healthy' ? 'Healthy'
            : health === 'auth_failed' ? 'OpenCode authentication failed'
              : health === 'unreachable' ? 'OpenCode unavailable'
                : health === 'unhealthy' ? 'OpenCode unhealthy'
                  : health === 'unknown' ? 'OpenCode status unknown' : 'OpenCode health invalid';
        row.append(dot, name, status);
        if (canAttempt) row.addEventListener('click', () => {
          if (row.disabled) return;
          closeDeviceMenu();
          location.assign('/?mesh_device=' + encodeURIComponent(item.device_id));
        });
        children.push(row);
      }
    }
    if (kind !== 'devices') {
      deviceMenu.hasRenderedDevices = false;
      deviceMenu.renderedCurrentDeviceId = undefined;
    }
    panel.replaceChildren(...children);
    positionDeviceMenu(panel);
    if (focusedDeviceId) {
      const restored = deviceMenuItems().find(item => item.dataset.ocmDeviceId === focusedDeviceId);
      (restored || panel).focus();
    }
  }

  function scheduleDeviceMenuRefresh(generation) {
    clearTimeout(deviceMenu.timer);
    deviceMenu.timer = setTimeout(() => {
      if (deviceMenu.open && deviceMenu.generation === generation) refreshDeviceMenu(generation);
    }, 5000);
  }

  function scheduleDeviceStatusRefresh() {
    if (deviceStatus.timer) return;
    deviceStatus.timer = setTimeout(() => {
      deviceStatus.timer = null;
      if (deviceMenu.open) { scheduleDeviceStatusRefresh(); return; }
      refreshDeviceStatus().catch(() => {}).finally(scheduleDeviceStatusRefresh);
    }, 5000);
  }

  function sameDeviceMenuDevices(previous, next) {
    return previous.length === next.length && previous.every((device, index) => {
      const updated = next[index];
      return updated && device.device_id === updated.device_id && device.name === updated.name && device.online === updated.online
        && device.upstream_health === updated.upstream_health && device.available === updated.available;
    });
  }

  // Only a completed discovery makes the local device snapshot authoritative,
  // and its timestamp bounds how long that verdict may refuse requests.
  function setDeviceStatusUnknown() {
    // Without a completed discovery the snapshot proves nothing, so it can no
    // longer refuse requests; keeping it for display is a separate concern.
    state.deviceSnapshotAt = null;
    state.devices = state.devices.map(device => ({ ...device, upstream_health: 'unknown', available: false }));
    renderBar();
  }

  async function refreshDeviceStatus(menuGeneration) {
    if (deviceStatus.request) {
      // A menu reopened after its own request was cancelled must not render that
      // stale response. Background and current-menu callers share the request.
      if (menuGeneration !== undefined && deviceStatus.request.menuGeneration !== undefined
          && deviceStatus.request.menuGeneration !== menuGeneration) {
        deviceStatus.request.controller.abort();
        deviceStatus.request = null;
      } else return deviceStatus.request.promise;
    }
    const controller = new AbortController();
    const request = { controller, menuGeneration, promise: null };
    deviceStatus.request = request;
    let rejectDeadline;
    const deadlineExpired = new Promise((_, reject) => { rejectDeadline = reject; });
    const deadline = setTimeout(() => {
      controller.abort();
      rejectDeadline(new Error('Mesh device discovery timed out'));
    }, 10000);
    request.promise = (async () => {
      try {
        const response = await Promise.race([nativeFetch('/_mesh/devices', { credentials: 'same-origin', cache: 'no-store', signal: controller.signal }), deadlineExpired]);
        if (!response.ok) throw new Error('Mesh device discovery failed: ' + response.status);
        const payload = await Promise.race([response.json(), deadlineExpired]);
        if (deviceStatus.request !== request) return;
        state.devices = Array.isArray(payload.devices) ? payload.devices : [];
        state.deviceSnapshotAt = Date.now();
        renderBar();
      } catch (error) {
        if (deviceStatus.request === request) setDeviceStatusUnknown();
        throw error;
      } finally {
        clearTimeout(deadline);
        if (deviceStatus.request === request) deviceStatus.request = null;
      }
    })();
    return request.promise;
  }

  async function refreshDeviceMenu(generation = deviceMenu.generation) {
    if (!deviceMenu.open || generation !== deviceMenu.generation) return;
    if (!deviceMenu.hasRenderedDevices) renderDeviceMenu('loading');
    try {
      await refreshDeviceStatus(generation);
      if (!deviceMenu.open || generation !== deviceMenu.generation) return;
      const devices = state.devices;
      if (!deviceMenu.hasRenderedDevices || deviceMenu.renderedCurrentDeviceId !== activeDeviceId() || !sameDeviceMenuDevices(deviceMenu.devices, devices)) {
        deviceMenu.devices = devices;
        renderDeviceMenu();
      }
      scheduleDeviceMenuRefresh(generation);
    } catch (_) {
      if (!deviceMenu.open || generation !== deviceMenu.generation) return;
      deviceMenu.devices = [];
      renderDeviceMenu('error');
      scheduleDeviceMenuRefresh(generation);
    }
  }

  function toggleDeviceMenu() {
    if (deviceMenu.open) { closeDeviceMenu(); return; }
    deviceMenu.open = true;
    deviceMenu.generation += 1;
    deviceMenu.devices = [];
    deviceMenu.hasRenderedDevices = false;
    deviceMenu.renderedCurrentDeviceId = undefined;
    deviceMenuButton()?.setAttribute('aria-expanded', 'true');
    refreshDeviceMenu(deviceMenu.generation);
  }

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && deviceMenu.open) { event.preventDefault(); closeDeviceMenu({ focus: true }); }
    else if (deviceMenu.open) navigateDeviceMenu(event);
  });
  document.addEventListener('pointerdown', event => {
    const wrap = ensureBar()?.querySelector('.ocm-device-menu-wrap');
    if (deviceMenu.open && wrap && !wrap.contains(event.target)) closeDeviceMenu();
  });

  function currentDeviceInfo() {
    const routeId = currentDeviceId();
    const id = routeId || (state.manifest && state.manifest.device_id) || state.defaultDevice;
    const device = state.devices.find(item => item.device_id === id);
    return { name: (device && device.name) || id || 'no device', online: device ? !!device.online : undefined,
      health: device?.upstream_health === undefined ? 'unknown' : device.upstream_health, available: device?.available === true };
  }

  function deviceHealthInfo(info) {
    if (info.online === false) return { state: 'offline', label: 'Agent offline' };
    if (info.available && info.health === 'healthy') return { state: 'healthy', label: 'OpenCode healthy' };
    if (info.health === 'auth_failed') return { state: 'unavailable', label: 'OpenCode authentication failed' };
    if (info.health === 'unreachable') return { state: 'unavailable', label: 'OpenCode unavailable' };
    if (info.health === 'unhealthy') return { state: 'unavailable', label: 'OpenCode unhealthy' };
    if (info.health === 'unknown') return { state: 'unknown', label: 'OpenCode status unknown' };
    return { state: 'unavailable', label: 'OpenCode health invalid' };
  }

  function transportInfo() {
    // While a foreground probe is verifying the old channel, new requests are
    // routed over Relay; the bar shows the effective transport of new requests.
    if (state.probing) {
      return { kind: 'relay', label: state.relayRtt != null ? 'Relay ' + state.relayRtt + 'ms' : 'Relay' };
    }
    if (state.channel && state.channel.readyState === 'open') {
      return { kind: 'p2p', label: state.rtt != null ? 'P2P ' + state.rtt + 'ms' : 'P2P' };
    }
    return { kind: 'relay', label: state.relayRtt != null ? 'Relay ' + state.relayRtt + 'ms' : 'Relay' };
  }

  async function measureRelayRtt() {
    if (state.handoffPending) return;
    if (state.channel && state.channel.readyState === 'open') return;
    if (document.hidden) return;
    const deviceId = activeDeviceId();
    if (!deviceId) return;
    const base = '/_mesh/device/' + encodeURIComponent(deviceId);
    const started = Date.now();
    try {
      const response = await nativeFetch(base + '/api/info', { credentials: 'same-origin', cache: 'no-store' });
      const elapsed = Date.now() - started;
      if (response.ok && !document.hidden && elapsed >= 0 && elapsed <= RTT_MAX_MS) { state.relayRtt = elapsed; renderBar(); }
    } catch (_) {}
  }

  function renderBar() {
    const bar = ensureBar();
    if (!bar) return;
    const info = currentDeviceInfo();
    const health = deviceHealthInfo(info);
    const transport = transportInfo();
    const device = bar.querySelector('.ocm-device-menu-button');
    device.querySelector('.ocm-device-menu-label').textContent = info.name;
    device.dataset.offline = String(info.online === false);
    device.dataset.health = health.state;
    device.setAttribute('title', health.label);
    device.setAttribute('aria-label', info.name + ': ' + health.label);
    const transportLabel = health.state === 'healthy' ? transport.label : health.label + ' · ' + transport.label;
    bar.querySelector('.ocm-transport').innerHTML =
      '<span class="ocm-dot" data-kind="' + transport.kind + '" data-health="' + health.state + '"></span><span title="' + health.label + '">' + transportLabel + '</span>';
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
    const match = location.pathname.match(/^\/server\/([^/]+)(\/.*)?$/);
    if (!match) return virtualDeviceId(location.pathname);
    try {
      const server = new URL(decodeServer(match[1]));
      if (server.origin !== location.origin) return null;
      return virtualDeviceId(server.pathname);
    } catch (_) { return null; }
  };
  const selectedServerUrl = () => {
    try {
      const servers = readJson('opencode.global.dat:server', { list: [] }).list || [];
      const layout = readJson('opencode.global.dat:layout', {});
      const selected = layout.home?.selection?.server;
      if (!selected) return null;
      if (/^https?:\/\//i.test(selected)) return selected;
      const match = servers.find(entry => {
        const url = entry?.http?.url;
        if (!url) return false;
        const normalized = String(url).replace(/\/+$/, '');
        return selected === url || selected === normalized || selected.endsWith(normalized);
      });
      return match?.http?.url || null;
    } catch (_) { return null; }
  };
  const selectedServerDeviceId = () => {
    try {
      const url = selectedServerUrl();
      return url ? virtualDeviceId(new URL(url, location.href).pathname) : null;
    } catch (_) { return null; }
  };
  const activeDeviceId = () => currentDeviceId() || selectedServerDeviceId() || state.manifest?.device_id || state.defaultDevice;
  const serverRoutePath = path => {
    const match = path.match(/^\/server\/([^/]+)(\/.*)?$/);
    if (!match) return null;
    try {
      const server = new URL(decodeServer(match[1]));
      if (server.origin !== location.origin || !virtualDeviceId(server.pathname)) return null;
      return match[2] || '/';
    } catch (_) { return null; }
  };
  const scopeNativeRequest = (input, init) => {
    // A bare origin is the default Server; switching pages must not change where its background requests go.
    const deviceId = state.defaultDevice;
    if (!deviceId) return [input, init];
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url, location.href);
    const httpOrigin = url.origin.replace(/^ws:/, 'http:').replace(/^wss:/, 'https:');
    if (httpOrigin !== location.origin || url.pathname.startsWith('/_mesh/') || url.pathname.startsWith('/server/') || virtualDeviceId(url.pathname)) return [input, init];
    url.pathname = '/_mesh/device/' + encodeURIComponent(deviceId) + url.pathname;
    if (input instanceof Request) return [new Request(url.href, init ? new Request(input, init) : input), undefined];
    return [url.href, init];
  };
  // Resolve the device a request would actually reach, using exactly the
  // attribution scopeNativeRequest applies: an explicit device prefix wins, a
  // bare path binds to the current default device, and Mesh control endpoints
  // and native server routes carry no device of their own here.
  const requestDeviceId = url => {
    const explicit = virtualDeviceId(url.pathname);
    if (explicit) return explicit;
    if (url.pathname.startsWith('/_mesh/') || url.pathname.startsWith('/server/')) return null;
    return state.defaultDevice || null;
  };
  // A navigation is not a retry loop. The Gateway answers Accept: text/html with
  // its offline recovery page (Gateway.wants_html uses the same test), so the
  // local refusal must stay out of the way and let the request travel. Accept
  // reaches fetch through init.headers, through a Request input, or through
  // both; init.headers wins in the platform, and an Accept that cannot be read
  // here is treated as a navigation, because forwarding costs one request while
  // refusing would replace the recovery page with raw JSON.
  const acceptsHtml = (input, init) => {
    const sources = [init && init.headers, typeof input === 'string' || input instanceof URL ? null : input && input.headers];
    for (const headers of sources) {
      if (!headers) continue;
      try {
        const accept = new Headers(headers).get('accept');
        if (accept) return accept.includes('text/html');
      } catch (_) {
        return true;
      }
    }
    return false;
  };
  // Last-resort local refusal for a device the recent snapshot marks offline.
  // The Gateway answers 503 immediately for that device anyway, so this only
  // keeps an upstream retry loop off the network (an offline device previously
  // drew about 3000 requests per hour). It covers every explicit device target,
  // not just the page own device: a V2 page subscribes several registered
  // Servers at once and each offline one keeps its own retry loop alive. It is
  // deliberately narrow: a foreign origin, a navigation, no snapshot, a stale
  // one, a failed one, an absent or ambiguous device, or an online device all
  // fall through to the normal transport. Every method is answered, because the
  // server refuses all of them; nothing is sent, so a request that is refused
  // here is known not to have been emitted anywhere.
  const offlineDeviceRefusal = (url, input, init) => {
    if (url.origin !== location.origin) return null;
    if (acceptsHtml(input, init)) return null;
    if (state.deviceSnapshotAt == null) return null;
    if (Date.now() - state.deviceSnapshotAt > DEVICE_SNAPSHOT_TTL_MS) return null;
    const deviceId = requestDeviceId(url);
    if (!deviceId) return null;
    const device = state.devices.find(item => item.device_id === deviceId);
    if (!device || device.online !== false) return null;
    return new Response(JSON.stringify({ error: OFFLINE_BODY_ERROR, device_id: deviceId }),
      { status: 503, headers: { 'content-type': 'application/json' } });
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
      // Offline-page handoffs are accepted only at the V2 home route. A session
      // deep link already identifies its Server and must never be retargeted by
      // an incidental query parameter.
      const handoffDevice = location.pathname === '/' ? new URLSearchParams(location.search).get('mesh_device') : null;
      const response = await nativeFetch('/_mesh/devices', { credentials: 'same-origin', signal: AbortSignal.timeout(10000) });
      if (!response.ok) throw new Error('Mesh device discovery failed: ' + response.status);
      const payload = await response.json();
       state.devices = Array.isArray(payload.devices) ? payload.devices : [];
       state.deviceSnapshotAt = Date.now();
       state.defaultDevice = payload.default_device || null;
       renderBar();
      const unsupported = new Set();
      const canAttempt = device => device.online === true && (device.upstream_health === undefined || device.upstream_health === 'healthy' || device.upstream_health === 'unknown');
      const devices = (await Promise.all(state.devices.filter(canAttempt).map(async device => {
        try {
          const info = await nativeFetch(serverTabUrl(device.device_id) + '/api/info', {
            credentials: 'same-origin', signal: AbortSignal.timeout(5000)
          });
          if (!info.ok) return null;
          if (info.headers.get('content-type')?.includes('application/json') && String((await info.json()).version).startsWith('2.')) return device;
          unsupported.add(device.device_id);
          return null;
        } catch (_) { return null; }
      }))).filter(Boolean);
      const store = readJson('opencode.global.dat:server', { list: [], projects: {}, lastProject: {}, recentlyClosed: {} });
      const requested = handoffDevice && state.devices.find(device => device.device_id === handoffDevice);
      // An explicit click is not a preference: keeping the query and retrying is
      // safer than silently opening another device when discovery, reachability,
      // or the V2 probe has not confirmed the requested target.
       if (handoffDevice && (!requested || !canAttempt(requested) || !devices.some(device => device.device_id === handoffDevice))) {
        throw new Error('Requested device handoff is unavailable or not an OpenCode V2 server');
      }
       const primary = requested || state.devices.find(device => canAttempt(device) && device.device_id === payload.configured_default_device)
        || devices.find(device => device.device_id === payload.default_device) || devices[0];
      if (!primary) throw new Error('No OpenCode V2 device available');
      // Preserve the configured target during a transient failure, but do not
      // initialize an unknown-health target without a successful V2 probe.
      if ((primary.upstream_health === undefined || primary.upstream_health === 'unknown')
          && !devices.some(device => device.device_id === primary.device_id)) {
        throw new Error('Default device has not been verified as an OpenCode V2 server');
      }
      if (unsupported.has(primary.device_id)) {
        throw new Error('Default device is not an OpenCode V2 server');
      }
      const primaryUrl = serverTabUrl(primary.device_id);
      state.defaultDevice = primary.device_id;
      const original = Array.isArray(store.list) ? store.list : [];
      const alias = original.find(entry => entry.http?.url?.replace(/\/+$/, '') === location.origin);
      store.list = original.filter(entry => entry.http?.url?.replace(/\/+$/, '') !== location.origin);
      // The native server list is user-managed; only add newly discovered V2 entries without overwriting names or external addresses.
      const existing = new Set((store.list || []).map(entry => entry.http?.url?.replace(/\/+$/, '')));
      const candidates = devices.some(device => device.device_id === primary.device_id) ? devices : [primary, ...devices];
      const added = candidates.filter(device => !existing.has(serverTabUrl(device.device_id))).map(device =>
        ({ type: 'http', displayName: (device.device_id === primary.device_id && alias?.displayName) || device.name || device.device_id,
          http: { url: serverTabUrl(device.device_id) } }));
      store.list = [...(store.list || []), ...added];
      localStorage.setItem('opencode.global.dat:server', JSON.stringify(store));
      const defaultKey = 'opencode.settings.dat:defaultServerUrl';
      const previous = localStorage.getItem(defaultKey);
       const initial = primary;
      if (!previous || previous.replace(/\/+$/, '') === location.origin) localStorage.setItem(defaultKey, serverTabUrl(initial.device_id));
      const layout = readJson('opencode.global.dat:layout', {});
      if (handoffDevice || layout.home?.selection?.server?.replace(/\/+$/, '') === location.origin) {
        layout.home = layout.home || {};
        layout.home.selection = layout.home.selection || {};
        layout.home.selection.server = primaryUrl;
        if (handoffDevice) delete layout.home.directory;
        localStorage.setItem('opencode.global.dat:layout', JSON.stringify(layout));
      }
      const pwaKey = 'opencode.pwa.last-route';
      const previousRoute = localStorage.getItem(pwaKey);
      if (previousRoute) {
        for (const origin of [location.origin, location.origin + '/']) {
          const prefix = '/server/' + encodeServer(origin) + '/';
          if (previousRoute.startsWith(prefix)) {
            localStorage.setItem(pwaKey, '/server/' + encodeServer(primaryUrl) + '/' + previousRoute.slice(prefix.length));
          }
        }
      }
      // Point canonicalLocalServer at the explicit device too; existing local project/window state stays preserved by the native migration.
      window.__ocmBootstrap.serverUrl = primaryUrl;
      // Bootstrap starts P2P before discovery completes. Switch generations only
      // after all native selection state agrees so an old manifest cannot win a
      // race or trigger a duplicate negotiation for the previous default.
      if (handoffDevice && state.transportStarted && state.routeDeviceId !== primary.device_id) reconnectForDevice();
      if (handoffDevice && !state.transportStarted) state.routeDeviceId = primary.device_id;
      if (handoffDevice) {
        const clean = new URL(location.href);
        clean.searchParams.delete('mesh_device');
        history.replaceState(history.state, '', clean.pathname + clean.search + clean.hash);
      }
  }

  async function bootstrapServers() {
    // Startup failures are visible and auto-retried; keep the same ready Promise so the entry module continues initializing after recovery.
    for (;;) {
      try {
        await syncNativeServers();
        window.__ocmBootstrap.error = null;
        document.getElementById('ocm-bootstrap-status')?.remove();
        return;
      } catch (error) {
        window.__ocmBootstrap.error = String(error);
        const parent = document.body || document.documentElement;
        if (parent) {
          const notice = document.getElementById('ocm-bootstrap-status') || document.createElement('div');
          notice.id = 'ocm-bootstrap-status';
          notice.textContent = '暂时无法连接 OpenCode 设备，正在自动重试…';
          notice.style.cssText = 'position:fixed;inset:40px 16px auto;padding:16px;background:#222;color:#fff;z-index:2147483647';
          if (!notice.parentNode) parent.appendChild(notice);
        }
        await new Promise(resolve => setTimeout(resolve, 3000));
      }
    }
  }

  function releaseIncoming(messageId) {
    const entry = state.incoming.get(messageId);
    if (!entry) return null;
    state.incoming.delete(messageId);
    clearTimeout(entry.timer);
    state.incomingBytes = Math.max(0, state.incomingBytes - entry.bytes);
    return entry;
  }

  function releaseIncomingForBusinessId(id) {
    for (const [messageId, entry] of state.incoming) {
      // HTTP/SSE normally use the request ID as message_id, while a WebSocket
      // logical message has its own envelope ID and carries the socket ID here.
      if (messageId === id || entry.businessId === id) releaseIncoming(messageId);
    }
  }

  function releaseAllIncoming() {
    for (const messageId of state.incoming.keys()) releaseIncoming(messageId);
  }

  function rememberIncomingTombstone(id) {
    if (!id) return;
    const previous = state.incomingTombstones.get(id);
    if (previous) clearTimeout(previous);
    while (state.incomingTombstones.size >= MAX_INCOMING_MESSAGES) {
      const oldest = state.incomingTombstones.entries().next().value;
      if (!oldest) break;
      clearTimeout(oldest[1]);
      state.incomingTombstones.delete(oldest[0]);
    }
    const timer = setTimeout(() => {
      if (state.incomingTombstones.get(id) === timer) state.incomingTombstones.delete(id);
    }, INCOMING_ASSEMBLY_TTL_MS);
    state.incomingTombstones.set(id, timer);
  }

  function releaseIncomingTombstones() {
    for (const timer of state.incomingTombstones.values()) clearTimeout(timer);
    state.incomingTombstones.clear();
  }

  function armIncomingDeadline(messageId, entry) {
    clearTimeout(entry.timer);
    entry.timer = setTimeout(() => {
      if (state.incoming.get(messageId) !== entry) return;
      failIncoming(messageId, new Error('P2P message assembly timeout'));
    }, INCOMING_ASSEMBLY_TTL_MS);
  }

  function failIncoming(messageId, error) {
    const entry = releaseIncoming(messageId);
    const businessId = entry?.businessId;
    if (!businessId) return;
    const socket = state.sockets.get(businessId);
    if (socket) socket.fail(error);
    else rejectEntry(businessId, error);
  }

  function incomingEntry(messageId, message) {
    let entry = state.incoming.get(messageId);
    if (entry) return entry;
    if (state.incoming.size >= MAX_INCOMING_MESSAGES) throw new Error('P2P incoming message limit exceeded');
    entry = { next: 0, chunks: [], bytes: 0, kind: null, meta: null, businessId: null, timer: null };
    state.incoming.set(messageId, entry);
    armIncomingDeadline(messageId, entry);
    return entry;
  }

  function decodeIncomingChunk(data, limit) {
    if (typeof data !== 'string' || limit < 0 || data.length > Math.ceil(limit / 3) * 4) {
      throw new Error('P2P incoming message too large');
    }
    try {
      const bytes = unb64(data);
      if (bytes.length > limit) throw new Error('P2P incoming message too large');
      return bytes;
    } catch (error) {
      if (error.message === 'P2P incoming message too large') throw error;
      throw new Error('invalid base64 encoding');
    }
  }

  function rejectEntry(id, error) {
    // Finish an in-flight request or stream and notify the Agent to cancel it.
    const err = error instanceof Error ? error : new Error(String(error));
    releaseIncomingForBusinessId(id);
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
    if (entry || stream) rememberIncomingTombstone(id);
    if (entry) send({ type: 'cancel', id }, entry.channel).catch(() => {});
  }

  function failTransport(error) {
    clearProbe();
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
    releaseAllIncoming();
    releaseIncomingTombstones();
    for (const socket of state.sockets.values()) {
      // Mark as terminated so queued send tasks cannot later fail() and emit error/close again.
      socket._done = true;
      socket.readyState = MeshWebSocket.CLOSED;
      socket.dispatch('error', error);
      socket.dispatch('close', { code: 1011, reason: error.message });
    }
    state.sockets.clear();
  }

  // The foreground health probe verifies that the old open P2P channel is still
  // alive after a background round (lock screen / frozen tab). It is bound to
  // the exact channel and generation it started on, owns its own ping timestamp
  // (never `state.pingSent`, so the periodic ping cannot overwrite it) and has a
  // single 3s deadline: repeated resume events neither restart nor extend it.
  function beginForegroundProbe() {
    // Only an open channel left over from the background can be verified; a
    // visible bootstrap or a relay-only resume never probes (not a lock screen).
    if (!state.channel || state.channel.readyState !== 'open') return;
    if (state.probe) return;
    const channel = state.channel;
    // The Agent echoes t verbatim. A unique token cannot collide with periodic
    // timestamps or a cancelled probe restarted in the same millisecond.
    const probe = { channel, generation: state.generation, pingT: 'probe:' + makeId(), startedAt: Date.now(), timer: null };
    state.probe = probe;
    state.probing = true;
    renderBar();
    probe.timer = setTimeout(() => failProbe(probe), PROBE_TIMEOUT_MS);
    send({ type: 'ping', t: probe.pingT }, channel).catch(() => {
      // A send failure means the channel is already dead; fail immediately.
      if (state.probe === probe) failProbe(probe);
    });
  }

  // A pong carrying the probe timestamp arrived: the channel is healthy, keep it
  // and restore P2P routing without any teardown.
  function completeProbe(probe) {
    if (!state.probe || state.probe !== probe) return; // late pong from a superseded probe
    if (probe.channel !== state.channel || probe.generation !== state.generation) return;
    if (Date.now() - probe.startedAt >= PROBE_TIMEOUT_MS) { failProbe(probe); return; }
    clearTimeout(probe.timer);
    state.probe = null;
    state.probing = false;
    // Only the current channel may keep the freshly measured latency.
    if (probe.channel === state.channel && probe.channel.readyState === 'open') {
      const rtt = Date.now() - probe.startedAt;
      if (rtt >= 0 && rtt <= RTT_MAX_MS) state.rtt = rtt;
    }
    renderBar();
  }

  // The probe deadline lapsed (or its ping could not be sent): the old channel
  // is stale. Fail everything still bound to it with the documented
  // unknown-outcome error (already-sent mutations are never replayed) and
  // rebuild P2P in the background.
  function failProbe(probe) {
    if (!state.probe || state.probe !== probe) return;
    clearTimeout(probe.timer);
    state.probe = null;
    state.probing = false;
    // The probe only ever applies to the channel and generation it was started
    // for; a superseded or already-replaced channel must not be failed by a late
    // timeout, and a teardown here never affects a newer connection.
    if (probe.channel !== state.channel || probe.generation !== state.generation) return;
    releaseCurrentAttempt(true);
    failTransport(new Error('P2P disconnected; request outcome may be unknown'));
    state.closed = true;
    runAttemptForCurrentDevice();
  }

  // Drop any in-flight probe (used when a teardown invalidates its channel).
  function clearProbe() {
    if (!state.probe) return;
    clearTimeout(state.probe.timer);
    state.probe = null;
    state.probing = false;
  }

  function settleMessage(message) {
    if (message.type === 'pong') {
      // A foreground probe owns its own timestamp; its pong completes the
      // probe even when a periodic ping with another timestamp is in flight.
      const probe = state.probe;
      if (probe && message.t === probe.pingT) {
        completeProbe(probe);
        return;
      }
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
        // Drop late data when close is pending or the socket never opened (spec: no data frames after CLOSING).
        if (socket.readyState !== MeshWebSocket.OPEN) return;
        let data;
        if (message.kind === 'bytes') {
          const bytes = unb64(message.data);
          data = socket.binaryType === 'arraybuffer' ? bytes.buffer : new Blob([bytes]);
        } else {
          data = message.data || '';
        }
        socket.dispatch('message', { data });
      } else if (message.type === 'ws_closed' || message.type === 'ws_error') {
        // Agent-side termination also marks the socket terminated so queued tasks cannot re-emit events via fail().
        socket._done = true;
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
      rememberIncomingTombstone(message.id);
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
        rememberIncomingTombstone(message.id);
        stream.controller.error(abortError);
        entry.reject(abortError);
        return;
      }
      rememberIncomingTombstone(message.id);
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
      if (message.type === 'stream_chunk' && message.body !== undefined) {
        try {
          stream.controller.enqueue(unb64(message.body));
        } catch (_) {
          rejectEntry(message.id, new Error('invalid base64 encoding'));
          return;
        }
      }
      if (message.type === 'stream_end' || message.type === 'stream_error') {
        state.streams.delete(message.id);
        state.pending.delete(message.id);
        if (entry.timer) clearTimeout(entry.timer);
        rememberIncomingTombstone(message.id);
        // Pre-first-frame proxy errors keep the same HTTP/JSON semantics as Relay; an already-started stream can only be aborted.
        if (message.type === 'stream_error' && !entry.resolved) {
          // Mirror INVALID_ENCODING_REASON from src/p2p.py, mapped to 400; other proxy errors map to 502.
          const reason = message.reason || message.error || 'stream failed';
          entry.resolve({ status: reason === 'invalid base64 encoding' ? 400 : 502,
            headers: { 'content-type': 'application/json' } });
          stream.controller.enqueue(enc.encode(JSON.stringify({ error: reason, reason })));
          stream.controller.close();
          return;
        }
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
    if (state.incomingTombstones.has(id)) return;
    try {
      const entry = incomingEntry(id, message);
      if (message.sequence !== entry.next) throw new Error('invalid frame sequence');
      entry.next += 1;
      if (message.type) {
        // Response, stream, and terminal metadata must use the request's
        // canonical envelope ID. Control/WS payloads are decoded before they
        // are associated with a socket, so an invalid unknown frame cannot
        // cancel an unrelated request merely by claiming its id.
        if (message.id == null || String(message.id) !== id) throw new Error('invalid P2P message');
        entry.kind = message.type;
        entry.meta = { ...message };
        entry.businessId = id;
        delete entry.meta.message_id;
        delete entry.meta.sequence;
        delete entry.meta.data;
        delete entry.meta.final;
      }
      const bytes = decodeIncomingChunk(message.data || '', Math.min(MAX_P2P_BYTES - entry.bytes, MAX_P2P_BYTES - state.incomingBytes));
      entry.chunks.push(bytes);
      entry.bytes += bytes.length;
      state.incomingBytes += bytes.length;
      if (!message.final) {
        if (!entry.timer) armIncomingDeadline(id, entry);
        return;
      }
      const body = new Uint8Array(entry.bytes);
      let offset = 0;
      for (const chunk of entry.chunks) { body.set(chunk, offset); offset += chunk.length; }
      const meta = entry.meta ? { ...entry.meta } : null;
      if (entry.kind === 'stream_chunk') {
        state.incomingBytes = Math.max(0, state.incomingBytes - entry.bytes);
        entry.bytes = 0;
        entry.chunks = [];
        entry.meta = null;
        clearTimeout(entry.timer);
        entry.timer = null;
        if (meta) {
          meta.id = meta.id || id;
          meta.body = b64(body);
          settleMessage(meta);
        } else {
          settleMessage({ type: 'stream_chunk', id: entry.businessId || id, body: b64(body) });
        }
        return;
      }
      if (meta) {
        releaseIncoming(id);
        meta.id = meta.id || id;
        meta.body = b64(body);
        settleMessage(meta);
        return;
      }
      const decoded = JSON.parse(dec.decode(body));
      releaseIncoming(id);
      settleMessage(decoded);
    } catch (error) {
      const normalized = error instanceof Error && error.message === 'invalid frame sequence'
        ? error : new Error(error instanceof Error && error.message === 'P2P incoming message too large'
          ? error.message : error instanceof Error && error.message === 'invalid base64 encoding'
            ? error.message : 'invalid P2P message');
      failIncoming(id, normalized);
    }
  }

  // The retry chain guards every mutation with its captured generation: an
  // aborted or expired negotiation must never schedule a backoff nor reset the
  // backoff value for a newer attempt.
  function runAttemptForCurrentDevice() {
    // This runs retries, hint rebuilds and device-switch attempts, never the
    // page bootstrap: a fresh attempt here must not borrow the initial fetch
    // wait, even when it replaces the initial negotiation.
    state.isInitialAttempt = false;
    const generation = state.generation;
    state.ready = connectP2P(state.routeDeviceId)
      .then(() => { if (generation === state.generation) state.reconnectDelay = 1000; })
      .catch(() => {
        if (generation !== state.generation) return null;
        state.reconnectDelay = Math.min(state.reconnectDelay * 2, 30000);
        scheduleReconnect();
        return null;
      });
    return state.ready;
  }

  // Release whatever the current attempt left behind so a still-settling old
  // chain can no longer mutate state or reach the network. Clearing both timers
  // drops any pending hint or backoff plan. The channel is unbound before the
  // peer closes so a synchronous close callback cannot re-enter; aborting the
  // controller also interrupts any local wait parked on it.
  function releaseCurrentAttempt(bump) {
    if (bump) state.generation += 1;
    if (state.networkTimer) { clearTimeout(state.networkTimer); state.networkTimer = null; }
    if (state.reconnectTimer) { clearTimeout(state.reconnectTimer); state.reconnectTimer = null; }
    const oldChannel = state.channel;
    if (oldChannel) { oldChannel.onclose = null; oldChannel.onerror = null; oldChannel.onmessage = null; }
    if (state.activeController) state.activeController.abort();
    if (state.pc) { try { state.pc.close(); } catch (_) {} }
    state.pc = null; state.channel = null;
  }

  function scheduleReconnect() {
    if (state.reconnectTimer) return;
    // A hint or a late event must not tear down a channel that is already open.
    if (state.channel && state.channel.readyState === 'open') return;
    // A channel close can leave the open wait hanging while this runs: abort
    // the in-flight negotiation so the old chain cannot pollute the backoff's
    // attempt once it settles, and give that attempt its own generation.
    releaseCurrentAttempt(Boolean(state.activeController));
    failTransport(new Error('P2P disconnected; request outcome may be unknown'));
    state.closed = true;
    const generation = state.generation;
    state.reconnectTimer = setTimeout(() => {
      state.reconnectTimer = null;
      if (generation !== state.generation) return;
      runAttemptForCurrentDevice();
    }, state.reconnectDelay);
  }

  // A network hint resumes P2P right away: an old negotiation in any stage is
  // cancelled and immediately rebuilt, with the already-open channel as the
  // only exception. Rate limiting comes from the debounce + cooldown, not from
  // ignoring hints (the initial attempt may be invalidated and rebuilt too).
  function kickReconnectNow() {
    if (state.channel && state.channel.readyState === 'open') return false;
    releaseCurrentAttempt(Boolean(state.activeController));
    // A channel can die without its close event being delivered yet: fail
    // everything still bound to the old not-open transport instead of leaking
    // pending requests and streams.
    failTransport(new Error('P2P disconnected; request outcome may be unknown'));
    runAttemptForCurrentDevice();
    return true;
  }

  function onNetworkHint() {
    if (state.networkTimer) return;
    const now = Date.now();
    if (state.lastNetworkAttempt != null && now - state.lastNetworkAttempt < NETWORK_COOLDOWN_MS) return;
    state.networkTimer = setTimeout(() => {
      state.networkTimer = null;
      if (kickReconnectNow()) state.lastNetworkAttempt = Date.now();
    }, NETWORK_DEBOUNCE_MS);
  }

  // Resume from the background only when the last attempt is stale, then re-enter
  // through the same debounced, cooldown-controlled hint path: a background-frozen
  // negotiation older than the threshold is cancelled and retried, and overlapping
  // hints collapse into a single flight. Recent attempts are left alone (the
  // caller already refreshed the probes).
  function onForegroundResume() {
    if (state.channel && state.channel.readyState === 'open') return;
    if (state.lastAttemptTime == null) return;
    if (Date.now() - state.lastAttemptTime < FOREGROUND_RETRY_MS) return;
    onNetworkHint();
  }

  async function connectP2P(deviceId) {
    state.closed = false;
    // Each device switch increments generation; old attempts invalidate themselves.
    const myGeneration = state.generation;
    const abandoned = () => state.generation !== myGeneration;
    // Bound setup stages without their own timeout so sockets cannot stay CONNECTING.
    const controller = new AbortController();
    state.activeController = controller;
    state.lastAttemptTime = Date.now();
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
      // The server picks the default device when no explicit route was known;
      // keep the route in sync so periodic re-checks do not treat it as a switch.
      state.routeDeviceId = deviceId || manifest.device_id || state.routeDeviceId;
      if (!manifest.p2p || !manifest.p2p.enabled || !window.RTCPeerConnection) throw new Error('p2p unavailable');
      pc = new RTCPeerConnection({ iceServers: (manifest.stun_servers || []).map(urls => ({ urls })) });
      const channel = pc.createDataChannel('opencode-mesh', { ordered: true });
      // Only current-generation events from the current channel affect global state.
      const alive = () => !abandoned() && state.channel === channel;
      channel.onclose = () => { if (alive()) scheduleReconnect(); };
      channel.onerror = () => { if (alive()) scheduleReconnect(); };
      channel.onmessage = event => { if (!alive()) return; try { settle(JSON.parse(typeof event.data === 'string' ? event.data : dec.decode(event.data))); } catch (_) {} };
      state.pc = pc; state.channel = channel;
      // Every setup stage joins the total deadline and the cancel wait, so the
      // 40s covers the whole negotiation and a stale chain can never send an
      // offer for the new network.
      const offer = await waitWithDeadline(pc.createOffer(), CONNECT_TIMEOUT_MS, 'p2p create offer', controller);
      if (abandoned()) return dispose();
      await waitWithDeadline(pc.setLocalDescription(offer), CONNECT_TIMEOUT_MS, 'p2p set local description', controller);
      if (abandoned()) return dispose();
      await waitWithDeadline(new Promise(resolve => { if (pc.iceGatheringState === 'complete') resolve(); else pc.onicegatheringstatechange = () => { if (pc.iceGatheringState === 'complete') resolve(); }; }), 15000, 'p2p gathering', controller);
      const answerResponse = await nativeFetch(manifest.p2p.offer, { method: 'POST', credentials: 'same-origin', signal: controller.signal, headers: { 'content-type': 'application/json' }, body: JSON.stringify({ type: pc.localDescription.type, sdp: pc.localDescription.sdp, device_id: manifest.device_id }) });
      const answer = await answerResponse.json();
      if (abandoned()) return dispose();
      if (!answerResponse.ok || answer.error) throw new Error(answer.error || 'p2p answer failed');
      await waitWithDeadline(pc.setRemoteDescription(answer), CONNECT_TIMEOUT_MS, 'p2p set remote description', controller);
      if (abandoned()) return dispose();
      await waitWithDeadline(new Promise((resolve, reject) => { if (channel.readyState === 'open') resolve(); else { channel.onopen = resolve; channel.onerror = reject; } }), 10000, 'p2p channel open', controller);
      if (abandoned()) return dispose();
      // Restore the guarded handler after waiting for open to catch later errors.
      channel.onerror = () => { if (alive()) scheduleReconnect(); };
      startPing();
      renderBar();
      clearTimeout(totalTimer);
    } catch (error) {
      dispose();
      throw error;
    } finally {
      // A newer attempt owns the controller slot; only our own finally may
      // clear it or flip isInitialAttempt.
      if (state.activeController === controller) {
        state.activeController = null;
        state.isInitialAttempt = false;
      }
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
    if (!state.transportStarted) return;
    const deviceId = activeDeviceId();
    if (deviceId === state.routeDeviceId) return;
    releaseCurrentAttempt(true);
    // The old device's manifest no longer applies; requests bound to its open
    // channel must fail instead of being replayed to the new device.
    state.manifest = null;
    failTransport(new Error('device switched'));
    state.closed = false;
    state.routeDeviceId = deviceId;
    runAttemptForCurrentDevice();
  }

  for (const name of ['pushState', 'replaceState']) {
    const original = history[name].bind(history);
    history[name] = (...args) => { const result = original(...args); reconnectForDevice(); return result; };
  }
  window.addEventListener('popstate', reconnectForDevice);

  async function send(message, channel = state.channel) {
    // Callers fix the channel before async reads; a device switch must not retarget the request.
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

  // Bounded request-body probing: small bodies return full bytes for P2P; oversized bodies
  // do not consume the source stream and are handed to Relay.
  // Known-size (content-length) bodies are rejected with zero reads and the original
  // Request is used for Relay; unknown-size streams are read with a single bounded reader
  // and paused at the limit so a cancelled clone/tee branch cannot hang.
  const decodeContentLength = request => {
    const header = request.headers.get('content-length');
    if (header == null || header === '') return null;
    const value = Number(header);
    return Number.isFinite(value) && value >= 0 ? value : null;
  };
  const probeBody = (request, limit) => {
    const known = decodeContentLength(request);
    if (known != null && known > limit) return { kind: 'relay' };
    if (request.body == null) return { kind: 'p2p', body: new Uint8Array() };
    const reader = request.body.getReader();
    const chunks = [];
    let total = 0;
    // Abort during probing must interrupt the pending read so a fully buffered body can still respond to abort.
    const onAbort = () => { reader.cancel(request.signal.reason).catch(() => {}); };
    request.signal.addEventListener('abort', onAbort, { once: true });
    return (async () => {
      try {
        while (true) {
          request.signal.throwIfAborted();
          const { done, value } = await reader.read();
          request.signal.throwIfAborted();
          if (done) break;
          chunks.push(value);
          total += value.length;
          if (total > limit) {
            // Over limit: pause the source reader and hand over ownership; Relay rebuilds the stream and keeps reading the remainder.
            return { kind: 'relay-stream', reader, chunks };
          }
        }
        const body = new Uint8Array(total);
        let offset = 0;
        for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.length; }
        return { kind: 'p2p', body };
      } catch (error) {
        reader.cancel(error).catch(() => {});
        throw error;
      } finally {
        request.signal.removeEventListener('abort', onAbort);
      }
    })();
  };
  // Rebuild the Relay request body by concatenating the probed prefix and the streamed remainder, without loss or duplication.
  const relayStreamBody = (request, reader, chunks) => {
    let released = false;
    let index = 0;
    let controller;
    const release = reason => {
      if (released) return;
      released = true;
      chunks.length = 0;
      request.signal.removeEventListener('abort', onAbort);
      reader.cancel(reason).catch(() => {});
    };
    const onAbort = () => { controller.error(request.signal.reason); release(request.signal.reason); };
    return new ReadableStream({
      start(value) {
        controller = value;
        request.signal.addEventListener('abort', onAbort, { once: true });
        if (request.signal.aborted) onAbort();
      },
      async pull(controller) {
        if (released) return;
        try {
          if (index < chunks.length) {
            const chunk = chunks[index];
            chunks[index++] = null;
            controller.enqueue(chunk);
            return;
          }
          const { done, value } = await reader.read();
          if (released) return;
          if (done) { controller.close(); release(); }
          else controller.enqueue(value);
        } catch (error) { controller.error(error); release(error); }
      },
      cancel(reason) { release(reason); },
    }, { highWaterMark: 0 });
  };

  async function p2pFetch(input, init = {}) {
    const channel = state.channel;
    const [scopedInput, scopedInit] = scopeNativeRequest(input, init);
    const request = new Request(typeof scopedInput === 'string' || scopedInput instanceof URL ? new URL(scopedInput, location.href) : scopedInput, scopedInit);
    request.signal.throwIfAborted();
    let { path, query } = requestPath(request);
    const requestedDevice = virtualDeviceId(path) || activeDeviceId();
    if (requestedDevice && requestedDevice !== state.manifest?.device_id) return nativeFetch(request);
    path = serverRoutePath(path) || devicePath(path);
    const id = makeId();
    // Bounded body probe: large uploads turn to Relay with zero reads (original Request kept); an unknown-size stream that exceeds the limit is rebuilt for continued reading.
    const probe = await probeBody(request, MAX_P2P_BODY);
    if (probe.kind === 'relay') return nativeFetch(request);
    if (probe.kind === 'relay-stream') {
      // Relay body = probed prefix + remaining source stream; signal propagates with the request, and abort releases the source stream.
      return nativeFetch(new Request(request, { body: relayStreamBody(request, probe.reader, probe.chunks), duplex: 'half', signal: request.signal }));
    }
    request.signal.throwIfAborted();
    const sizeProbe = probe.body;
    const headers = headersObject(request.headers);
    const accept = (headers.accept || '').toLowerCase();
    if (accept.includes('text/event-stream') || path === '/event' || path === '/global/event' || path === '/api/event' || path.endsWith('/event')) {
      const first = new Promise((resolve, reject) => {
        const entry = { resolve, reject, channel };
        entry.timer = setTimeout(() => rejectEntry(id, new Error('stream headers timeout')), 30000);
        state.pending.set(id, entry);
      });
      const stream = new ReadableStream({ start(controller) { state.streams.set(id, { controller }); }, cancel() { state.streams.delete(id); state.pending.delete(id); send({ type: 'cancel', id }, channel).catch(() => {}); } });
      request.signal.addEventListener('abort', () => { rejectEntry(id, new DOMException('Aborted', 'AbortError')); }, { once: true });
      if (request.signal && request.signal.aborted) rejectEntry(id, new DOMException('Aborted', 'AbortError'));
      try {
        const [meta] = await Promise.all([first, orderedP2PSend(async () => {
          request.signal.throwIfAborted();
          await send({ type: 'stream_request', id, method: request.method, path, query, headers, body: b64(sizeProbe) }, channel);
        })]);
        return new Response(stream, { status: meta.status, headers: meta.headers });
      }
      catch (error) { rejectEntry(id, error); throw error; }
    }
    const result = await new Promise((resolve, reject) => {
      const entry = { resolve, reject, channel };
      entry.timer = setTimeout(() => rejectEntry(id, new Error('request timeout')), 120000);
      state.pending.set(id, entry);
      orderedP2PSend(async () => {
        request.signal.throwIfAborted();
        await send({ type: 'request', id, method: request.method, path, query, headers, body: b64(sizeProbe) }, channel);
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
      this._channel = state.channel;
      // Serialize frames so text cannot overtake a queued binary frame; close frames are queued behind them too.
      this._sendQueue = Promise.resolve();
      this.id = makeId();
      this._opened = false; // ws_open was sent (the Agent has been told about this socket)
      this._done = false;   // Reached the terminal state: at most one close event, no further frames
      state.sockets.set(this.id, this);
      Promise.resolve(state.ready).then(() => {
        if (this._done || this.readyState !== MeshWebSocket.CONNECTING) return;
        if (!this._channel || this._channel.readyState !== 'open') return this.fail(new Error('P2P unavailable'));
        // Queue ws_open behind the send queue so no queued frame (including close) can overtake it,
        // and a close during CONNECTING never emits ws_open to the Agent afterwards.
        this._sendQueue = this._sendQueue.then(async () => {
          if (this._done || this.readyState !== MeshWebSocket.CONNECTING) return;
          this._opened = true;
          await send({ type: 'ws_open', id: this.id, path: serverRoutePath(url.pathname) || devicePath(url.pathname), query: url.search.slice(1), headers: {}, protocols: Array.isArray(protocols) ? protocols : (protocols ? [protocols] : []) }, this._channel);
        }).catch(error => this.fail(error));
      });
    }
    addEventListener(type, fn) { if (!this._listeners.has(type)) this._listeners.set(type, new Set()); this._listeners.get(type).add(fn); }
    removeEventListener(type, fn) { this._listeners.get(type)?.delete(fn); }
    dispatch(type, event) { this['on' + type]?.(event); for (const fn of this._listeners.get(type) || []) fn.call(this, event); }
    // Expose shared DataChannel backpressure to callers.
    get bufferedAmount() { return this._channel && this._channel.readyState === 'open' ? this._channel.bufferedAmount : 0; }
    // Single termination exit: idempotent, clears state.sockets, at most one close event.
    terminate(code, reason, error) {
      if (this._done) return;
      this._done = true;
      releaseIncomingForBusinessId(this.id);
      rememberIncomingTombstone(this.id);
      state.sockets.delete(this.id);
      this.readyState = MeshWebSocket.CLOSED;
      if (error) this.dispatch('error', error);
      this.dispatch('close', { code, reason });
    }
    fail(error) { this.terminate(1011, error.message, error); }
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
        if (this._done) return; // Terminated: drop stale queued frames, never trigger fail again
        if (typeof data === 'string') {
          await send({ type: 'ws_data', id: this.id, kind: 'text', data }, this._channel);
          return;
        }
        const bytes = await toBytes(data);
        await send({ type: 'ws_data', id: this.id, kind: 'bytes', data: b64(bytes) }, this._channel);
      }).catch(error => { this.fail(error); });
    }
    close(code = 1000, reason = '') {
      // A repeated close while CLOSING/CLOSED is a no-op (matches browser semantics).
      if (this.readyState !== MeshWebSocket.CONNECTING && this.readyState !== MeshWebSocket.OPEN) return;
      this.readyState = MeshWebSocket.CLOSING;
      if (!this._opened) {
        // ws_open was not sent: the connection never established; treat as a failed setup and
        // terminate locally (close 1006) without sending any frames to the Agent, which
        // will not bridge this id either.
        this.terminate(1006, '');
        return;
      }
      // ws_open was sent: the close frame is queued after all pending data frames (a bounded
      // DataChannel keeps order), the Agent closes the upstream and replies ws_closed to
      // complete the handshake; a failed send also terminates.
      this._sendQueue = this._sendQueue.then(async () => {
        if (this._done) return;
        await send({ type: 'ws_close', id: this.id, code, reason }, this._channel);
      }).catch(() => { this.terminate(1006, ''); });
    }
  }

  function startInitialTransport() {
    if (state.transportStarted) return state.ready;
    state.transportStarted = true;
    state.routeDeviceId = activeDeviceId();
    state.isInitialAttempt = true;
    const bootstrapGeneration = state.generation;
    state.ready = connectP2P(state.routeDeviceId)
      .then(() => { if (state.generation === bootstrapGeneration) state.reconnectDelay = 1000; })
      .catch(() => { if (state.generation !== bootstrapGeneration) return null; scheduleReconnect(); return null; });
    return state.ready;
  }
  window.__ocmTransport = state;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', renderBar, { once: true });
  else renderBar();
  scheduleDeviceStatusRefresh();
  let relayTick = 0;
  setInterval(() => { reconnectForDevice(); renderBar(); if (++relayTick % 5 === 0) measureRelayRtt(); }, 2000);
  setTimeout(measureRelayRtt, 1500);
  // Height the app box from the visible viewport instead of a CSS viewport unit.
  // The app declares h-dvh, which measures the layout viewport, and that measure
  // runs past what the user can actually see in two independent cases: an
  // installed WebAPK draws under the system navigation bar, and an open IME
  // shrinks the visible area without shrinking 100dvh. Either way the surplus
  // lands on the composer, and body{overflow:hidden} makes it unreachable rather
  // than scrollable. visualViewport.height is the only measure here that already
  // accounts for both, so it -- not dvh -- decides the box height. The stylesheet
  // keeps a calc() fallback for the window before this runs.
  function applyVisibleViewportHeight() {
    const root = document.getElementById('root');
    const bar = document.getElementById('ocm-mesh-bar');
    if (!root || !bar) return;
    const visual = window.visualViewport;
    const available = (visual ? visual.height : window.innerHeight) - bar.getBoundingClientRect().height;
    root.style.height = Math.max(0, Math.round(available)) + 'px';
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', applyVisibleViewportHeight, { once: true });
  else applyVisibleViewportHeight();
  window.addEventListener('resize', applyVisibleViewportHeight);
  window.addEventListener('orientationchange', applyVisibleViewportHeight);
  if (window.visualViewport) window.visualViewport.addEventListener('resize', applyVisibleViewportHeight);
  // The native entry module waits for discovery to finish before starting; a
  // root handoff additionally holds the transport itself until its target is
  // confirmed, so no old selected device can receive a manifest or bare API.
  window.__ocmBootstrap = { serverUrl: null, ready: null };
  if (rootHandoffDevice) {
    window.__ocmBootstrap.ready = bootstrapServers().then(() => {
      state.handoffPending = false;
      startInitialTransport();
    });
  } else {
    startInitialTransport();
    window.__ocmBootstrap.ready = bootstrapServers();
  }
  window.__ocmBootstrap.ready.catch(error => { console.error('Mesh bootstrap:', error); });

  window.fetch = async (input, init) => {
    if (state.handoffPending) {
      await window.__ocmBootstrap.ready;
      return window.fetch(input, init);
    }
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url, location.href);
    // The offline refusal is asked before the passthrough below, because a
    // foreign-device request never reaches it: a V2 page subscribes several
    // registered Servers, and each offline one keeps its own retry loop alive.
    const offlineRefusal = offlineDeviceRefusal(url, input, init);
    if (offlineRefusal) return offlineRefusal;
    if (url.origin !== location.origin || (url.pathname.startsWith('/_mesh/') && !url.pathname.startsWith('/_mesh/device/')) || (virtualDeviceId(url.pathname) && virtualDeviceId(url.pathname) !== state.manifest?.device_id)) return nativeFetch(input, init);
    // While a foreground probe verifies the old open channel, new requests go
    // to Relay; P2P is only restored after a matching pong.
    if (!state.probing && state.channel && state.channel.readyState === 'open') return p2pFetch(input, init);
    // Only the initial P2P attempt may borrow a short wait; retry attempts never
    // add latency while the transport runs on Relay.
    if (!state.probing && state.pc && state.isInitialAttempt && !state.closed && !state.reconnectTimer) {
      await Promise.race([state.ready, new Promise(resolve => setTimeout(resolve, 1200))]);
      if (!state.probing && state.channel && state.channel.readyState === 'open') return p2pFetch(input, init);
    }
    return nativeFetch(...scopeNativeRequest(input, init));
  };
  window.WebSocket = class extends MeshWebSocket {
    constructor(input, protocols) {
      const [scopedInput] = scopeNativeRequest(input);
      const url = new URL(scopedInput, location.href);
      if (url.host !== location.host || (virtualDeviceId(url.pathname) && virtualDeviceId(url.pathname) !== state.manifest?.device_id) || state.probing || !state.channel || state.channel.readyState !== 'open') {
        return new nativeWebSocket(scopedInput, protocols);
      }
      super(scopedInput, protocols);
    }
  };
  function resetRttAfterBackground() {
    state.rtt = null; state.relayRtt = null; state.pingSent = null;
    renderBar();
    // The foreground probe (see beginForegroundProbe) replaces the former
    // immediate ping: it both re-measures latency and deadlines the stale-open
    // channel, with its own timestamp slot.
    measureRelayRtt();
  }
  // A network hint only nudges the transport; it never tears down a channel
  // that is already open.
  window.addEventListener('online', onNetworkHint);
  if (navigator.connection && typeof navigator.connection.addEventListener === 'function') {
    navigator.connection.addEventListener('change', onNetworkHint);
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      // Hiding again cancels an in-flight probe without executing its expiry
      // (timers may be frozen while hidden); the next visible transition starts
      // a fresh full window.
      clearProbe();
      return;
    }
    resetRttAfterBackground();
    beginForegroundProbe();
    onForegroundResume();
  });
  window.addEventListener('pageshow', event => {
    if (!event.persisted) return;
    resetRttAfterBackground();
    beginForegroundProbe();
    onForegroundResume();
  });

  window.addEventListener('beforeunload', () => { if (state.pc) state.pc.close(); });
})();
</script>
"""
