"""Offline status page and device-online criterion behavior.

The online criterion must be identical everywhere: ``/_mesh/devices`` reports a
device online only while its control connection is fresh (``ws`` set and
``last_seen`` within 45s; a missing ``last_seen`` stays compatible), which is
the same rule the Gateway uses for routing and P2P gating.

The Node tests run the real OFFLINE_PAGE script with a scripted fetch and a
tiny DOM shim. They pin the offline page contract:

- the current target device is named on the page via textContent only (no HTML
  injection), even for unknown/verbatim device ids;
- when other devices are online the page never claims "No device is online";
- every online device has a manual switch link to the path-type device entry
  ``/_mesh/device/{id}``;
- a reload happens only when the original target recovers (never an auto
  switch/replay for the null-target page);
- a failed poll clears previous dots instead of keeping a stale green light as
  a live result, shows an unknown status, and the next successful poll recovers;
- a poll that never settles is aborted after 10 seconds (AbortController), the
  status is expressed as unknown, the poll guard is released so the next round
  retries, and a response that arrives after the deadline is discarded instead
  of overwriting the unknown state;
- the browser WebSocket gate to an explicit device uses the same freshness
  criterion as the list endpoint: a stale control connection is rejected with
  4403 without touching the in-flight Agent connection.
"""
import asyncio
import json
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from src import main as mesh_main
from src.main import Gateway, OFFLINE_PAGE


def _gateway(state_file: Path) -> Gateway:
    return Gateway({'state_file': str(state_file),
                    'auth': {'username': 'test', 'password': 'test-pass'}})


# ---------- ASGI behavior tests ----------

def test_devices_endpoint_reports_stale_fresh_and_legacy_with_isolated_state():
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            gateway = _gateway(Path(directory) / 'registry.json')
            now = time.time()
            gateway.registry.devices = {
                'stale-a': {'device_id': 'stale-a', 'name': 'Stale A', 'ws': object(), 'last_seen': now - 120},
                'fresh-b': {'device_id': 'fresh-b', 'name': 'Fresh B', 'ws': object(), 'last_seen': now},
                'legacy-c': {'device_id': 'legacy-c', 'name': 'Legacy C', 'ws': object()},
                'plain-d': {'device_id': 'plain-d', 'name': 'Plain D'},
            }
            transport = httpx.ASGITransport(app=gateway.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test',
                                         auth=('test', 'test-pass')) as client:
                payload = (await client.get('/_mesh/devices')).json()
                by_id = {d['device_id']: d for d in payload['devices']}
                assert by_id['stale-a']['online'] is False
                assert by_id['fresh-b']['online'] is True
                assert by_id['legacy-c']['online'] is True, 'missing last_seen stays compatible'
                assert by_id['plain-d']['online'] is False
                assert payload['default_device'] == 'fresh-b'
                # The list endpoint and the routing criterion must be the same implementation.
                for device_id, row in by_id.items():
                    assert row['online'] is mesh_main.device_online(gateway.registry.devices[device_id])
                assert gateway.is_online(gateway.registry.devices['legacy-c']) is True
                assert gateway.is_online(gateway.registry.devices['stale-a']) is False
    asyncio.run(scenario())


def test_offline_device_html_names_target_and_keeps_no_store():
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            gateway = _gateway(Path(directory) / 'registry.json')
            now = time.time()
            gateway.registry.devices = {
                'stale-a': {'device_id': 'stale-a', 'name': 'Stale A', 'ws': object(), 'last_seen': now - 120},
                'fresh-b': {'device_id': 'fresh-b', 'name': 'Fresh B', 'ws': object(), 'last_seen': now},
            }
            transport = httpx.ASGITransport(app=gateway.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test',
                                         auth=('test', 'test-pass')) as client:
                html = await client.get('/_mesh/device/stale-a', headers={'accept': 'text/html'})
                assert html.status_code == 200
                assert html.headers['cache-control'] == 'no-store'
                assert '__TARGET__' not in html.text
                assert json.dumps('stale-a') in html.text, 'the page knows which device it stands for'
                # API clients keep a JSON offline answer with a stable 503 for a known device.
                api = await client.get('/_mesh/device/stale-a')
                assert api.status_code == 503
                assert api.json()['error'].startswith('Specified device offline')
                assert api.json()['device_id'] == 'stale-a'
    asyncio.run(scenario())


def test_transport_manifest_and_p2p_offer_use_the_freshness_criterion():
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            gateway = _gateway(Path(directory) / 'registry.json')
            now = time.time()
            gateway.registry.devices = {
                'stale-a': {'device_id': 'stale-a', 'name': 'Stale A', 'ws': object(), 'last_seen': now - 120},
                'fresh-b': {'device_id': 'fresh-b', 'name': 'Fresh B', 'ws': object(), 'last_seen': now},
            }
            transport = httpx.ASGITransport(app=gateway.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test',
                                         auth=('test', 'test-pass')) as client:
                stale = (await client.get('/_mesh/transport-manifest', params={'device': 'stale-a'})).json()
                fresh = (await client.get('/_mesh/transport-manifest', params={'device': 'fresh-b'})).json()
                assert stale['device_id'] == 'stale-a' and stale['p2p']['enabled'] is False
                assert fresh['device_id'] == 'fresh-b' and fresh['p2p']['enabled'] is True
                assert fresh['server_url'] == 'http://test/_mesh/device/fresh-b'
                # A stale device is refused before any signaling is forwarded; no close logic is added.
                refused = await client.post('/_mesh/p2p/offer', json={'device_id': 'stale-a'})
                assert refused.status_code == 503
                assert refused.json()['error'] == 'Device offline'
                unknown = await client.post('/_mesh/p2p/offer', json={'device_id': 'missing'})
                assert unknown.status_code == 503
    asyncio.run(scenario())


# ---------- browser WebSocket gate uses the same freshness criterion ----------
#
# A browser link to an explicit device must use exactly the criterion the list
# endpoint reports, so a stale control connection is rejected instead of being
# presented as a live target. The rejection must not touch the in-flight Agent
# connection (unlike the HTTP proxy path, which frees the route by closing it).

class _RecorderAgentWS:
    """Stand-in Agent control connection that records sent frames and closes."""

    def __init__(self):
        self.sent: list[dict] = []
        self.close_codes: list[int] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def close(self, code: int = 1000) -> None:
        self.close_codes.append(code)


async def _drive_browser_ws(gateway: Gateway, path: str) -> list[dict]:
    """Drive one browser WebSocket handshake through the ASGI app directly.

    httpx ASGITransport cannot speak the websocket ASGI protocol, so the test
    plays the ASGI server channel: the app first receives ``websocket.connect``,
    then replies with ``websocket.accept``/``websocket.close`` messages that the
    test observes as if they were the wire.
    """
    sent: list[dict] = []
    receives = {'n': 0}

    async def receive():
        receives['n'] += 1
        if receives['n'] == 1:
            return {'type': 'websocket.connect'}
        if receives['n'] == 2:
            return {'type': 'websocket.disconnect', 'code': 1000}
        await asyncio.sleep(3600)

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        'type': 'websocket', 'asgi': {'version': '3.0'},
        'path': path, 'raw_path': path.encode(), 'root_path': '',
        'query_string': b'', 'subprotocols': [],
        'headers': [(b'authorization', b'Basic dGVzdDp0ZXN0LXBhc3M=')],
        'client': ('127.0.0.1', 12345), 'server': ('test', 80),
    }

    await asyncio.wait_for(gateway.app(scope, receive, send), timeout=5)
    return sent


def test_browser_ws_stale_explicit_device_rejected_4403_without_touching_inflight():
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            gateway = _gateway(Path(directory) / 'registry.json')
            now = time.time()
            inflight = _RecorderAgentWS()
            gateway.registry.devices = {
                'stale-a': {'device_id': 'stale-a', 'name': 'Stale A', 'ws': inflight, 'last_seen': now - 120},
                'fresh-b': {'device_id': 'fresh-b', 'name': 'Fresh B', 'ws': _RecorderAgentWS(), 'last_seen': now},
            }
            sent = await _drive_browser_ws(gateway, '/_mesh/device/stale-a')
            assert sent == [{'type': 'websocket.close', 'code': 4403, 'reason': ''}], sent
            # The in-flight Agent connection is left alone: same object, no
            # close, no forwarded frames, and no bridge/owner state was created.
            assert gateway.registry.devices['stale-a']['ws'] is inflight
            assert inflight.close_codes == []
            assert inflight.sent == []
            assert gateway.browser_ws == {}
            assert gateway.owners == {}
    asyncio.run(scenario())


def test_browser_ws_fresh_explicit_device_is_bridged():
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            gateway = _gateway(Path(directory) / 'registry.json')
            now = time.time()
            agent = _RecorderAgentWS()
            gateway.registry.devices = {
                'fresh-b': {'device_id': 'fresh-b', 'name': 'Fresh B', 'ws': agent, 'last_seen': now},
            }
            sent = await _drive_browser_ws(gateway, '/_mesh/device/fresh-b')
            assert any(m.get('type') == 'websocket.accept' for m in sent), sent
            assert not any(m.get('type') == 'websocket.close' and m.get('code') == 4403 for m in sent)
            assert agent.sent, 'ws_open control frame must reach the fresh Agent connection'
    asyncio.run(scenario())


def test_browser_ws_unknown_explicit_device_keeps_4403():
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            gateway = _gateway(Path(directory) / 'registry.json')
            now = time.time()
            gateway.registry.devices = {
                'fresh-b': {'device_id': 'fresh-b', 'name': 'Fresh B', 'ws': _RecorderAgentWS(), 'last_seen': now},
            }
            sent = await _drive_browser_ws(gateway, '/_mesh/device/not-registered')
            assert sent == [{'type': 'websocket.close', 'code': 4403, 'reason': ''}], sent
            assert gateway.registry.devices['fresh-b']['ws'] is not None, 'other devices untouched'
    asyncio.run(scenario())


# ---------- Real OFFLINE_PAGE script behavior in Node ----------

OFFLINE_JS = OFFLINE_PAGE.split('<script>', 1)[1].split('</script>', 1)[0]

HARNESS_JS = r"""
const assert = require('node:assert/strict');
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
const flush = () => new Promise(r => setImmediate(r));
const nodes = {};
function makeNode(tag) {
  const el = { tagName: String(tag).toUpperCase(), className: '', children: [], _text: '' };
  Object.defineProperty(el, 'textContent', {
    get() { return this._text; },
    set(v) { this._text = String(v == null ? '' : v); if (this._text === '') this.children = []; },
  });
  el.append = (...kids) => kids.forEach(k => el.children.push(k));
  return el;
}
global.document = {
  getElementById: id => nodes[id] || (nodes[id] = makeNode('div')),
  createElement: tag => makeNode(tag),
};
const reloads = [];
global.location = { origin: 'https://mesh.test', host: 'mesh.test', reload: () => reloads.push(__now) };
const fetchCalls = [];
global.fetch = (url, init) => {
  fetchCalls.push({ url: String(url), cache: init && init.cache, signal: init && init.signal });
  const next = scripted.shift();
  if (next === 'fail') return Promise.reject(new Error('network down'));
  if (next === 'hang') return new Promise(() => {});
  if (next && next.ok === false) return Promise.resolve({ ok: false });
  if (next && typeof next.delay === 'number') {
    // A poll that settles after the 10s deadline: the response arrives late and
    // must be discarded instead of overwriting the unknown state.
    return new Promise(resolve => setTimeout(() => resolve({ ok: true, json: () => Promise.resolve(next.body) }), next.delay));
  }
  return Promise.resolve({ ok: true, json: () => Promise.resolve(next) });
};
const getMsg = () => nodes.msg.textContent;
const getRows = () => nodes.list.children;
const getCells = () => getRows().flatMap(li => li.children);
const getLinks = () => getCells().filter(el => el.tagName === 'A');
"""

FINISHER = r"""
let completed = false;
process.on('beforeExit', () => assert.ok(completed, 'async assertions did not complete'));
(async () => {
"""


def run_offline(target, responses, body):
    # The offline page's initial tick() runs at script evaluation, so the whole
    # poll sequence must be scripted before the script is evaluated.
    prefix = 'const scripted = ' + json.dumps(responses) + ';\n'
    script = (HARNESS_JS + '\n' + prefix + OFFLINE_JS.replace('__TARGET__', json.dumps(target)) + '\n' + body)
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_late_poll_completion_cannot_release_new_poll_guard():
    body = FINISHER + r"""
  await flush();
  advance(12000); await flush();
  assert.equal(fetchCalls.length, 2);
  advance(1000); await flush(); await flush();
  advance(2000); await flush();
  assert.equal(fetchCalls.length, 2, 'late old completion must not unlock the pending new poll');
  completed = true;
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
    run_offline('device-a', [{'delay': 13000, 'body': {'devices': []}}, 'hang'], body)


def test_offline_page_names_target_and_lists_online_switch_without_reload():
    body = FINISHER + r"""
  await flush();
  assert.equal(reloads.length, 0, 'target offline: no reload');
  assert.ok(getMsg().startsWith('Device "Alpha" is offline'), 'target name shown verbatim');
  assert.ok(getMsg().includes('another device is online'), 'no misleading no-device claim');
  assert.ok(!getMsg().includes('No device is online'), 'online device exists');
  assert.equal(fetchCalls[0].cache, 'no-store', 'device list polling stays no-store');
  assert.equal(getLinks().length, 1, 'only the online device is a switch entry');
  assert.equal(getLinks()[0].href, '/_mesh/device/device-b');
  assert.equal(getLinks()[0].textContent, 'Beta');
  const rowsText = getRows().map(li => li.children.map(c => c.textContent).join('|')).join('\n');
  assert.ok(rowsText.includes('Alpha') && rowsText.includes('offline'), 'offline target listed');
  assert.ok(rowsText.includes('Beta') && rowsText.includes('online'), 'online device listed');
  completed = true;
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_offline('device-a', [
        {'devices': [{'device_id': 'device-a', 'name': 'Alpha', 'online': False},
                     {'device_id': 'device-b', 'name': 'Beta', 'online': True}]},
    ], body)


def test_offline_page_reloads_only_when_target_recovers():
    body = FINISHER + r"""
  await flush();
  assert.equal(reloads.length, 0, 'no reload while the target is offline');
  advance(3000); await flush(); await flush();
  assert.equal(reloads.length, 1, 'target recovery reloads exactly once');
  completed = true;
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_offline('device-a', [
        {'devices': [{'device_id': 'device-a', 'name': 'Alpha', 'online': False},
                     {'device_id': 'device-b', 'name': 'Beta', 'online': True}]},
        {'devices': [{'device_id': 'device-a', 'name': 'Alpha', 'online': True},
                     {'device_id': 'device-b', 'name': 'Beta', 'online': True}]},
    ], body)


def test_offline_page_without_target_lists_online_device_but_never_reloads():
    body = FINISHER + r"""
  await flush();
  assert.equal(getMsg(), 'No device is online. This page refreshes automatically.');
  assert.equal(reloads.length, 0);
  advance(3000); await flush(); await flush();
  assert.equal(getMsg(), 'Choose an online device below to continue.', 'online device must not be hidden');
  assert.ok(!getMsg().includes('No device is online'), 'no misleading claim while one device is online');
  assert.equal(reloads.length, 0, 'no target: never auto-reload/auto-switch');
  assert.equal(getLinks().length, 1);
  assert.equal(getLinks()[0].href, '/_mesh/device/device-b');
  completed = true;
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_offline(None, [
        {'devices': [{'device_id': 'device-a', 'name': 'Alpha', 'online': False},
                     {'device_id': 'device-b', 'name': 'Beta', 'online': False}]},
        {'devices': [{'device_id': 'device-a', 'name': 'Alpha', 'online': False},
                     {'device_id': 'device-b', 'name': 'Beta', 'online': True}]},
    ], body)


def test_offline_page_names_are_safe_text_for_known_and_unknown_devices():
    malicious = '<img src=x onerror="globalThis.__xss=true">'
    body = FINISHER + r"""
  await flush();
  assert.ok(getMsg().includes('__XSS_MARKER__'), 'hostile name stays literal text');
  assert.equal(globalThis.__xss, undefined, 'no HTML was executed');
  assert.ok(!getCells().some(el => el.tagName === 'IMG'), 'no injected element node');
  assert.equal(getLinks()[0].textContent, 'Beta & <b>bold</b>', 'name is text, not markup');
  completed = true;
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
""".replace('__XSS_MARKER__', malicious)
    run_offline('device-a', [
        {'devices': [{'device_id': 'device-a', 'name': malicious, 'online': False},
                     {'device_id': 'device-b', 'name': 'Beta & <b>bold</b>', 'online': True}]},
    ], body)
    # An unknown target id is shown verbatim and safely.
    body = FINISHER + r"""
  await flush();
  assert.ok(getMsg().startsWith('Device "unknown-dev" is offline'));
  assert.equal(globalThis.__xss, undefined);
  assert.equal(getLinks().length, 1, 'unknown target does not hide the online entry point');
  completed = true;
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_offline('unknown-dev', [
        {'devices': [{'device_id': 'device-b', 'name': 'Beta', 'online': True}]},
    ], body)


def test_offline_page_poll_failure_expresses_unknown_and_recovers():
    body = FINISHER + r"""
  await flush();
  assert.equal(getLinks().length, 1, 'fresh poll shows the online green entry');
  advance(3000); await flush(); await flush();
  assert.equal(getMsg(), 'Cannot reach the device list; status is unknown. This page keeps retrying automatically.');
  assert.ok(!getCells().some(el => String(el.className).includes(' on')), 'stale green dot must not persist');
  assert.equal(getRows()[0].children[0].textContent, 'Status unknown — retrying');
  assert.equal(reloads.length, 0, 'unknown state must not fake a target recovery');
  advance(3000); await flush(); await flush();
  assert.equal(getMsg(), 'Cannot reach the device list; status is unknown. This page keeps retrying automatically.');
  advance(3000); await flush(); await flush();
  assert.ok(getMsg().startsWith('Device "Alpha" is offline'), 'a successful poll recovers the live state');
  assert.equal(getLinks().length, 1, 'recovered online entry point is back');
  assert.ok(getCells().some(el => String(el.className).includes(' on')), 'green dot restored on recovery');
  completed = true;
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_offline('device-a', [
        {'devices': [{'device_id': 'device-a', 'name': 'Alpha', 'online': False},
                     {'device_id': 'device-b', 'name': 'Beta', 'online': True}]},
        'fail',
        {'ok': False},
        {'devices': [{'device_id': 'device-a', 'name': 'Alpha', 'online': False},
                     {'device_id': 'device-b', 'name': 'Beta', 'online': True}]},
    ], body)


def test_offline_page_aborts_a_hung_poll_and_recovers_on_the_next_round():
    body = FINISHER + r"""
  await flush();
  assert.equal(fetchCalls[0].cache, 'no-store', 'hung poll stays no-store');
  advance(10000); await flush(); await flush(); await flush();
  assert.equal(fetchCalls[0].signal.aborted, true, 'the 10s deadline aborts the hung request');
  assert.equal(getMsg(), 'Cannot reach the device list; status is unknown. This page keeps retrying automatically.');
  assert.equal(getRows()[0].children[0].textContent, 'Status unknown — retrying');
  assert.ok(!getCells().some(el => String(el.className).includes(' on')), 'no dots kept from a hung poll');
  assert.equal(reloads.length, 0, 'unknown state is not a fake recovery');
  assert.equal(fetchCalls.length, 1, 'the guard blocked retries while the poll hung');
  advance(2000); await flush(); await flush(); await flush();
  assert.equal(fetchCalls.length, 2, 'after the abort the next round retries');
  assert.ok(getMsg().startsWith('Device "Alpha" is offline'), 'the next round after the abort recovers');
  assert.equal(getLinks().length, 1);
  assert.ok(getCells().some(el => String(el.className).includes(' on')), 'fresh data restores the green dot');
  assert.equal(reloads.length, 0);
  completed = true;
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_offline('device-a', [
        'hang',
        {'devices': [{'device_id': 'device-a', 'name': 'Alpha', 'online': False},
                     {'device_id': 'device-b', 'name': 'Beta', 'online': True}]},
    ], body)


def test_offline_page_discards_a_stale_late_response_after_the_deadline():
    body = FINISHER + r"""
  await flush();
  advance(10000); await flush(); await flush(); await flush();
  assert.equal(getMsg(), 'Cannot reach the device list; status is unknown. This page keeps retrying automatically.');
  assert.equal(fetchCalls[0].signal.aborted, true, 'the deadline aborts the slow request');
  assert.equal(reloads.length, 0);
  advance(2000); await flush(); await flush(); await flush();
  assert.equal(getMsg(), 'Cannot reach the device list; status is unknown. This page keeps retrying automatically.');
  assert.ok(!getMsg().includes('BETA-STALE'), 'the late response must not overwrite the unknown state');
  assert.ok(!getCells().some(el => el.textContent === 'BETA-STALE'), 'the stale row must not render');
  assert.ok(!getCells().some(el => String(el.className).includes(' on')), 'no stale green dot');
  assert.equal(reloads.length, 0);
  // The second poll expires at 22s; the next scheduled poll starts at 24s.
  advance(12000); await flush(); await flush(); await flush();
  assert.ok(getMsg().startsWith('Device "Alpha" is offline'), 'a fresh poll recovers after the discard');
  assert.ok(getCells().some(el => el.textContent === 'BETA-FRESH'), 'the fresh data rendered');
  assert.ok(!getCells().some(el => el.textContent === 'BETA-STALE'), 'the stale body never rendered');
  assert.ok(getCells().some(el => String(el.className).includes(' on')), 'only fresh data carries the green dot');
  assert.equal(reloads.length, 0);
  completed = true;
})().then(() => { completed = true; }).catch(e => { completed = true; console.error(e); process.exitCode = 1; });
"""
    run_offline('device-a', [
        {'delay': 12000, 'body': {'devices': [{'device_id': 'device-a', 'name': 'Alpha', 'online': False},
                                              {'device_id': 'device-b', 'name': 'BETA-STALE', 'online': True}]}},
        'hang',
        {'devices': [{'device_id': 'device-a', 'name': 'Alpha', 'online': False},
                     {'device_id': 'device-b', 'name': 'BETA-FRESH', 'online': True}]},
    ], body)
