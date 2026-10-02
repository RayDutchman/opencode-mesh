"""Offline device request gate in the browser adapter.

A device that the authoritative `/_mesh/devices` snapshot marks offline cannot
answer anything, so the Gateway answers 503 immediately for every method. The
upstream OpenCode V2 SDK does not stop there: it retries `/api/event` on a fixed
~1.2s cadence, which the adapter forwarded as-is (observed on the VPS: ~3018
requests per hour for one offline device).

These tests run the real TRANSPORT_ADAPTER inside Node, reusing the fake-clock /
scripted-network harness of test_v2_reconnect_network.py, and pin the intended
local behaviour:

- a device-level request for a device a fresh snapshot marks `online === false`
  is answered in the browser with a 503 whose JSON body is byte-identical to the
  Gateway's own offline response, and never touches nativeFetch or P2P. This
  covers every explicit `/_mesh/device/<id>/...` target, not only the page's own
  device: a V2 page routinely subscribes several registered Servers at once, and
  an unwatched target was measured leaving ~50 503/min on the wire;
- navigation requests are never answered locally, because the Gateway serves its
  offline recovery page for them (`Gateway.wants_html`); Accept is read from both
  `init.headers` and a `Request` input;
- recovery is unchanged, because online status is still discovered by the 5s
  `/_mesh/devices` poll (≤5s latency), and an online device is never gated;
- the snapshot has a freshness budget: a stale snapshot, a failed or hung
  discovery, a device that is absent, and a device without an explicit
  `online: false` all keep the request on the normal transport, because a wrong
  "offline" verdict would break a reachable device;
- Mesh control-plane endpoints (`/_mesh/devices`, `/_mesh/ui/...`, offers),
  cross-origin requests and `/server/...` requests are never gated;
- every method is gated, because the Gateway answers 503 for every method when a
  device is offline: the local answer is a reproduction of the server answer,
  not a second policy about which requests may travel.
"""

import json
import subprocess
from types import SimpleNamespace

from src.main import Gateway
from test_v2_reconnect_network import ADAPTER_JS
from test_v2_reconnect_network import FINISHER
from test_v2_reconnect_network import HARNESS_WITH_CONN_JS

# Discovery control for these tests: an explicit queue of snapshots followed by a
# sticky default, plus failure and hang modes that drive the real
# setDeviceStatusUnknown() invalidation path. Calls are recorded in
# global.fetchCalls exactly like the base harness does for the other URLs.
ONLINE_A = {"device_id": "device-a", "name": "Device A", "online": True, "upstream_health": "healthy", "available": True}
OFFLINE_B = {"device_id": "device-b", "name": "Device B", "online": False, "upstream_health": "unknown", "available": False}
ONLINE_B = {"device_id": "device-b", "name": "Device B", "online": True, "upstream_health": "healthy", "available": True}
OFFLINE_C = {"device_id": "device-c", "name": "Device C", "online": False, "upstream_health": "unknown", "available": False}
OFFLINE_BODY = '{"error":"Specified device offline or not found","device_id":"device-b"}'

DISCOVERY_EXTENSION_JS = r"""
const OFFLINE_BODY = __OFFLINE_BODY__;
const GATE_BASE_FETCH = global.fetch;
global.DISCOVERY_MODE = 'ok';
global.DISCOVERY_DEFAULT = { devices: [] };
global.DISCOVERY_HANG = false;
global.fetch = (input, init = {}) => {
  const url = typeof input === 'string' ? input : String(input.url || input);
  if (!url.includes('/_mesh/devices')) return GATE_BASE_FETCH(input, init);
  const rec = { url, init, signal: init.signal || null, aborted: false };
  global.fetchCalls.push(rec);
  if (init.signal) init.signal.addEventListener('abort', () => { rec.aborted = true; });
  if (global.DISCOVERY_MODE === 'fail') return Promise.reject(new Error('discovery network failure'));
  if (global.DISCOVERY_HANG) return new Promise(() => {});
  const payload = global.DISCOVERY_RESPONSES.length ? global.DISCOVERY_RESPONSES.shift() : global.DISCOVERY_DEFAULT;
  return Promise.resolve({ ok: true, status: 200, json: async () => payload });
};
const discoveryCalls = () => global.fetchCalls.filter(call => call.url.includes('/_mesh/devices'));
const eventCalls = () => global.fetchCalls.filter(call => call.url.includes('/api/event'));
const offlineEvent = () => window.fetch('https://mesh.test/_mesh/device/device-b/api/event',
  { headers: { accept: 'text/event-stream' } });
// The reported incident: the page device is online or offline independently of
// the other registered Server it subscribes in parallel.
const foreignEvent = () => window.fetch('https://mesh.test/_mesh/device/device-c/api/event',
  { headers: { accept: 'text/event-stream' } });
""".replace('__OFFLINE_BODY__', json.dumps(OFFLINE_BODY))

FINISHER_TAIL = r"""
})().then(() => { completed = true; }).catch(e => {
  completed = true;
  console.error('TIMELINE:\n' + (globalThis.__tl ? globalThis.__tl.join('\n') : '<none>'));
  console.error(e);
  process.exitCode = 1;
});
"""

# A page bound to device-b. The layout selection pins the route before bootstrap,
# so the manifest and the page own explicit device requests belong to device-b and
# take the P2P/Relay selection instead of the passthrough for other devices.
PAGE_ON_DEVICE_B = r"""
storage.set('opencode.global.dat:server', JSON.stringify({ list: [
  { type: 'http', displayName: 'Device B', http: { url: 'https://mesh.test/_mesh/device/device-b' } },
] }));
storage.set('opencode.global.dat:layout', JSON.stringify({ home: { selection: { server: 'https://mesh.test/_mesh/device/device-b' } } }));
global.location = { origin: 'https://mesh.test', host: 'mesh.test', pathname: '/', search: '', hash: '', href: 'https://mesh.test/' };
global.MANIFEST_BEHAVIOR = '__MANIFEST__';
global.DISCOVERY_DEFAULT = __DEFAULT__;
global.DISCOVERY_RESPONSES = __QUEUE__;
"""

# Production shape of the reported incident: the page is loaded while its device
# is still online, the device drops, and the 5s device-list poll notices. The
# gate arms from that poll, never from page load.
ARM_OFFLINE_SNAPSHOT = r"""
  const s = window.__ocmTransport;
  const onlineFlag = id => (s.devices.find(item => item.device_id === id) || {}).online;
  for (let i = 0; i < 10; i++) await tick();
  assert.equal(s.manifest.device_id, 'device-b', 'the page holds a manifest for its own device');
  assert.equal(s.defaultDevice, 'device-b', 'the page default is its own device');
  advance(5000);
  for (let i = 0; i < 10; i++) await tick();
  assert.equal(onlineFlag('device-a'), true, 'the poll still reports the other device online');
  assert.equal(onlineFlag('device-b'), false, 'the 5s device-list poll observed the offline device');
"""

def snapshot(*devices, default='device-b'):
    return {"configured_default_device": default, "default_device": default, "devices": list(devices)}


def build_preamble(default_payload, queue=(), manifest='p2p-disabled', page=PAGE_ON_DEVICE_B):
    return (page
            .replace('__MANIFEST__', manifest)
            .replace('__DEFAULT__', json.dumps(default_payload))
            .replace('__QUEUE__', json.dumps(list(queue))))


def online_preamble(*extra_offline):
    """Clean bootstrap: the page device is online when the page loads. The first
    queued snapshot is consumed by bootstrap itself; later polls fall back to the
    sticky default, which reports the page device offline."""
    offline = snapshot(ONLINE_A, OFFLINE_B, *extra_offline)
    return build_preamble(offline, queue=[snapshot(ONLINE_A, ONLINE_B, *extra_offline)])


def run_gate(preamble, body, timeout=30):
    script = '\n'.join([HARNESS_WITH_CONN_JS, DISCOVERY_EXTENSION_JS, preamble, ADAPTER_JS, body])
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr
    return result.stdout


def gateway_offline_json(device_id):
    """The response the Gateway itself produces for an offline explicit device."""

    class Probe:
        wants_html = staticmethod(Gateway.wants_html)
        offline_page = staticmethod(Gateway.offline_page)
        offline_response = Gateway.offline_response

    return Probe().offline_response(SimpleNamespace(headers={}), device_id, known=True)


def test_known_offline_device_request_is_answered_locally_without_any_network_call():
    """A device-level request for a known offline device is answered with a local
    503 and never leaves the browser."""
    body = FINISHER + ARM_OFFLINE_SNAPSHOT + r"""
  const before = global.fetchCalls.length;
  const response = await offlineEvent();
  assert.equal(response.status, 503, 'an offline device is refused without asking the server');
  assert.equal(response.ok, false, 'the SDK must not see a success');
  assert.equal(response.headers.get('content-type'), 'application/json', 'the refusal stays a JSON body, never a stream');
  assert.equal(await response.text(), OFFLINE_BODY, 'the local body matches the Gateway offline body');
  assert.equal(global.fetchCalls.length, before, 'the retry loop request never reached native fetch');
  assert.equal(s.pending.size, 0, 'no transport entry was allocated');
  assert.equal(s.streams.size, 0, 'no stream was opened');
""" + FINISHER_TAIL
    run_gate(online_preamble(), body)


def test_offline_device_recovers_through_the_normal_transport_after_a_fresh_snapshot():
    """Recovery latency is unchanged: the next 5s device-list poll restores
    traffic for the same request shape."""
    body = FINISHER + ARM_OFFLINE_SNAPSHOT + r"""
  assert.equal((await offlineEvent()).status, 503, 'precondition: the offline device is gated');
  assert.equal(eventCalls().length, 0, 'a gated request never reached native fetch');
  global.DISCOVERY_RESPONSES.push(__RECOVERED__);
  const polled = discoveryCalls().length;
  advance(5000);
  for (let i = 0; i < 10; i++) await tick();
  assert.equal(discoveryCalls().length, polled + 1, 'the next device-list poll arrives on the 5s schedule');
  assert.equal(s.devices[1].online, true, 'the refreshed snapshot reports the device online again');
  const response = await offlineEvent();
  assert.equal(response.status, 200, 'a recovered device is served over the normal transport');
  assert.equal(eventCalls().length, 1, 'exactly one request left the browser this time');
  assert.equal(eventCalls()[0].url, 'https://mesh.test/_mesh/device/device-b/api/event',
    'the request keeps its explicit device target');
""" + FINISHER_TAIL
    run_gate(build_preamble(snapshot(ONLINE_A, OFFLINE_B),
                            queue=[snapshot(ONLINE_A, ONLINE_B), snapshot(ONLINE_A, OFFLINE_B), snapshot(ONLINE_A, ONLINE_B)]),
             body.replace('__RECOVERED__', json.dumps(snapshot(ONLINE_A, ONLINE_B))))


def test_online_device_requests_are_never_gated():
    """Reverse guard: while the page device is reachable, both explicit and bare
    requests keep the exact pre-existing behaviour."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  for (let i = 0; i < 10; i++) await tick();
  assert.equal(s.defaultDevice, 'device-b');
  assert.equal((await offlineEvent()).status, 200, 'an online device is not refused');
  assert.deepEqual(eventCalls().map(call => call.url), ['https://mesh.test/_mesh/device/device-b/api/event'],
    'the explicit device request reached native fetch unchanged');
  const bare = await window.fetch('https://mesh.test/api/session/ses/models');
  assert.equal(bare.status, 200, 'a bare path is still answered by the transport');
  assert.ok(global.fetchCalls.some(call => call.url === 'https://mesh.test/_mesh/device/device-b/api/session/ses/models'),
    'a bare path still binds to the default device');
""" + FINISHER_TAIL
    run_gate(build_preamble(snapshot(ONLINE_A, ONLINE_B), queue=[]), body)


def test_stale_snapshot_never_gates_requests():
    """Reverse guard for the freshness budget: while the 5s poller is starved (a
    backgrounded tab gets its timers throttled), an old snapshot must not refuse
    a device that may have come back."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  for (let i = 0; i < 10; i++) await tick();
  assert.equal(s.manifest.device_id, 'device-b');
  // Only the 5000ms device-status scheduler is swallowed; every other adapter
  // timer keeps running, so manifest deadlines and reconnect backoff behave
  // normally while no further device-list poll arrives.
  const realSetTimeout = global.setTimeout;
  global.setTimeout = (fn, ms) => (ms === 5000 ? 0 : realSetTimeout(fn, ms));
  advance(25000);
  for (let i = 0; i < 10; i++) await tick();
  assert.equal(discoveryCalls().length, 2, 'bootstrap plus exactly one device-list poll');
  assert.equal(s.devices[1].online, false, 'the last snapshot still says offline');
  assert.equal((await offlineEvent()).status, 503, 'precondition: the fresh snapshot gates');
  advance(25000);
  for (let i = 0; i < 10; i++) await tick();
  assert.equal(discoveryCalls().length, 2, 'no refresh arrived while the poller was starved');
  const response = await offlineEvent();
  assert.equal(response.status, 200, 'a stale snapshot must not answer locally');
  assert.equal(eventCalls().length, 1, 'the request went to the server instead');
""" + FINISHER_TAIL
    run_gate(build_preamble(snapshot(ONLINE_A, OFFLINE_B), queue=[snapshot(ONLINE_A, ONLINE_B)]), body)


def test_failed_discovery_invalidates_the_snapshot():
    """Reverse guard: a failed `/_mesh/devices` poll invalidates the snapshot
    instead of freezing an offline verdict, and a later success restores it."""
    body = FINISHER + ARM_OFFLINE_SNAPSHOT + r"""
  assert.equal((await offlineEvent()).status, 503, 'precondition: the fresh snapshot gates');
  global.DISCOVERY_MODE = 'fail';
  const polled = discoveryCalls().length;
  advance(5000);
  for (let i = 0; i < 10; i++) await tick();
  assert.equal(discoveryCalls().length, polled + 1, 'the failing poll was actually attempted');
  assert.equal(s.deviceSnapshotAt, null, 'a failed poll invalidated the snapshot');
  assert.equal(s.devices[1].online, false, 'the last known status is still shown');
  assert.equal((await offlineEvent()).status, 200, 'a failed poll must not refuse requests locally');
  assert.equal(eventCalls().length, 1, 'the request went to the server instead');
  global.DISCOVERY_MODE = 'ok';
  advance(5000);
  for (let i = 0; i < 10; i++) await tick();
  assert.equal((await offlineEvent()).status, 503, 'a successful poll restores the gate');
  assert.equal(eventCalls().length, 1, 'the gate is armed again without any request');
""" + FINISHER_TAIL
    run_gate(online_preamble(), body)


def test_hung_discovery_does_not_gate_requests():
    """A discovery that never settles fails its own 10s deadline and invalidates
    the snapshot instead of silently freezing the previous verdict."""
    body = FINISHER + ARM_OFFLINE_SNAPSHOT + r"""
  assert.equal((await offlineEvent()).status, 503, 'precondition: the fresh snapshot gates');
  global.DISCOVERY_HANG = true;
  const polled = discoveryCalls().length;
  advance(5000);
  for (let i = 0; i < 5; i++) await tick();
  assert.equal(discoveryCalls().length, polled + 1, 'the hung poll is in flight');
  advance(10000);
  for (let i = 0; i < 10; i++) await tick();
  assert.equal(s.deviceSnapshotAt, null, 'the deadline invalidated the snapshot');
  assert.equal((await offlineEvent()).status, 200, 'a hung poll must not refuse requests locally');
  assert.equal(eventCalls().length, 1, 'the request went to the server instead');
""" + FINISHER_TAIL
    run_gate(online_preamble(), body)


def test_device_absent_from_the_snapshot_is_not_gated():
    """A device id the snapshot does not describe is unknown, not offline."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  for (let i = 0; i < 10; i++) await tick();
  assert.equal(s.defaultDevice, 'device-b', 'the page default is its own device');
  advance(5000);
  for (let i = 0; i < 10; i++) await tick();
  assert.deepEqual(s.devices.map(d => d.device_id), ['device-a'], 'the snapshot no longer describes device-b');
  assert.equal(s.devices[0].online, true);
  assert.equal((await offlineEvent()).status, 200, 'an undescribed device keeps using the transport');
  assert.deepEqual(eventCalls().map(call => call.url), ['https://mesh.test/_mesh/device/device-b/api/event'],
    'the explicit device request reached native fetch');
  const bare = await window.fetch('https://mesh.test/api/session/ses/models');
  assert.equal(bare.status, 200, 'an undescribed default device keeps using the transport');
  assert.ok(global.fetchCalls.some(call => call.url === 'https://mesh.test/_mesh/device/device-b/api/session/ses/models'),
    'a bare path still binds to the default device');
""" + FINISHER_TAIL
    run_gate(build_preamble(snapshot(ONLINE_A, default='device-a'), queue=[snapshot(ONLINE_A, ONLINE_B)]), body)


def test_device_without_an_explicit_offline_flag_is_not_gated():
    """Only `online === false` is a verdict; a missing flag means unknown."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  for (let i = 0; i < 10; i++) await tick();
  assert.equal(s.devices[0].online, undefined, 'the snapshot omits the online flag');
  assert.equal((await offlineEvent()).status, 200, 'a missing online flag must not be read as offline');
  assert.equal(eventCalls().length, 1, 'the request went to the server instead');
""" + FINISHER_TAIL
    run_gate(build_preamble(snapshot({"device_id": "device-b", "name": "Device B", "upstream_health": "unknown"})), body)


def test_control_plane_cross_origin_and_native_server_requests_are_never_gated():
    """Mesh control endpoints, native server routes and cross-origin traffic keep
    their existing path even while the page device and another device are both
    marked offline. Only explicit device paths are the gate's business."""
    body = FINISHER + ARM_OFFLINE_SNAPSHOT + r"""
  const polled = discoveryCalls().length;
  const discovery = await window.fetch('/_mesh/devices', { credentials: 'same-origin', cache: 'no-store' });
  assert.equal(discovery.status, 200, 'device discovery is never refused locally');
  assert.equal(discoveryCalls().length, polled + 1, 'the discovery request really reached the network');
  const asset = await window.fetch('/_mesh/ui/2/device-b/_assets/app.css');
  assert.equal(asset.status, 200, 'the versioned UI namespace is never refused locally');
  assert.ok(global.fetchCalls.some(call => call.url === '/_mesh/ui/2/device-b/_assets/app.css'));
  const offer = await window.fetch('/_mesh/offers/p2p-1', { method: 'POST', body: new Uint8Array(0) });
  assert.equal(offer.status, 200, 'a P2P signalling POST is never refused locally');
  const cross = await window.fetch('https://external.test/api/info');
  assert.equal(cross.status, 200, 'a cross-origin request is untouched');
  assert.ok(global.fetchCalls.some(call => call.url === 'https://external.test/api/info'),
    'a cross-origin request keeps its address and leaves through the native transport');
  const route = await window.fetch('https://mesh.test/server/encoded/api/event');
  assert.equal(route.status, 200, 'a native server route is not resolved to a device here');
  assert.ok(global.fetchCalls.some(call => call.url === 'https://mesh.test/server/encoded/api/event'),
    'a native server route keeps its own address');
  const online = await window.fetch('https://mesh.test/_mesh/device/device-a/api/event',
    { headers: { accept: 'text/event-stream' } });
  assert.equal(online.status, 200, 'a reachable device keeps the existing native passthrough');
  const foreign = await foreignEvent();
  assert.equal(foreign.status, 503, 'another offline device is refused locally too');
  assert.equal(await foreign.text(), OFFLINE_BODY.replace('device-b', 'device-c'),
    'the local body names the device that was requested');
  const gated = await offlineEvent();
  assert.equal(gated.status, 503, 'the page device request is still gated');
  assert.deepEqual(eventCalls().map(call => call.url), ['https://mesh.test/server/encoded/api/event',
    'https://mesh.test/_mesh/device/device-a/api/event'],
    'only the offline device requests were gated; every other path left through the transport');
""" + FINISHER_TAIL
    run_gate(online_preamble(OFFLINE_C), body)


def test_offline_foreign_device_request_is_answered_locally_without_any_network_call():
    """Regression for the deployed scope defect: the gate used to sit behind the
    foreign-device passthrough, so a page that also subscribes another registered
    Server kept answering its retry loop from the Gateway (~50 503/min measured
    on the VPS) instead of refusing it locally."""
    body = FINISHER + ARM_OFFLINE_SNAPSHOT + r"""
  assert.equal(s.manifest.device_id, 'device-b', 'precondition: the page is bound to device-b');
  assert.equal(onlineFlag('device-c'), false, 'precondition: the same snapshot reports device-c offline');
  const before = global.fetchCalls.length;
  const response = await foreignEvent();
  assert.equal(response.status, 503, 'a request for another offline device is refused locally');
  assert.equal(response.ok, false, 'the SDK must not see a success');
  assert.equal(response.headers.get('content-type'), 'application/json', 'the refusal stays a JSON body');
  assert.equal(await response.text(), OFFLINE_BODY.replace('device-b', 'device-c'),
    'the local body carries the requested device id');
  assert.equal(global.fetchCalls.length, before, 'the watched device retry loop never reached native fetch');
""" + FINISHER_TAIL
    run_gate(online_preamble(OFFLINE_C), body)


def test_html_navigation_to_an_offline_device_is_never_gated():
    """A navigation is not a retry loop: the Gateway answers Accept: text/html
    with its offline recovery page, so the local refusal must stay out of the
    way. Accept arrives in init.headers here, the form the SDK-less callers and
    location-driven loads use."""
    body = FINISHER + ARM_OFFLINE_SNAPSHOT + r"""
  const before = global.fetchCalls.length;
  const own = await window.fetch('https://mesh.test/',
    { headers: { accept: 'text/html,application/xhtml+xml' } });
  assert.equal(own.status, 200, 'a navigation to the offline page device must reach the Gateway recovery page');
  const watched = await window.fetch('https://mesh.test/_mesh/device/device-c/session/ses_abc',
    { headers: { accept: 'text/html' } });
  assert.equal(watched.status, 200, 'a navigation to another offline device must reach the Gateway recovery page');
  assert.equal(global.fetchCalls.length, before + 2, 'both navigations left through the native transport');
  assert.ok(global.fetchCalls.some(call => call.url.includes('/_mesh/device/device-c/session/ses_abc')),
    'the watched navigation kept its explicit device address');
""" + FINISHER_TAIL
    run_gate(online_preamble(OFFLINE_C), body)


def test_request_object_html_navigation_to_an_offline_device_is_never_gated():
    """The same navigation exemption with the Accept header carried by a Request
    object instead of init. A Request for the same device without an HTML Accept
    is still refused, so the exemption cannot leak into API traffic."""
    body = FINISHER + ARM_OFFLINE_SNAPSHOT + r"""
  const navigation = new Request('https://mesh.test/_mesh/device/device-c/session/ses_abc',
    { headers: { accept: 'text/html' } });
  assert.equal((await window.fetch(navigation)).status, 200,
    'a Request carrying Accept: text/html reaches the Gateway recovery page');
  const api = new Request('https://mesh.test/_mesh/device/device-c/api/event',
    { headers: { accept: 'text/event-stream' } });
  const refused = await window.fetch(api);
  assert.equal(refused.status, 503, 'a Request without an HTML Accept is still refused locally');
  assert.deepEqual(eventCalls().map(call => call.url), [],
    'only the navigation left the browser');
""" + FINISHER_TAIL
    run_gate(online_preamble(OFFLINE_C), body)


def test_every_method_is_answered_locally_and_nothing_is_emitted():
    """Method rule: the Gateway answers 503 for any method when a device is
    offline, so the local reproduction covers reads and mutations alike. Nothing
    is emitted, which is what keeps the "never replay an emitted mutation" rule
    intact: the gate only answers requests that were never issued anywhere."""
    body = FINISHER + ARM_OFFLINE_SNAPSHOT + r"""
  const before = global.fetchCalls.length;
  const attempts = [
    ['GET', await window.fetch('https://mesh.test/_mesh/device/device-b/api/session/ses/models')],
    ['GET (SSE)', await offlineEvent()],
    ['POST (SSE)', await window.fetch('https://mesh.test/_mesh/device/device-b/api/session/ses/prompt',
      { method: 'POST', headers: { accept: 'text/event-stream' }, body: new Uint8Array(0) })],
    ['POST', await window.fetch('https://mesh.test/_mesh/device/device-b/api/session/ses/abort',
      { method: 'POST', body: new Uint8Array(0) })],
    ['DELETE', await window.fetch('https://mesh.test/_mesh/device/device-b/api/session/ses', { method: 'DELETE' })],
  ];
  for (const [method, response] of attempts) {
    assert.equal(response.status, 503, method + ' is refused with the offline status');
    assert.equal(response.ok, false, method + ' is not a success');
    assert.equal(response.headers.get('content-type'), 'application/json', method + ' keeps the JSON body');
    assert.equal(await response.text(), OFFLINE_BODY, method + ' keeps the Gateway body');
  }
  assert.equal(global.fetchCalls.length, before, 'no attempt of any method reached native fetch');
  assert.equal(s.pending.size, 0, 'a locally refused request occupies no transport slot');
  assert.equal(s.streams.size, 0, 'a locally refused mutation opens no stream');
  assert.equal(s.sockets.size, 0, 'a locally refused request opens no socket');
""" + FINISHER_TAIL
    run_gate(online_preamble(), body)


def test_local_response_is_identical_to_the_gateway_offline_response():
    """The synthesised 503 is byte-identical to the Gateway's own offline JSON,
    so the SDK cannot distinguish a local refusal from the server's answer."""
    body = FINISHER + ARM_OFFLINE_SNAPSHOT + r"""
  const response = await offlineEvent();
  console.log('OBSERVED ' + JSON.stringify({
    status: response.status,
    contentType: response.headers.get('content-type'),
    body: await response.text(),
  }));
""" + FINISHER_TAIL
    stdout = run_gate(online_preamble(), body)
    observed = json.loads(next(line for line in stdout.splitlines() if line.startswith('OBSERVED '))[len('OBSERVED '):])
    gateway = gateway_offline_json('device-b')
    assert observed['status'] == gateway.status_code == 503
    assert observed['contentType'] == gateway.headers['content-type'] == 'application/json'
    assert observed['body'] == gateway.body.decode()