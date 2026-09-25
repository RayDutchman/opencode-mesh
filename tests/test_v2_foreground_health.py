"""Foreground-resume health probe for an old open P2P channel.

Runs the real TRANSPORT_ADAPTER inside Node with the shared controllable
fake-clock / scripted-network harness (test_v2_reconnect_network) plus the
Relay/Agent-reply extension (test_v2_relay_p2p_switch). No real network is
touched.

Reported field failure being protected (Android 16 WebView, same LAN): lock
screen minutes -> unlock -> every API call stuck with no error until a full
refresh; the bar kept showing P2P without latency digits. A frozen page leaves
the DataChannel reporting `open` while the Agent side is gone: nothing deadlines
the pong, `onForegroundResume` early-returns while open, and requests keep being
routed into the dead channel for the full 120s request timeout.

The approved bounded design implemented here:

- resuming from the background immediately probes the old open P2P channel with
  a ping and a 3s pong deadline, bound to that exact channel + generation;
- while the probe is pending, NEW HTTP and WebSocket requests go to Relay;
- a matching pong within the window restores P2P on the SAME channel with no
  teardown (the healthy-network-hint invariant "never tear an open channel" is
  untouched);
- no pong within 3s fails the old channel (pending/streams/sockets end with the
  documented unknown-outcome error; sent mutations are never replayed) and
  rebuilds P2P in the background;
- a late pong or a late probe timeout can never affect a newer connection;
- the periodic ping owns `state.pingSent` and can never overwrite the probe
  association (`state.probe.pingT`);
- repeated visibility/pageshow events never extend (or restart) the deadline;
- hiding again cancels the in-flight probe without executing its expiry and the
  next visible transition starts a fresh full window;
- a page that is initially visible does not probe itself (it is not a lock
  screen), and foreground resume without an open channel keeps the previous
  hint/backoff behaviour.

Pong fidelity: src/main.py p2p_send() ships control messages (pong included) as
the whole {type, t} JSON inside the envelope data fragment with an empty
message_id, so these tests use deliverPong() instead of the repo deliver()
helper (which puts type/status/headers on the envelope meta and cannot carry t).
"""

import subprocess

from test_v2_reconnect_network import ADAPTER_JS
from test_v2_reconnect_network import FINISHER
from test_v2_reconnect_network import HARNESS_WITH_CONN_JS
from test_v2_relay_p2p_switch import FINISHER_TAIL
from test_v2_relay_p2p_switch import HARNESS_EXTENSION_JS

# Harness extension for the probe: Agent-exact pong delivery, a counting native
# WebSocket (so Relay routing during verification is observable), and a way to
# make the adapter's 5s periodic ping fire inside the 3s probe window.
PROBE_EXTENSION_JS = r"""
// Agent-exact pong frame: the whole {type:'pong', t} JSON travels inside the
// envelope `data` fragment with an empty message_id, exactly like
// src/main.py p2p_send()'s control branch; this exercises the adapter's
// settle() reassembly path and its strict t === pingT comparison.
const deliverPong = (channel, t) => {
  if (!channel || typeof channel.onmessage !== 'function') return;
  const inner = JSON.stringify({ type: 'pong', t });
  const env = { message_id: '', sequence: 0, data: b64s(encB.encode(inner)), final: true };
  channel.onmessage({ data: JSON.stringify(env) });
};
// Record native WebSocket constructions: during verification new sockets must
// be constructed natively (Relay), never bridged onto the probed channel.
const nativeWsRecords = [];
global.WebSocket = class {
  constructor(url, protocols) { nativeWsRecords.push({ url, protocols }); }
};
// Locate the adapter's periodic ping interval (period 5000) so a test can make
// it fire inside the 3s probe window and prove it cannot overwrite the probe.
const pingInterval = () => __timers.find(t => t.period === 5000 && t.at > __now);
const armPingIn = ms => { const t = pingInterval(); if (t) t.at = __now + ms; return t; };
"""


def run_probe(harness, preamble, body, timeout=20):
    script = (
        harness + '\n' + HARNESS_EXTENSION_JS + '\n' + PROBE_EXTENSION_JS
        + '\n' + preamble + '\n' + ADAPTER_JS + '\n' + FINISHER + body + FINISHER_TAIL
    )
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr


def test_cancelled_probe_pong_cannot_validate_same_millisecond_replacement():
    run_probe(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'ok';", r"""
  const s = window.__ocmTransport;
  await tick(); await tick();
  lastChannel().forceOpen(); await tick(); await tick();
  fireForeground(); await tick();
  const old = s.probe.pingT;
  document.hidden = true; fireForeground();
  document.hidden = false; fireForeground(); await tick();
  deliverPong(s.channel, old); await tick();
  assert.equal(s.probing, true, 'cancelled probe must not validate its replacement');
  deliverPong(s.channel, s.probe.pingT); await tick();
  assert.equal(s.probing, false);
  assert.equal(s.rtt, 0);
""")


def test_pong_after_deadline_cannot_win_over_delayed_timeout_callback():
    run_probe(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'ok';", r"""
  const s = window.__ocmTransport;
  await tick(); await tick();
  lastChannel().forceOpen(); await tick(); await tick();
  const old = s.channel;
  fireForeground(); await tick();
  const token = s.probe.pingT;
  // Model a busy event loop delivering a frame before an overdue timer callback.
  __now += 3001;
  deliverPong(old, token); await tick(); await tick();
  assert.notEqual(s.channel, old, 'late pong cannot retain an expired channel');
  assert.equal(pcs[0].closed, true);
""")


def test_initial_wait_cannot_bypass_a_new_foreground_probe():
    run_probe(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'ok';", r"""
  const s = window.__ocmTransport;
  await tick(); await tick();
  const op = window.fetch('https://mesh.test/api/session/test/waiting');
  await tick();
  assert.equal(relayBusiness().length, 0, 'request waiting for initial negotiation');
  lastChannel().forceOpen();
  fireForeground();
  await tick(); await tick();
  assert.equal(s.probing, true);
  assert.equal(relayBusiness().length, 1, 'initial waiter must also honor the probe');
  relayRecords[0].complete('relay');
  assert.equal(await (await op).text(), 'relay');
  assert.equal(s.pending.size, 0);
""")


def test_foreground_probe_routes_new_requests_to_relay_until_pong():
    """While the foreground probe is pending, new HTTP goes to Relay, new
    WebSocket constructions go native, and a matching pong restores P2P."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick(); await tick();
  lastChannel().forceOpen();
  await tick(); await tick();
  assert.equal(s.channel.readyState, 'open', 'P2P channel open');
  assert.equal(s.probing, false, 'no probe before any background round');
  // Background -> foreground resumes the page: the open channel is probed at once.
  fireForeground();
  await tick();
  assert.equal(s.probing, true, 'foreground resume starts a channel probe');
  assert.ok(s.probe, 'probe state bound');
  assert.equal(s.probe.channel, s.channel, 'probe bound to the current channel');
  assert.equal(s.probe.generation, s.generation, 'probe bound to the current generation');
  assert.equal(sentMessages(s.channel, 'ping').length, 1, 'probe sent an immediate ping');
  // While verifying, new HTTP goes to Relay and new WebSocket goes native.
  const getOp = window.fetch('https://mesh.test/api/session/test/models', { method: 'GET' });
  await tick();
  assert.equal(relayBusiness().length, 1, 'HTTP during verification goes to Relay');
  assert.equal(s.pending.size, 0, 'no P2P request parked while verifying');
  relayRecords[0].complete('{"relay":1}');
  assert.equal(await (await getOp).text(), '{"relay":1}', 'verification-period GET settled via Relay');
  new window.WebSocket('https://mesh.test/api/ws/session', []);
  assert.equal(nativeWsRecords.length, 1, 'WebSocket during verification goes native (Relay)');
  // A matching pong within the window ends the verification without teardown.
  deliverPong(s.channel, s.probe.pingT);
  await tick();
  assert.equal(s.probing, false, 'matching pong ends the verification');
  assert.equal(s.probe, null, 'probe state released');
  assert.equal(s.channel.readyState, 'open', 'healthy channel untouched');
  const op2 = window.fetch('https://mesh.test/api/session/test/models', { method: 'GET' });
  await tick();
  assert.equal(relayBusiness().length, 1, 'post-pong GET goes back to P2P');
  const req = requestOn(s.channel, '/api/session/test/models');
  assert.ok(req, 'post-pong GET sent over P2P');
  deliver(s.channel, { type: 'response', id: req.id, status: 200, headers: { 'content-type': 'application/json' }, body: encB.encode('{"p2p":1}') });
  await tick();
  assert.equal(await (await op2).text(), '{"p2p":1}', 'post-pong GET fulfilled over P2P');
""" + FINISHER_TAIL
    run_probe(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'ok';", body)


def test_probe_pong_restores_healthy_channel_without_rebuild():
    """A prompt pong restores the SAME channel and rtt; not rebuild happens."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick(); await tick();
  lastChannel().forceOpen();
  await tick(); await tick();
  assert.equal(s.channel.readyState, 'open', 'P2P channel open');
  // Healthy baseline rtt over the periodic ping before the background round.
  advance(5000); await tick();
  const baseline = s.pingSent;
  assert.ok(baseline != null, 'periodic ping timestamp recorded');
  deliverPong(s.channel, baseline);
  await tick();
  assert.ok(s.rtt != null, 'baseline rtt established');
  const fetchesBefore = manifestFetches.length, pcsBefore = pcs.length;
  // Resume: rtt is cleared for the probe, then the probe pong restores it.
  fireForeground(); await tick();
  assert.equal(s.rtt, null, 'resume clears the stale latency display');
  assert.equal(s.probing, true, 'probe in flight');
  deliverPong(s.channel, s.probe.pingT);
  await tick();
  assert.equal(s.probing, false, 'prompt pong restores the transport');
  assert.equal(s.channel.readyState, 'open', 'healthy channel kept');
  assert.ok(s.rtt != null, 'rtt restored from the probe pong');
  assert.equal(manifestFetches.length, fetchesBefore, 'no rebuild on a healthy probe');
  assert.equal(pcs.length, pcsBefore, 'no new peer connection');
  assert.equal(s.channel, createdChannels[0], 'the original channel remains');
  const op = window.fetch('https://mesh.test/api/session/test/models', { method: 'GET' });
  await tick();
  const req = requestOn(s.channel, '/api/session/test/models');
  assert.ok(req, 'request uses the original P2P channel after the probe');
  deliver(s.channel, { type: 'response', id: req.id, status: 200, headers: { 'content-type': 'application/json' }, body: encB.encode('{"ok":1}') });
  await tick();
  assert.equal(await (await op).text(), '{"ok":1}', 'business request complete over P2P');
""" + FINISHER_TAIL
    run_probe(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'ok';", body)


def test_probe_no_pong_fails_channel_and_rebuilds_after_3s():
    """No pong inside the 3s window fails the old channel and rebuilds P2P in
    the background; the fresh channel serves new requests."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick(); await tick();
  lastChannel().forceOpen();
  await tick(); await tick();
  assert.equal(s.channel.readyState, 'open', 'P2P channel open');
  fireForeground(); await tick();
  assert.equal(s.probing, true, 'probe started');
  assert.equal(s.probe.timer != null, true, '3s deadline armed');
  assert.equal(manifestFetches.length, 1, 'nothing rebuilt yet');
  // The channel never answers the probe ping.
  advance(2999); await tick();
  assert.equal(s.probing, true, 'still verifying just before the deadline');
  assert.equal(manifestFetches.length, 1, 'deadline not extended, not fired early');
  advance(1); await tick(); await tick(); await tick();
  assert.equal(s.probing, false, 'probe expired');
  assert.equal(s.probe, null, 'probe state released after expiry');
  assert.equal(s.channel, createdChannels[1], 'transport rebound to the fresh negotiating channel');
  assert.equal(createdChannels[0].readyState, 'closed', 'stale channel disposed');
  assert.equal(manifestFetches.length, 2, 'background rebuild started');
  assert.ok(createdChannels[1], 'fresh channel created by the rebuild');
  lastChannel().forceOpen();
  await tick(); await tick();
  assert.equal(s.channel.readyState, 'open', 'rebuild opened a fresh channel');
  assert.equal(s.channel, createdChannels[1], 'rebuild channel is the new one');
  const op = window.fetch('https://mesh.test/api/session/test/models', { method: 'GET' });
  await tick();
  assert.equal(relayBusiness().length, 0, 'no Relay for a post-rebuild request');
  const req = requestOn(s.channel, '/api/session/test/models');
  assert.ok(req, 'post-rebuild request sent over P2P');
  deliver(s.channel, { type: 'response', id: req.id, status: 200, headers: { 'content-type': 'application/json' }, body: encB.encode('{"fresh":1}') });
  await tick();
  assert.equal(await (await op).text(), '{"fresh":1}', 'post-rebuild request complete');
""" + FINISHER_TAIL
    run_probe(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'ok';", body)


def test_probe_timeout_fails_pending_mutation_without_replay():
    """A mutation already sent on the old channel is failed with the documented
    unknown-outcome error when the probe expires, and is never replayed on the
    fresh channel."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick(); await tick();
  lastChannel().forceOpen();
  await tick(); await tick();
  assert.equal(s.channel.readyState, 'open', 'P2P channel open');
  // Mutation parked on the open P2P channel before the background round.
  const clickOp = window.fetch('https://mesh.test/api/session/sid/send_message', { method: 'POST', headers: { accept: 'text/event-stream' }, body: encB.encode('prompt') });
  const clickError = clickOp.then(() => null, e => e.message);
  await tick();
  const oldReq = requestOn(s.channel, '/api/session/sid/send_message');
  assert.ok(oldReq, 'mutation sent over P2P pre-probe');
  assert.equal(s.pending.size, 1, 'mutation pending pre-header');
  // Resume probes, never gets a pong: expiry fails the old channel.
  fireForeground(); await tick();
  assert.equal(s.probing, true, 'probe in flight');
  advance(3000); await tick(); await tick();
  assert.equal(await clickError, 'P2P disconnected; request outcome may be unknown', 'mutation failed with outcome unknown');
  assert.equal(s.pending.size, 0, 'pending cleared after the stale channel');
  assert.equal(s.streams.size, 0, 'streams cleared after the stale channel');
  assert.equal(relayBusiness().length, 0, 'mutation was not resent over Relay');
  // Rebuild opens a fresh channel; the old mutation is never replayed.
  assert.equal(manifestFetches.length, 2, 'rebuild started');
  lastChannel().forceOpen();
  await tick(); await tick();
  assert.equal(s.channel.readyState, 'open', 'fresh channel open');
  assert.equal(requestOn(createdChannels[1], '/api/session/sid/send_message'), null, 'old mutation not replayed on the fresh channel');
  assert.equal(sentMessages(createdChannels[1], 'stream_request').length, 0, 'no stream_request replay');
  const op2 = window.fetch('https://mesh.test/api/session/sid/send_message', { method: 'POST', headers: { accept: 'text/event-stream' }, body: encB.encode('prompt2') });
  await tick();
  const req2 = requestOn(s.channel, '/api/session/sid/send_message');
  assert.ok(req2, 'a fresh mutation goes P2P after the rebuild');
  assert.notEqual(req2.id, oldReq.id, 'fresh request id');
  deliver(s.channel, { type: 'stream_chunk', id: req2.id, status: 200, headers: { 'content-type': 'text/event-stream' } });
  const op2Res = await op2;
  assert.equal(op2Res.status, 200, 'fresh mutation streamed');
  deliver(s.channel, { type: 'stream_end', id: req2.id });
  await op2Res.text();
  assert.equal(s.streams.size, 0, 'no stranded streams');
  assert.equal(s.pending.size, 0, 'no stranded pending entries');
""" + FINISHER_TAIL
    run_probe(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'ok';", body)


def test_late_pong_after_rebuild_does_not_disturb_new_channel():
    """A stale probe pong over the old channel, or its timestamp over the new
    channel, cannot touch the fresh connection."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick(); await tick();
  lastChannel().forceOpen();
  await tick(); await tick();
  fireForeground(); await tick();
  const probeT = s.probe.pingT;
  assert.equal(s.probing, true, 'probe in flight');
  advance(3000); await tick(); await tick(); await tick();
  assert.equal(manifestFetches.length, 2, 'expiry rebuilt the channel');
  assert.equal(s.probing, false, 'probe released');
  lastChannel().forceOpen();
  await tick(); await tick();
  assert.equal(s.channel.readyState, 'open', 'fresh channel open');
  assert.equal(s.channel, createdChannels[1], 'fresh channel in use');
  // Late pong over the old channel: handler detached, no-op.
  const ch0 = createdChannels[0];
  assert.equal(ch0.onmessage, null, 'old channel handler detached');
  deliverPong(ch0, probeT);
  // The same stale timestamp echoed over the new channel: no probe association there.
  deliverPong(s.channel, probeT);
  await tick(); await tick();
  assert.equal(s.channel.readyState, 'open', 'late pong does not disturb the fresh channel');
  assert.equal(s.probing, false, 'no probe was restarted');
  assert.equal(manifestFetches.length, 2, 'no extra rebuild from the late pong');
  const op = window.fetch('https://mesh.test/api/session/test/models', { method: 'GET' });
  await tick();
  assert.ok(requestOn(s.channel, '/api/session/test/models'), 'request still goes P2P on the fresh channel');
""" + FINISHER_TAIL
    run_probe(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'ok';", body)


def test_device_switch_during_probe_cancels_probe_and_rebinds():
    """A device switch while the probe is pending cancels the verification and
    rebinds the transport to the new device; the stale probe pong is inert."""
    preamble = r"""
storage.set('opencode.global.dat:server', JSON.stringify({ list: [
  { type: 'http', displayName: 'A', http: { url: 'https://mesh.test/_mesh/device/device-a' } },
  { type: 'http', displayName: 'B', http: { url: 'https://mesh.test/_mesh/device/device-b' } },
] }));
storage.set('opencode.global.dat:layout',
  JSON.stringify({ home: { selection: { server: 'https://mesh.test/_mesh/device/device-a' } } }));
global.MANIFEST_BEHAVIOR = 'ok';
"""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick(); await tick(); await tick();
  assert.ok(manifestFetches[0].url.includes('device=device-a'), 'bootstrap targets device-a');
  lastChannel().forceOpen();
  await tick(); await tick();
  assert.equal(s.channel.readyState, 'open', 'device-a channel open');
  fireForeground(); await tick();
  assert.equal(s.probing, true, 'probe active on device-a');
  const ch0 = createdChannels[0];
  // Switch to device-b while the probe is pending.
  storage.set('opencode.global.dat:layout',
    JSON.stringify({ home: { selection: { server: 'https://mesh.test/_mesh/device/device-b' } } }));
  history.pushState(null, '', '/other');
  await tick(); await tick(); await tick();
  assert.equal(s.probe, null, 'probe cancelled by the device switch');
  assert.equal(s.probing, false, 'verification ended');
  assert.equal(manifestFetches.length, 2, 'device-b attempt started');
  assert.ok(manifestFetches[1].url.includes('device=device-b'), 'fresh attempt targets device-b');
  // A stale probe pong over the old device channel cannot touch the new attempt.
  deliverPong(ch0, Date.now());
  await tick(); await tick();
  assert.equal(manifestFetches.length, 2, 'stale pong did not disturb the switch attempt');
  lastChannel().forceOpen();
  await tick(); await tick();
  assert.equal(s.channel.readyState, 'open', 'device-b channel open');
  assert.equal(s.probing, false, 'no probe restarted by the stale pong');
""" + FINISHER_TAIL
    run_probe(HARNESS_WITH_CONN_JS, preamble, body)


def test_repeated_foreground_events_do_not_extend_probe_deadline():
    """A storm of visibility/pageshow events neither restarts nor extends the
    3s window; the deadline fires exactly once."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick(); await tick();
  lastChannel().forceOpen();
  await tick(); await tick();
  fireForeground(); await tick();
  assert.equal(s.probing, true, 'probe in flight');
  const startedAt = s.probe.pingT;
  // A storm of resume events must not extend (or restart) the 3s deadline.
  advance(500); fireForeground(); firePageshow(); fireForeground();
  advance(500); fireForeground(); firePageshow();
  advance(1000); fireForeground(); firePageshow(); fireForeground();
  advance(999); await tick();
  assert.equal(s.probing, true, 'still the same probe window near the deadline');
  assert.equal(s.probe.pingT, startedAt, 'probe association untouched by repeated events');
  assert.equal(manifestFetches.length, 1, 'no rebuild before the deadline');
  advance(1); await tick(); await tick(); await tick();
  assert.equal(s.probing, false, 'the single deadline fired once');
  assert.equal(manifestFetches.length, 2, 'exactly one rebuild from the storm');
""" + FINISHER_TAIL
    run_probe(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'ok';", body)


def test_hidden_again_cancels_probe_and_revisible_gets_fresh_window():
    """Hiding again cancels the open verification without executing its expiry;
    the next visible transition starts a fresh full verification window."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick(); await tick();
  lastChannel().forceOpen();
  await tick(); await tick();
  fireForeground(); await tick();
  assert.equal(s.probing, true, 'first probe in flight');
  const firstT = s.probe.pingT;
  // Page goes to the background again: the open verification must NOT run to
  // expiry (timers may be frozen while hidden); the channel itself is untouched.
  document.hidden = true;
  fireForeground(); await tick();
  assert.equal(s.probe, null, 'probe cancelled when hidden again');
  assert.equal(s.probing, false, 'no expiry executed while hidden');
  assert.equal(s.channel.readyState, 'open', 'channel kept open across the hide');
  assert.equal(manifestFetches.length, 1, 'nothing rebuilt while hidden');
  // Re-visible after some hidden time: a fresh full window starts.
  advance(1000);
  document.hidden = false;
  fireForeground(); await tick();
  assert.ok(s.probe, 'fresh probe started on re-visible');
  assert.notEqual(s.probe.pingT, firstT, 'fresh window uses a new timestamp');
  assert.equal(s.probing, true, 'verification in flight again');
  advance(2999); await tick();
  assert.equal(s.probing, true, 'fresh window still open just before its deadline');
  assert.equal(manifestFetches.length, 1, 'nothing rebuilt inside the fresh window');
  advance(1); await tick(); await tick(); await tick();
  assert.equal(s.probing, false, 'fresh window expired');
  assert.equal(manifestFetches.length, 2, 'exactly one rebuild from the fresh window');
""" + FINISHER_TAIL
    run_probe(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'ok';", body)


def test_periodic_ping_does_not_overwrite_probe_association():
    """The periodic ping owns state.pingSent; its pong cannot complete the probe
    and its send never overwrites the probe timestamp."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick(); await tick();
  lastChannel().forceOpen();
  await tick(); await tick();
  fireForeground(); await tick();
  assert.equal(s.probing, true, 'probe in flight');
  const probeT = s.probe.pingT;
  // Force the 5s periodic ping to fire inside the probe window.
  armPingIn(500);
  advance(500); await tick();
  const pingT = s.pingSent;
  assert.ok(pingT != null && pingT !== probeT, 'periodic ping used its own slot');
  assert.equal(s.probing, true, 'periodic ping did not end the probe');
  // Its pong answers the PERIODIC ping only: the probe stays pending.
  deliverPong(s.channel, pingT); await tick();
  assert.equal(s.probing, true, 'periodic pong cannot complete the probe');
  assert.equal(s.probe.pingT, probeT, 'probe association untouched by the periodic ping');
  // The probe's own pong completes the verification and restores rtt.
  deliverPong(s.channel, probeT); await tick();
  assert.equal(s.probing, false, 'probe completed by its own pong');
  assert.equal(s.probe, null, 'probe released');
  assert.ok(s.rtt != null, 'rtt restored');
  assert.equal(s.channel.readyState, 'open', 'channel untouched');
  assert.equal(manifestFetches.length, 1, 'no rebuild');
""" + FINISHER_TAIL
    run_probe(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'ok';", body)


def test_network_hint_does_not_probe_or_tear_down_open_channel():
    """A network hint keeps its invariant: never tear an open channel, and it
    does not start a foreground probe either."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick(); await tick();
  lastChannel().forceOpen();
  await tick(); await tick();
  assert.equal(s.channel.readyState, 'open', 'P2P channel open');
  const fetchesBefore = manifestFetches.length, pcsBefore = pcs.length;
  fireOnline(); fireConnChange();
  advance(300); await tick(); await tick();
  assert.equal(s.probe, null, 'network hints never start a probe');
  assert.equal(s.probing, false, 'no verification state from a hint');
  assert.equal(s.channel.readyState, 'open', 'open channel not torn down');
  assert.equal(manifestFetches.length, fetchesBefore, 'no rebuild from a hint on an open channel');
  assert.equal(pcs.length, pcsBefore, 'no new peer connection');
""" + FINISHER_TAIL
    run_probe(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'ok';", body)


def test_foreground_resume_without_open_channel_keeps_hint_path():
    """Without an open channel a foreground resume performs no probe and keeps
    the previous hint/backoff retry behaviour."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await s.ready.catch(() => {});
  await tick();
  assert.ok(s.reconnectTimer, 'backoff wait scheduled after bootstrap failure');
  global.MANIFEST_BEHAVIOR = 'defer';
  s.lastAttemptTime = Date.now() - 16000;
  fireForeground(); await tick();
  assert.equal(s.probe, null, 'no probe without an open channel');
  assert.equal(s.probing, false, 'no verification without an open channel');
  advance(300); await tick(); await tick();
  assert.equal(manifestFetches.length, 2, 'stale foreground resume still retries via the hint path');
  assert.ok(s.activeController, 'retry attempt in flight');
""" + FINISHER_TAIL
    run_probe(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'p2p-disabled';", body)


def test_bootstrap_visible_page_does_not_probe():
    """A page that is initially visible never probes itself: a fresh P2P
    bootstrap is not treated as a lock-screen resume."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick(); await tick();
  lastChannel().forceOpen();
  await tick(); await tick();
  assert.equal(s.channel.readyState, 'open', 'bootstrap opened P2P');
  assert.equal(s.probe, null, 'a visible bootstrap page does not probe itself');
  assert.equal(s.probing, false, 'no verification on initial load');
  const op = window.fetch('https://mesh.test/api/session/test/models', { method: 'GET' });
  await tick();
  assert.equal(relayBusiness().length, 0, 'initial visible page uses P2P directly');
  assert.ok(requestOn(s.channel, '/api/session/test/models'), 'GET sent over P2P');
""" + FINISHER_TAIL
    run_probe(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'ok';", body)


def test_probe_timeout_terminates_preexisting_websocket():
    """A WebSocket already bridged on the old channel ends with error + close
    1011 when the probe expires, clearing the sockets table."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick(); await tick();
  lastChannel().forceOpen();
  await tick(); await tick();
  assert.equal(s.channel.readyState, 'open', 'P2P channel open');
  // A WebSocket already bridged on the old channel before the background round.
  const ws = new window.WebSocket('https://mesh.test/api/ws/session', []);
  const events = [];
  ws.addEventListener('open', () => events.push('open'));
  ws.addEventListener('error', () => events.push('error'));
  ws.addEventListener('close', e => events.push('close:' + e.code));
  await tick();
  assert.equal(s.sockets.size, 1, 'socket bridged on the P2P channel');
  // Resume probes, no pong: expiry fails the channel, sockets included.
  fireForeground(); await tick();
  assert.equal(s.probing, true, 'probe in flight');
  advance(3000); await tick(); await tick();
  assert.equal(s.sockets.size, 0, 'socket released');
  assert.equal(ws.readyState, 3, 'socket closed');
  assert.deepEqual(events, ['error', 'close:1011'], 'terminated with error + close 1011');
""" + FINISHER_TAIL
    run_probe(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'ok';", body)
