"""Browser reconnect behaviour driven by network hints and foreground resume.

These tests run the real TRANSPORT_ADAPTER inside Node with controllable fake
timers and mocked browser APIs. They never touch a real network: fetch, the
WebRTC exchange and the channel are scripted doubles that mirror the shapes the
adapter reads.

Each test names the production change it protects:

- a network hint (window online / navigator.connection change) cancels the
  pending backoff wait and tries immediately, but only once per storm
  (debounce + cooldown);
- a hint cancels and rebuilds an old negotiation in ANY not-yet-open stage
  (initial or retry, ICE gathering or waiting for the channel) immediately,
  without waiting for an old escalated backoff;
- an already-open P2P channel is never torn down by a hint;
- an aborted stale chain (its catch/finally/channel events) cannot schedule a
  backoff, reset the backoff value, clear the fresh attempt's controller or
  flip isInitialAttempt;
- foreground resume and bfcache restore re-enter through the same debounced,
  cooldown-controlled hint path (stale only) and overlap into a single flight;
- a hint-started or scheduled retry never borrows the initial 1.2s fetch wait;
  only the page-bootstrap attempt may (even when it replaced the initial one);
- a kick fails requests still bound to a channel that died without its close
  event being delivered, so pending/stream work cannot hang;
- missing NetworkInformation only disables the connection listener, not online;
- retry-phase fetches go straight to Relay without the initial 1.2s window;
- after a device switch, events from the old device cannot reconnect;
- the total deadline still interrupts a pending local wait, including a
  createOffer that never settles.
"""

import subprocess

from src.static_adapter import TRANSPORT_ADAPTER

ADAPTER_JS = (
    TRANSPORT_ADAPTER.split('<script id="ocm-transport-adapter">', 1)[1]
    .split('</script>', 1)[0]
    .replace('__OCM_VERSION_JSON__', '"test"')
)

HARNESS_BASE_JS = r"""
const assert = require('node:assert/strict');
// Controllable fake timers: advance(ms) runs due timers in order, re-inserts intervals.
let __now = 0, __seq = 0;
const __timers = [];
global.setTimeout = (fn, ms = 0) => { const t = { id: ++__seq, fn, at: __now + ms, period: null }; __timers.push(t); return t.id; };
global.clearTimeout = id => { const i = __timers.findIndex(t => t.id === id); if (i >= 0) __timers.splice(i, 1); };
global.setInterval = (fn, ms) => { const t = { id: ++__seq, fn, at: __now + ms, period: ms }; __timers.push(t); return t.id; };
global.clearInterval = global.clearTimeout;
function advance(ms) {
  const target = __now + ms;
  for (;;) {
    __timers.sort((a, b) => a.at - b.at);
    const t = __timers[0];
    if (!t || t.at > target) break;
    __now = t.at;
    __timers.splice(0, 1);
    t.fn();
    if (t.period !== null) { t.at = __now + t.period; __timers.push(t); }
  }
  __now = target;
}
const tick = () => new Promise(r => setImmediate(r));
// Keep wall-clock reads consistent with the fake timer clock so cooldown and
// staleness windows are deterministic in tests.
global.Date = class extends global.Date {
  static now() { return __now; }
};
global.window = global;
const winListeners = {};
global.addEventListener = (type, fn) => { (winListeners[type] = winListeners[type] || new Set()).add(fn); };
const docListeners = {};
global.document = {
  readyState: 'loading', hidden: false,
  addEventListener: (type, fn) => { (docListeners[type] = docListeners[type] || new Set()).add(fn); },
  getElementById: () => null, createElement: () => ({}), head: null, body: null,
};
global.history = { pushState() {}, replaceState() {} };
const storage = new Map();
global.localStorage = { getItem: k => (storage.has(k) ? storage.get(k) : null), setItem: (k, v) => storage.set(k, v) };
global.WebSocket = class {};
const connListeners = {};
const manifestFetches = [];
const hangFetches = [];
global.fetchCalls = [];
global.MANIFEST_BEHAVIOR = 'defer';
// Stall flags park an attempt at a specific stage: ICE_HANG keeps the peer
// gathering forever, OFFER_HANG makes createOffer never settle.
global.ICE_HANG = false;
global.OFFER_HANG = false;
// nativeFetch: scripted by URL role. Manifest and offer exchanges are deferred
// until settled or aborted so tests can park an attempt at any stage.
global.fetch = (input, init = {}) => {
  const url = typeof input === 'string' ? input : String(input.url || input);
  const rec = { url, init, signal: init.signal || null, aborted: false };
  global.fetchCalls.push(rec);
  if (init.signal) init.signal.addEventListener('abort', () => { rec.aborted = true; });
  if (url.includes('/_mesh/transport-manifest')) {
    const p = new Promise((resolve, reject) => {
      if (init.signal) init.signal.addEventListener('abort', () => { rec.aborted = true; reject(new DOMException('Aborted', 'AbortError')); });
      rec.resolve = resolve; rec.reject = reject;
    });
    manifestFetches.push(rec);
    const behavior = global.MANIFEST_BEHAVIOR;
    if (behavior === 'fail') rec.resolve({ ok: false, status: 503, json: async () => ({}) });
    if (behavior === 'p2p-disabled') rec.resolve({ ok: true, json: async () => ({ device_id: 'device-a', p2p: { enabled: false }, stun_servers: [] }) });
    if (behavior === 'ok') rec.resolve({ ok: true, json: async () => ({ device_id: 'device-a', p2p: { enabled: true, offer: '/_mesh/offers/' + manifestFetches.length }, stun_servers: [] }) });
    if (behavior === 'ok-hang') rec.resolve({ ok: true, json: async () => ({ device_id: 'device-a', p2p: { enabled: true, offer: '/_mesh/offers-hang/' + manifestFetches.length }, stun_servers: [] }) });
    return p;
  }
  if (url.includes('/_mesh/devices')) return new Promise(() => {}); // bootstrap discovery stays pending
  if (url.includes('/_mesh/offers-hang/')) {
    hangFetches.push(rec);
    return new Promise((resolve, reject) => {
      if (init.signal) init.signal.addEventListener('abort', () => { rec.aborted = true; reject(new DOMException('Aborted', 'AbortError')); });
    });
  }
  return Promise.resolve({ ok: true, status: 200, json: async () => ({}) }); // relay / api info
};
const createdChannels = [];
function makeChannel() {
  const c = {
    readyState: 'connecting', bufferedAmount: 0, sent: [],
    send: f => c.sent.push(f),
    forceOpen() { c.readyState = 'open'; if (c.onopen) c.onopen(); },
    forceClose() { c.readyState = 'closed'; if (c.onclose) c.onclose(); },
    forceError() { c.readyState = 'closed'; if (c.onerror) c.onerror(); },
    forceSilentClose() { c.readyState = 'closed'; },
  };
  createdChannels.push(c);
  return c;
}
class FakePC {
  constructor() { this.iceGatheringState = global.ICE_HANG ? 'gathering' : 'complete'; this.channel = makeChannel(); this.closed = false; }
  createDataChannel() { return this.channel; }
  createOffer() { if (global.OFFER_HANG) return new Promise(() => {}); return Promise.resolve({ type: 'offer', sdp: 'x' }); }
  setLocalDescription(d) { this.localDescription = d; return Promise.resolve(); }
  setRemoteDescription(d) { this.remoteDescription = d; return Promise.resolve(); }
  close() { if (this.closed) return; this.closed = true; this.channel.forceClose(); }
}
const pcs = [];
global.RTCPeerConnection = class extends FakePC { constructor(o) { super(o); pcs.push(this); } };
global.location = { origin: 'https://mesh.test', host: 'mesh.test', href: 'https://mesh.test/', pathname: '/' };
const fireOnline = () => [...(winListeners.online || [])].forEach(f => f());
const fireConnChange = () => [...(connListeners.change || [])].forEach(f => f());
const fireForeground = () => [...(docListeners.visibilitychange || [])].forEach(f => f());
const firePageshow = event => [...(winListeners.pageshow || [])].forEach(f => f(event || { persisted: true }));
const lastChannel = () => createdChannels[createdChannels.length - 1];
"""

HARNESS_WITH_CONN_JS = HARNESS_BASE_JS + r"""
Object.defineProperty(global, 'navigator', {
  value: { connection: { addEventListener: (type, fn) => { (connListeners[type] = connListeners[type] || new Set()).add(fn); } } },
  configurable: true, writable: true,
});
"""

HARNESS_WITHOUT_CONN_JS = HARNESS_BASE_JS + r"""
Object.defineProperty(global, 'navigator', { value: {}, configurable: true, writable: true });
"""

FINISHER = r"""
let completed = false;
process.on('beforeExit', () => assert.ok(completed, 'async assertions did not complete'));
(async () => {
"""


def run_adapter(harness, preamble, body, timeout=15):
    script = harness + '\n' + preamble + '\n' + ADAPTER_JS + '\n' + body
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr


def test_network_hint_cancels_backoff_and_tries_early():
    """An online hint during the backoff wait cancels it and attempts immediately."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await s.ready.catch(() => {});
  await tick();
  assert.equal(manifestFetches.length, 1, 'initial attempt fetched the manifest');
  assert.ok(s.reconnectTimer, 'failed initial attempt scheduled a backoff wait');
  global.MANIFEST_BEHAVIOR = 'defer';
  advance(100);
  assert.equal(manifestFetches.length, 1, 'backoff wait has not fired yet');
  fireOnline();
  advance(299);
  assert.equal(manifestFetches.length, 1, 'debounce window holds the hint');
  advance(1);
  await tick();
  assert.equal(manifestFetches.length, 2, 'online hint fires an early attempt');
  assert.equal(s.reconnectTimer, null, 'backoff wait was cancelled for the early attempt');
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_adapter(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'p2p-disabled';", body)


def test_network_storm_is_debounced_and_cooldown_limits_kicks():
    """A storm of hits collapses to one kick; further hits within the cooldown are ignored."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await s.ready.catch(() => {});
  await tick();
  assert.equal(manifestFetches.length, 1);
  global.MANIFEST_BEHAVIOR = 'defer';
  for (let i = 0; i < 8; i++) fireOnline();
  for (let i = 0; i < 3; i++) fireConnChange();
  advance(300); await tick();
  assert.equal(manifestFetches.length, 2, 'one debounced kick for the whole storm');
  assert.ok(s.activeController, 'fresh attempt is in flight');
  assert.equal(pcs.length, 0, 'a manifest-parked attempt has not created a peer connection');
  fireOnline(); advance(300); await tick();
  assert.equal(manifestFetches.length, 2, 'hits inside the cooldown are suppressed');
  advance(5000); await tick();
  fireOnline(); advance(300); await tick();
  assert.equal(manifestFetches.length, 3, 'after the cooldown a hint kicks again');
  assert.equal(manifestFetches[1].aborted, true, 'the stale negotiation was aborted');
  assert.equal(s.generation, 1, 'the aborted negotiation got exactly one generation bump');
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_adapter(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'p2p-disabled';", body)


def test_stale_negotiation_abort_never_pollutes_new_attempt():
    """An aborted stale attempt must not schedule a backoff nor reset its value."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await s.ready.catch(() => {});
  await tick();
  global.MANIFEST_BEHAVIOR = 'defer';
  fireOnline(); advance(300); await tick();
  assert.equal(manifestFetches.length, 2, 'retry attempt in flight');
  s.reconnectDelay = 12345;
  advance(5000); await tick();
  fireOnline(); advance(300); await tick();
  assert.equal(manifestFetches.length, 3, 'hint aborts the stale negotiation and starts a new attempt');
  assert.equal(manifestFetches[1].aborted, true, 'stale negotiation aborted');
  assert.equal(s.reconnectTimer, null, 'stale rejection must not schedule a backoff');
  assert.equal(s.reconnectDelay, 12345, 'stale rejection must not reset the backoff value');
  assert.equal(s.generation, 1, 'stale attempt belongs to the previous generation');
  assert.ok(s.activeController, 'stale finally did not clear the fresh controller');
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_adapter(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'p2p-disabled';", body)


def test_open_p2p_is_not_torn_down_by_network_hint():
    """A hint rebuilds a not-yet-open negotiation; an open channel is left alone."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  fireOnline(); advance(300); await tick(); await tick();
  assert.equal(manifestFetches.length, 2, 'a hint during the initial negotiation rebuilds it');
  assert.equal(manifestFetches[0].aborted, true, 'the initial negotiation was invalidated');
  assert.equal(s.generation, 1, 'the stalled initial negotiation was invalidated once');
  lastChannel().forceOpen();
  await tick(); await tick();
  assert.equal(s.channel.readyState, 'open', 'P2P channel is open');
  advance(6000);
  const fetchesBefore = manifestFetches.length;
  const pcsBefore = pcs.length;
  fireOnline(); fireConnChange(); advance(300); await tick(); await tick();
  assert.equal(manifestFetches.length, fetchesBefore, 'open P2P is not renegotiated');
  assert.equal(pcs.length, pcsBefore, 'no extra peer connection was created');
  assert.equal(s.channel.readyState, 'open', 'open channel untouched');
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_adapter(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'ok';", body)


def test_foreground_resume_within_threshold_does_not_retry():
    """A recent foreground resume only refreshes probes; the backoff plan stays."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await s.ready.catch(() => {});
  await tick();
  assert.equal(manifestFetches.length, 1);
  fireForeground(); await tick(); await tick();
  assert.equal(manifestFetches.length, 1, 'foreground resume inside the threshold keeps the backoff wait');
  assert.ok(s.reconnectTimer, 'backoff wait still scheduled');
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_adapter(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'p2p-disabled';", body)


def test_foreground_resume_retries_after_stale_relay():
    """A stale relay (last attempt older than the threshold) retries on foreground."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await s.ready.catch(() => {});
  await tick();
  assert.ok(s.reconnectTimer, 'backoff wait scheduled');
  global.MANIFEST_BEHAVIOR = 'defer';
  s.lastAttemptTime = Date.now() - 16000;
  fireForeground(); advance(300); await tick(); await tick();
  assert.equal(manifestFetches.length, 2, 'stale foreground resume tries immediately');
  assert.equal(s.reconnectTimer, null, 'backoff wait replaced by the foreground attempt');
  assert.ok(s.activeController, 'foreground attempt is in flight');
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_adapter(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'p2p-disabled';", body)


def test_missing_network_information_still_allows_online_hint():
    """Without navigator.connection the adapter still reacts to window online."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  assert.equal(typeof navigator.connection, 'undefined', 'no NetworkInformation in this environment');
  await s.ready.catch(() => {});
  await tick();
  assert.equal(manifestFetches.length, 1);
  global.MANIFEST_BEHAVIOR = 'defer';
  fireOnline(); advance(300); await tick();
  assert.equal(manifestFetches.length, 2, 'online hint works without navigator.connection');
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_adapter(HARNESS_WITHOUT_CONN_JS, "global.MANIFEST_BEHAVIOR = 'p2p-disabled';", body)


def test_background_retry_does_not_delay_relay_fetch():
    """While a retry attempt is in flight, business fetches go straight to Relay."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await s.ready.catch(() => {});
  await tick();
  global.MANIFEST_BEHAVIOR = 'ok-hang';
  fireOnline(); advance(300); await tick(); await tick();
  assert.equal(manifestFetches.length, 2, 'retry attempt reached the offer exchange');
  assert.ok(s.pc, 'retry attempt created a peer connection');
  const relayCalls = () => global.fetchCalls.filter(c => c.url.includes('/api/session/'));
  const operation = window.fetch('https://mesh.test/api/session/test/prompt', { method: 'POST', body: new Uint8Array(0) });
  await tick();
  assert.equal(relayCalls().length, 1, 'retry-phase fetch goes to Relay immediately');
  advance(1200); await tick();
  assert.equal(relayCalls().length, 1, 'no replay after the initial-wait window');
  await operation;
  assert.equal(manifestFetches.length, 2, 'the retry attempt was not disturbed');
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_adapter(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'p2p-disabled';", body)


def test_initial_attempt_keeps_short_fetch_wait():
    """Only the initial attempt may borrow the short fetch wait before Relay."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick(); await tick();
  assert.ok(s.pc, 'initial attempt created a peer connection');
  assert.ok(s.activeController, 'initial attempt still in flight');
  const relayCalls = () => global.fetchCalls.filter(c => c.url.includes('/api/session/'));
  const operation = window.fetch('https://mesh.test/api/session/test/prompt', { method: 'POST', body: new Uint8Array(0) });
  await tick();
  assert.equal(relayCalls().length, 0, 'initial attempt lets the fetch wait for P2P');
  advance(1200); await tick();
  assert.equal(relayCalls().length, 1, 'fetch falls back to Relay after the short window');
  await operation;
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_adapter(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'ok-hang';", body)


def test_old_device_events_do_not_reconnect_after_switch():
    """After a device switch, the stale attempt and its channel cannot reconnect."""
    preamble = r"""
storage.set('opencode.global.dat:server', JSON.stringify({ list: [
  { type: 'http', displayName: 'A', http: { url: 'https://mesh.test/_mesh/device/device-a' } },
  { type: 'http', displayName: 'B', http: { url: 'https://mesh.test/_mesh/device/device-b' } },
] }));
storage.set('opencode.global.dat:layout',
  JSON.stringify({ home: { selection: { server: 'https://mesh.test/_mesh/device/device-a' } } }));
global.MANIFEST_BEHAVIOR = 'ok-hang';
"""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick(); await tick(); await tick();
  assert.equal(manifestFetches.length, 1, 'device-a attempt in flight');
  assert.ok(manifestFetches[0].url.includes('device=device-a'), 'initial attempt targets device-a');
  assert.ok(hangFetches[0], 'device-a attempt parked on its offer exchange');
  storage.set('opencode.global.dat:layout',
    JSON.stringify({ home: { selection: { server: 'https://mesh.test/_mesh/device/device-b' } } }));
  global.MANIFEST_BEHAVIOR = 'ok';
  history.pushState(null, '', '/other');
  await tick(); await tick(); await tick();
  assert.equal(manifestFetches.length, 2, 'device-b attempt started');
  assert.ok(manifestFetches[1].url.includes('device=device-b'), 'fresh attempt targets device-b');
  assert.equal(hangFetches[0].aborted, true, 'stale device-a negotiation was aborted on switch');
  assert.equal(s.generation, 1, 'switch bumped the generation once');
  await tick();
  lastChannel().forceOpen();
  await tick(); await tick();
  assert.equal(s.channel.readyState, 'open', 'device-b channel open');
  const countBefore = manifestFetches.length;
  createdChannels[0].forceClose();
  createdChannels[0].forceError();
  await tick(); await tick();
  assert.equal(manifestFetches.length, countBefore, 'old device channel events do not reconnect');
  advance(40500); await tick();
  assert.equal(s.reconnectTimer, null, 'stale attempt never schedules a backoff after the switch');
  assert.equal(manifestFetches.length, countBefore, 'no late reconnect from the old device');
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_adapter(HARNESS_WITH_CONN_JS, preamble, body)


def test_total_deadline_aborts_pending_local_wait():
    """The total connect deadline interrupts a parked offer exchange and enters backoff."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick(); await tick();
  assert.equal(hangFetches.length, 1, 'attempt parked on the offer exchange');
  assert.ok(s.activeController, 'attempt still in flight');
  advance(39999);
  assert.equal(hangFetches[0].aborted, false, 'deadline has not fired yet');
  advance(1); await tick(); await tick();
  assert.equal(hangFetches[0].aborted, true, 'total deadline interrupted the pending local wait');
  assert.equal(s.activeController, null, 'attempt cleaned up');
  assert.ok(s.reconnectTimer, 'expired attempt entered backoff');
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_adapter(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'ok-hang';", body)


def test_network_hint_aborts_open_wait_and_rebuilds_immediately():
    """A hint aborts a hanging channel-open wait and renegotiates at once, without waiting for the old backoff."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await s.ready.catch(() => {});
  await tick();
  global.MANIFEST_BEHAVIOR = 'ok';
  fireOnline(); advance(300); await tick(); await tick();
  assert.equal(manifestFetches.length, 2, 'retry attempt running');
  assert.equal(createdChannels[0].readyState, 'connecting', 'attempt parked on the open wait');
  assert.ok(s.activeController, 'attempt still parked');
  s.reconnectDelay = 30000;
  advance(5000); await tick();
  fireOnline(); advance(300); await tick(); await tick();
  assert.equal(s.generation, 1, 'hint bumped the stalled generation once');
  assert.equal(createdChannels[0].readyState, 'closed', 'stalled channel disposed');
  assert.equal(manifestFetches[1].aborted, true, 'stale manifest confirmed aborted');
  assert.equal(manifestFetches.length, 3, 'the hint rebuilds immediately, ignoring the old backoff');
  assert.ok(s.activeController, 'the fresh attempt is in flight');
  assert.equal(s.reconnectTimer, null, 'no backoff while the fresh attempt runs');
  assert.equal(s.reconnectDelay, 30000, 'the aborted chain did not reset the backoff value');
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_adapter(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'p2p-disabled';", body)


def test_network_hint_rebuilds_initial_attempt_stalled_at_ice():
    """A hint during the initial ICE wait cancels it and rebuilds immediately."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick(); await tick();
  assert.equal(pcs.length, 1, 'initial attempt reached the ICE stage');
  assert.ok(s.activeController, 'initial attempt in flight');
  fireOnline(); advance(300); await tick(); await tick();
  assert.equal(manifestFetches.length, 2, 'initial attempt rebuilt right away');
  assert.equal(s.generation, 1, 'stalled initial negotiation invalidated once');
  assert.equal(pcs[0].closed, true, 'old peer connection disposed');
  assert.ok(s.activeController, 'fresh attempt in flight');
  assert.equal(s.reconnectTimer, null, 'no backoff while the fresh attempt runs');
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_adapter(HARNESS_WITH_CONN_JS, "global.ICE_HANG = true; global.MANIFEST_BEHAVIOR = 'ok';", body)


def test_network_hint_rebuilds_retry_stalled_at_ice():
    """A hint also rebuilds a retry attempt stuck at the ICE stage, ignoring an old escalated backoff."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await s.ready.catch(() => {});
  await tick();
  global.MANIFEST_BEHAVIOR = 'ok';
  global.ICE_HANG = true;
  s.reconnectDelay = 30000;
  fireOnline(); advance(300); await tick(); await tick();
  assert.equal(manifestFetches.length, 2, 'retry attempt running');
  assert.equal(pcs[0].iceGatheringState, 'gathering', 'retry attempt parked on the ICE wait');
  assert.ok(s.activeController, 'retry attempt in flight');
  advance(5000); await tick();
  fireOnline(); advance(300); await tick(); await tick();
  assert.equal(manifestFetches.length, 3, 'hint rebuilds the ICE-stalled retry immediately');
  assert.equal(s.generation, 1, 'stalled retry invalidated once');
  assert.equal(pcs[0].closed, true, 'old peer connection disposed');
  assert.ok(s.activeController, 'fresh retry in flight');
  assert.equal(s.reconnectTimer, null, 'nothing waits for the old 30s backoff');
  assert.equal(s.reconnectDelay, 30000, 'the aborted chain did not reset the backoff value');
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_adapter(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'p2p-disabled';", body)


def test_stuck_create_offer_is_cancelled_by_total_deadline():
    """The 40s total deadline also interrupts a createOffer that never settles."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick(); await tick();
  assert.ok(s.activeController, 'attempt parked inside createOffer');
  assert.equal(pcs[0].closed, false, 'peer still negotiating before the deadline');
  advance(39999);
  assert.ok(s.activeController, 'deadline has not fired yet');
  advance(1); await tick(); await tick();
  assert.equal(s.activeController, null, 'attempt cleaned up after the deadline');
  assert.equal(pcs[0].closed, true, 'peer disposed at the deadline');
  assert.ok(s.reconnectTimer, 'expired attempt entered backoff');
  assert.equal(manifestFetches.length, 1, 'no further negotiation after the deadline');
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_adapter(HARNESS_WITH_CONN_JS, "global.OFFER_HANG = true; global.MANIFEST_BEHAVIOR = 'ok';", body)


def test_stale_chain_cannot_pollute_fresh_attempt():
    """A cancelled old chain's close/catch/finally cannot touch the fresh attempt."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick(); await tick();
  assert.equal(manifestFetches.length, 1, 'initial attempt in flight');
  s.reconnectDelay = 12345;
  fireOnline(); advance(300); await tick(); await tick();
  assert.equal(manifestFetches.length, 2, 'hint replaced the initial negotiation');
  assert.ok(s.activeController, 'fresh attempt owns the controller slot');
  createdChannels[0].forceClose();
  createdChannels[0].forceError();
  await tick(); await tick();
  assert.equal(manifestFetches.length, 2, 'old channel events do not reconnect');
  assert.equal(s.reconnectTimer, null, 'old chain did not schedule a backoff');
  assert.equal(s.reconnectDelay, 12345, 'old chain did not reset the backoff value');
  assert.equal(s.generation, 1, 'old chain did not bump the generation again');
  assert.ok(s.activeController, 'stale finally did not clear the fresh controller');
  assert.equal(s.isInitialAttempt, false, 'the hint-started retry never borrows the initial wait');
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_adapter(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'ok';", body)


def test_pageshow_resume_retries_stale_relay():
    """A bfcache restore accelerates a stale relay exactly like a foreground resume."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await s.ready.catch(() => {});
  await tick();
  global.MANIFEST_BEHAVIOR = 'defer';
  s.lastAttemptTime = Date.now() - 16000;
  firePageshow({ persisted: true }); advance(300); await tick(); await tick();
  assert.equal(manifestFetches.length, 2, 'stale pageshow restore tries immediately');
  assert.equal(s.reconnectTimer, null, 'backoff wait replaced by the pageshow attempt');
  assert.ok(s.activeController, 'pageshow attempt is in flight');
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_adapter(HARNESS_WITHOUT_CONN_JS, "global.MANIFEST_BEHAVIOR = 'p2p-disabled';", body)


def test_overlapping_hints_collapse_to_single_flight():
    """A network hint, connection change and stale foreground resume overlap into one attempt."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await s.ready.catch(() => {});
  await tick();
  global.MANIFEST_BEHAVIOR = 'defer';
  s.lastAttemptTime = Date.now() - 16000;
  fireOnline(); fireForeground(); fireConnChange();
  advance(300); await tick(); await tick();
  assert.equal(manifestFetches.length, 2, 'overlapping hints collapse into one attempt');
  assert.equal(s.generation, 0, 'an idle backoff kick does not bump the generation');
  assert.ok(s.activeController, 'the single fresh attempt is in flight');
  advance(4000); await tick();
  fireForeground(); advance(300); await tick();
  assert.equal(manifestFetches.length, 2, 'foreground respects the cooldown after the hint kick');
  assert.equal(s.generation, 0, 'suppressed foreground adds no generation bump');
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_adapter(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'p2p-disabled';", body)


def test_hint_started_attempt_does_not_wait_for_relay():
    """A hint-started retry never borrows the 1.2s initial fetch wait."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick(); await tick();
  assert.ok(s.pc, 'initial attempt parked at the ICE wait');
  assert.equal(s.isInitialAttempt, true, 'page bootstrap attempt is the initial one');
  fireOnline(); advance(300); await tick(); await tick();
  assert.equal(manifestFetches.length, 2, 'hint rebuilt the initial negotiation');
  assert.equal(s.isInitialAttempt, false, 'the hint-started attempt is not initial');
  assert.ok(s.pc, 'fresh attempt parked at the ICE wait');
  const relayCalls = () => global.fetchCalls.filter(c => c.url.includes('/api/session/'));
  const operation = window.fetch('https://mesh.test/api/session/test/prompt', { method: 'POST', body: new Uint8Array(0) });
  await tick();
  assert.equal(relayCalls().length, 1, 'retry attempt fetch goes to Relay immediately');
  assert.equal(s.pending.size, 0, 'no P2P wait was attempted');
  await operation;
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_adapter(HARNESS_WITH_CONN_JS, "global.ICE_HANG = true; global.MANIFEST_BEHAVIOR = 'ok';", body)


def test_hint_kick_fails_pending_on_silently_dead_channel():
    """A kick fails requests still bound to a channel that died without a close event."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick(); await tick(); await tick();
  lastChannel().forceOpen();
  await tick(); await tick();
  assert.equal(s.channel.readyState, 'open', 'P2P channel is open');
  const operation = window.fetch('https://mesh.test/api/session/test/prompt', { method: 'POST', body: new Uint8Array(0) })
    .then(() => null, e => e.message);
  await tick();
  assert.equal(s.pending.size, 1, 'request parked on the open channel');
  lastChannel().forceSilentClose();
  await tick();
  assert.equal(s.channel.readyState, 'closed', 'channel died without a close event');
  fireOnline(); advance(300); await tick(); await tick();
  assert.equal(s.pending.size, 0, 'kick failed the pending request instead of leaking it');
  assert.equal(await operation, 'P2P disconnected; request outcome may be unknown');
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_adapter(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'ok';", body)