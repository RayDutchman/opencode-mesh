"""Relay->P2P switch behaviour around in-flight business requests.

These tests run the real TRANSPORT_ADAPTER from src/static_adapter.py inside Node,
reusing the controllable fake-clock / scripted-network harness of
test_v2_reconnect_network.py verbatim and extending it with:

- scripted Relay responses whose headers arrive immediately while the response
  body stays open until a test releases or fails it (a plain GET or an SSE
  click stream with "headers arrived, body pending");
- simulated Agent reply frames delivered over the opened DataChannel, shaped
  exactly like src/main.py `p2p_send` builds them (type/id/status/headers kept
  on the envelope frame, raw reply bytes in `data`; stream header frames carry
  an empty data payload, which the SSE parser ignores).

No real network is touched: fetch, WebRTC and the DataChannel are scripted
doubles. Every test records its native fetch calls, request ids, simulated
timestamps and channel switches in a timeline printed on completion (and on
failure through stderr), and asserts the recorded sequence.

Scenario under investigation (reported: buttons fail during the Relay->P2P
switch on browsers and APK alike, any device): a Relay request is in flight with
its headers delivered and its body still pending when P2P successfully opens.
The facts we verify against the actual shared adapter are:

- the old request finishes on the channel it started on (the native Relay
  fetch) and is never replayed over the new P2P DataChannel;
- subsequent requests select P2P once the channel is open;
- an old Relay request failing or being aborted after P2P opened does not
  disturb the new channel or its pending entries;
- when P2P setup fails, new requests use Relay; when an established channel
  closes, already-sent work fails without replay and new requests use Relay.
"""

import subprocess

from test_v2_reconnect_network import ADAPTER_JS
from test_v2_reconnect_network import FINISHER
from test_v2_reconnect_network import HARNESS_WITH_CONN_JS

# Harness extension: streams Relay business responses and lets tests deliver
# Agent replies over the DataChannel. Wraps the base scripted fetch so manifest,
# offer and device URLs keep their existing behaviours.
HARNESS_EXTENSION_JS = r"""
// --- Relay business responses: headers arrive now, body stays open ---
const encB = new TextEncoder();
const relayRecords = [];
function mkRelay(url, init) {
  const record = {
    url, init, aborted: false, bodyOpen: true, controller: null,
    started: Date.now(), didError: false,
  };
  if (init.signal) init.signal.addEventListener('abort', () => {
    record.aborted = true;
    record.fail(new DOMException('Aborted', 'AbortError'));
  }, { once: true });
  const body = new ReadableStream({
    start(c) { record.controller = c; },
    cancel() { record.bodyOpen = false; },
  });
  const response = new Response(body, { status: 200, headers: { 'content-type': 'application/json' } });
  record.complete = data => {
    if (!record.bodyOpen) return false;
    record.bodyOpen = false;
    try { record.controller.enqueue(typeof data === 'string' ? encB.encode(data) : data); record.controller.close(); }
    catch (_) { return false; }
    return true;
  };
  record.fail = err => {
    if (!record.bodyOpen) return;
    record.bodyOpen = false;
    record.didError = true;
    try { record.controller.error(err); } catch (_) {}
  };
  relayRecords.push(record);
  return response;
}
const baseFetch = global.fetch;
global.fetch = (input, init = {}) => {
  const url = typeof input === 'string' ? input : String(input.url || input);
  if (url.includes('/api/session/')) {
    global.fetchCalls.push({ url, init, signal: init.signal || null, aborted: false, kind: 'relay-business' });
    const response = mkRelay(url, init);
    if (!global.DEFER_RELAY_HEADERS) return response;
    const record = relayRecords[relayRecords.length - 1];
    return new Promise((resolve, reject) => {
      record.releaseHeaders = () => resolve(response);
      if (init.signal) init.signal.addEventListener('abort', () => reject(init.signal.reason), { once: true });
    });
  }
  return baseFetch(input, init);
};
// --- Agent reply shims over the DataChannel (mirror src/main.py p2p_send) ---
const b64s = bytes => { let s = ''; for (const b of bytes) s += String.fromCharCode(b); return Buffer.from(s, 'binary').toString('base64'); };
const unb64s = text => Uint8Array.from(Buffer.from(text, 'base64'));
const seqCount = new Map();
function deliver(channel, inner) {
  // inner: { type, id, status?, headers?, body? } with body a Uint8Array of the raw reply bytes.
  const env = { message_id: inner.id, type: inner.type, id: inner.id };
  if (inner.status !== undefined) env.status = inner.status;
  if (inner.headers !== undefined) env.headers = inner.headers;
  const seq = seqCount.get(inner.id) || 0;
  seqCount.set(inner.id, seq + 1);
  env.sequence = seq;
  env.data = b64s(inner.body || new Uint8Array(0));
  env.final = true;
  channel.onmessage({ data: JSON.stringify(env) });
}
function sentMessages(channel, type) {
  const out = [];
  for (const raw of channel.sent) {
    let env;
    try { env = JSON.parse(raw); } catch (_) { continue; }
    if (!env || !env.data) continue;
    let inner = null;
    try { inner = JSON.parse(new TextDecoder().decode(unb64s(env.data))); } catch (_) {}
    if (inner && inner.type === type) out.push(inner);
    else if (env.type === type) out.push(env);
  }
  return out;
}
function requestOn(channel, path) {
  return sentMessages(channel, 'request').concat(sentMessages(channel, 'stream_request')).find(m => m.path === path) || null;
}
const relayBusiness = () => global.fetchCalls.filter(c => c.kind === 'relay-business');
"""

# Standard finisher: close the async IIFE. The timeline is exposed on globalThis
# because the IIFE body and the rejection handler are different scopes.
FINISHER_TAIL = r"""
})().then(() => { completed = true; }).catch(e => {
  completed = true;
  console.error('TIMELINE:\n' + (globalThis.__tl ? globalThis.__tl.join('\n') : '<none>'));
  console.error(e);
  process.exitCode = 1;
});
"""


def run_switch(harness, preamble, body, timeout=20):
    script = harness + '\n' + HARNESS_EXTENSION_JS + '\n' + preamble + '\n' + ADAPTER_JS + '\n' + body
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr


def test_relay_pending_during_p2p_open_completes_on_original_channel():
    """A Relay GET/POST with headers arrived and body pending finishes on the
    native Relay fetch even when P2P opens mid-body; the next request goes P2P
    and the Relay mutation is never replayed over the DataChannel."""
    preamble = "global.MANIFEST_BEHAVIOR = 'defer';"
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  const timeline = [];
  globalThis.__tl = timeline;
  const mark = ev => timeline.push('t=' + String(Date.now()).padStart(6, '0') + ' ' + ev);
  await tick();
  mark('bootstrap parked on manifest');
  global.MANIFEST_BEHAVIOR = 'ok';
  fireOnline(); advance(300); await tick(); await tick();
  mark('hint-started switch attempt running, channel connecting');
  assert.equal(manifestFetches.length, 2, 'second attempt after the hint');
  assert.equal(createdChannels[0].readyState, 'connecting', 'attempt parked on the open wait');
  assert.equal(s.isInitialAttempt, false, 'hint-started attempt never borrows the initial wait');
  // Relay GET in flight: headers arrive now, body stays open.
  const getOp = window.fetch('https://mesh.test/api/session/test/models', { method: 'GET' });
  await tick();
  const getRes = await getOp;
  mark('relay GET headers arrived; body pending');
  assert.equal(relayBusiness().length, 1, 'relay GET went through the native fetch');
  assert.equal(relayRecords[0].bodyOpen, true, 'relay GET body still pending');
  // Relay mutation (button click shape) also in flight before P2P opens.
  const postOp = window.fetch('https://mesh.test/api/session/test/prompt', { method: 'POST', headers: { accept: 'text/event-stream' }, body: new Uint8Array(0) });
  await tick();
  const postRes = await postOp;
  mark('relay POST headers arrived; body pending');
  assert.equal(relayBusiness().length, 2, 'relay POST also on the native fetch');
  assert.equal(relayRecords[1].bodyOpen, true, 'relay POST body still pending');
  // P2P opens while both Relay bodies are pending.
  lastChannel().forceOpen();
  await tick(); await tick();
  mark('P2P channel open');
  assert.equal(s.channel.readyState, 'open', 'P2P open during pending Relay bodies');
  assert.equal(relayRecords[0].bodyOpen, true, 'old Relay GET untouched by the P2P open');
  assert.equal(relayRecords[1].bodyOpen, true, 'old Relay POST untouched by the P2P open');
  assert.equal(relayRecords[0].aborted, false, 'no abort observed on the old Relay GET');
  // A brand-new request while the old Relay bodies are still pending goes P2P.
  const nextOp = window.fetch('https://mesh.test/api/session/test/models', { method: 'GET' });
  await tick();
  mark('new GET dispatched after open');
  assert.equal(relayBusiness().length, 2, 'new request did not re-enter Relay');
  const p2pGet = requestOn(s.channel, '/api/session/test/models');
  assert.ok(p2pGet, 'new GET sent over P2P');
  assert.equal(p2pGet.method, 'GET');
  assert.equal(requestOn(s.channel, '/api/session/test/prompt'), null, 'Relay mutation not replayed on P2P');
  assert.equal(sentMessages(s.channel, 'stream_request').length, 0, 'no stream_request replay of the mutation');
  assert.equal(sentMessages(s.channel, 'request').length, 1, 'exactly one P2P request, no duplicate sends');
  // Old requests still complete on their original Relay channel.
  relayRecords[0].complete('{"hello":1}');
  assert.equal(await getRes.text(), '{"hello":1}', 'old GET finished via Relay');
  relayRecords[1].complete('data: {"mutation":true}\n\n');
  assert.equal(await postRes.text(), 'data: {"mutation":true}\n\n', 'old mutation finished via Relay');
  mark('both Relay bodies completed on the original channel');
  // New P2P GET completes via the simulated Agent reply.
  assert.ok(p2pGet.id, 'P2P request carries an id');
  deliver(s.channel, { type: 'response', id: p2pGet.id, status: 200, headers: { 'content-type': 'application/json' }, body: encB.encode('{"p2p":2}') });
  await tick();
  const p2pRes = await nextOp;
  assert.equal(await p2pRes.text(), '{"p2p":2}', 'new request fulfilled over P2P');
  mark('new GET fulfilled over P2P');
  assert.equal(s.pending.size, 0, 'no stranded pending entries');
  assert.equal(relayBusiness().length, 2, 'Relay used exactly twice (GET+POST), never replayed');
  console.log('TIMELINE\n' + timeline.join('\n'));
""" + FINISHER_TAIL
    run_switch(HARNESS_WITH_CONN_JS, preamble, body)


def test_relay_sse_click_stream_survives_p2p_open_and_next_click_goes_p2p():
    """A pre-open SSE click stream (the button/prompt shape) keeps streaming to
    completion on Relay across the P2P open; the next click goes P2P and streams
    via the DataChannel end to end."""
    preamble = "global.MANIFEST_BEHAVIOR = 'defer';"
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  const timeline = [];
  globalThis.__tl = timeline;
  const mark = ev => timeline.push('t=' + String(Date.now()).padStart(6, '0') + ' ' + ev);
  await tick();
  global.MANIFEST_BEHAVIOR = 'ok';
  fireOnline(); advance(300); await tick(); await tick();
  // Button click BEFORE P2P opens: prompt POST with SSE accept -> Relay stream.
  const clickOp = window.fetch('https://mesh.test/api/session/sid/send_message', { method: 'POST', headers: { accept: 'text/event-stream' }, body: encB.encode('prompt') });
  await tick();
  const clickRes = await clickOp;
  const clickReader = clickRes.body.getReader();
  mark('relay click stream: headers arrived, body pending');
  assert.equal(relayBusiness().length, 1, 'pre-open click went to Relay');
  relayRecords[0].controller.enqueue(encB.encode('data: {"message":{"content":"one"}}\n\n'));
  const firstRead = await clickReader.read();
  assert.equal(firstRead.done, false, 'relay event streamed before the P2P open');
  assert.equal(new TextDecoder().decode(firstRead.value).includes('"one"'), true);
  // P2P opens mid-stream.
  lastChannel().forceOpen();
  await tick(); await tick();
  mark('P2P open while the Relay click stream is still streaming');
  assert.equal(s.channel.readyState, 'open', 'P2P open while Relay stream in flight');
  assert.equal(relayRecords[0].bodyOpen, true, 'Relay stream alive across the switch');
  // The Relay stream keeps producing events and ends normally on its original channel.
  relayRecords[0].controller.enqueue(encB.encode('data: {"done":true}\n\n'));
  relayRecords[0].controller.close();
  relayRecords[0].bodyOpen = false;
  const rest = [];
  for (;;) {
    const r = await clickReader.read();
    if (r.done) break;
    rest.push(new TextDecoder().decode(r.value));
  }
  assert.equal(rest.some(t => t.includes('"done"')), true, 'Relay stream completed after the P2P open');
  mark('relay click stream completed on its original channel');
  // Next button click goes P2P SSE.
  const nextClick = window.fetch('https://mesh.test/api/session/sid/send_message', { method: 'POST', headers: { accept: 'text/event-stream' }, body: encB.encode('prompt2') });
  await tick();
  assert.equal(relayBusiness().length, 1, 'post-open click did NOT go to Relay');
  const p2pReq = requestOn(s.channel, '/api/session/sid/send_message');
  assert.ok(p2pReq, 'post-open click sent a P2P stream request');
  assert.equal(p2pReq.type, 'stream_request');
  assert.equal(sentMessages(s.channel, 'stream_request').length, 1, 'exactly one P2P stream request, no replay');
  assert.ok(p2pReq.id, 'P2P stream request carries an id');
  deliver(s.channel, { type: 'stream_chunk', id: p2pReq.id, status: 200, headers: { 'content-type': 'text/event-stream' } });
  await tick();
  const p2pRes = await nextClick;
  assert.equal(p2pRes.status, 200, 'P2P stream headers delivered');
  const p2pReader = p2pRes.body.getReader();
  deliver(s.channel, { type: 'stream_chunk', id: p2pReq.id, body: encB.encode('data: {"message":{"content":"two"}}\n\n') });
  await tick();
  // The real Agent sends the header frame with an empty data payload, so the
  // stream first yields a 0-length chunk that the SSE parser ignores; skip it.
  let chunkText = '';
  for (;;) {
    const r = await p2pReader.read();
    if (r.done) break;
    chunkText = new TextDecoder().decode(r.value);
    if (chunkText !== '') break;
  }
  assert.equal(chunkText.includes('"two"'), true, 'P2P stream chunk delivered');
  deliver(s.channel, { type: 'stream_end', id: p2pReq.id });
  await tick();
  const end = await p2pReader.read();
  assert.equal(end.done, true, 'P2P stream ended');
  assert.equal(s.streams.size, 0, 'no stranded streams');
  assert.equal(s.pending.size, 0, 'no stranded pending entries');
  mark('post-open click streamed over P2P and ended');
  console.log('TIMELINE\n' + timeline.join('\n'));
""" + FINISHER_TAIL
    run_switch(HARNESS_WITH_CONN_JS, preamble, body)


def test_p2p_open_survives_old_relay_abort_and_failure():
    """After P2P opens, an in-flight Relay request that is aborted or fails must
    not disturb the new channel: a concurrent P2P request still settles and the
    channel stays open."""
    preamble = "global.MANIFEST_BEHAVIOR = 'defer';"
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  const timeline = [];
  globalThis.__tl = timeline;
  const mark = ev => timeline.push('t=' + String(Date.now()).padStart(6, '0') + ' ' + ev);
  await tick();
  global.MANIFEST_BEHAVIOR = 'ok';
  fireOnline(); advance(300); await tick(); await tick();
  // Two pre-open Relay GETs, both parked with headers arrived and body pending.
  const ac = new AbortController();
  const abortOp = window.fetch('https://mesh.test/api/session/test/a', { method: 'GET', signal: ac.signal });
  const failOp = window.fetch('https://mesh.test/api/session/test/b', { method: 'GET' });
  await tick();
  const abortRes = await abortOp;
  const failRes = await failOp;
  mark('two Relay GETs pending before the P2P open');
  assert.equal(relayBusiness().length, 2, 'two Relay GETs on the native fetch');
  // P2P opens while both Relay bodies are pending.
  lastChannel().forceOpen();
  await tick(); await tick();
  mark('P2P open');
  assert.equal(s.channel.readyState, 'open', 'P2P channel open');
  const abortReader = abortRes.body.getReader();
  const failReader = failRes.body.getReader();
  // A P2P request is parked on the channel before the old Relay requests fail.
  const p2pOp = window.fetch('https://mesh.test/api/session/test/c', { method: 'GET' });
  await tick();
  const p2pReq = requestOn(s.channel, '/api/session/test/c');
  assert.ok(p2pReq, 'P2P request parked on the open channel');
  // Abort the old Relay request.
  ac.abort();
  await tick();
  assert.equal(relayRecords[0].aborted, true, 'Relay request observed the abort');
  await abortReader.read().then(() => { throw new Error('abort should reject the body read'); }, e => {
    assert.equal(e.name, 'AbortError', 'aborted Relay body rejected with AbortError');
  });
  mark('old Relay request aborted after the P2P open');
  // Fail the other old Relay request with a network error.
  relayRecords[1].fail(new Error('network down'));
  await tick();
  await failReader.read().then(() => { throw new Error('failure should reject the body read'); }, e => {
    assert.equal(e.message, 'network down', 'failed Relay body rejected with the network error');
  });
  mark('old Relay request failed after the P2P open');
  // The new channel is unharmed and the P2P request settles normally.
  assert.equal(s.channel.readyState, 'open', 'P2P channel still open after old Relay failures');
  deliver(s.channel, { type: 'response', id: p2pReq.id, status: 200, headers: { 'content-type': 'application/json' }, body: encB.encode('{"ok":1}') });
  await tick();
  assert.equal(await (await p2pOp).text(), '{"ok":1}', 'P2P request settled after old Relay failures');
  assert.equal(s.pending.size, 0, 'no stranded pending entries');
  assert.equal(s.streams.size, 0, 'no stranded streams');
  console.log('TIMELINE\n' + timeline.join('\n'));
""" + FINISHER_TAIL
    run_switch(HARNESS_WITH_CONN_JS, preamble, body)


def test_p2p_setup_failure_falls_back_to_relay_and_settles():
    """When P2P setup fails outright, business requests fall back to Relay
    immediately (no initial-wait borrow) and settle; recovery later reopens P2P."""
    preamble = "global.MANIFEST_BEHAVIOR = 'defer';"
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  const timeline = [];
  globalThis.__tl = timeline;
  const mark = ev => timeline.push('t=' + String(Date.now()).padStart(6, '0') + ' ' + ev);
  await tick();
  global.MANIFEST_BEHAVIOR = 'fail';
  fireOnline(); advance(300); await tick(); await tick();
  mark('P2P setup failed; backoff scheduled');
  assert.equal(manifestFetches.length, 2, 'failed hint attempt replaced the bootstrap');
  assert.ok(s.reconnectTimer, 'failed attempt enters backoff');
  assert.equal(s.closed, true, 'transport marked closed after the failure');
  // Request immediately after the failure: must go Relay without any borrow wait.
  const op = window.fetch('https://mesh.test/api/session/test/models', { method: 'GET' });
  await tick();
  mark('request issued right after setup failure');
  assert.equal(relayBusiness().length, 1, 'request went straight to Relay');
  relayRecords[0].complete('{"relay":1}');
  assert.equal(await (await op).text(), '{"relay":1}', 'request settled via Relay');
  assert.equal(s.pending.size, 0, 'no stranded pending entries');
  mark('fallback request settled via Relay');
  // Recovery: the scheduled backoff starts a fresh attempt (the hint itself is
  // inside its 5s cooldown, so we let the backoff fire) and reopens P2P.
  global.MANIFEST_BEHAVIOR = 'ok';
  advance(5000); await tick(); await tick();
  assert.ok(s.pc, 'recovery attempt running');
  lastChannel().forceOpen();
  await tick(); await tick();
  mark('P2P recovered');
  assert.equal(s.channel.readyState, 'open', 'P2P reopened after recovery');
  const op2 = window.fetch('https://mesh.test/api/session/test/models', { method: 'GET' });
  await tick();
  const req = requestOn(s.channel, '/api/session/test/models');
  assert.ok(req, 'new request went P2P after recovery');
  assert.equal(relayBusiness().length, 1, 'no new Relay call after recovery');
  deliver(s.channel, { type: 'response', id: req.id, status: 200, headers: { 'content-type': 'application/json' }, body: encB.encode('{"p2p":1}') });
  await tick();
  assert.equal(await (await op2).text(), '{"p2p":1}', 'request fulfilled over recovered P2P');
  console.log('TIMELINE\n' + timeline.join('\n'));
""" + FINISHER_TAIL
    run_switch(HARNESS_WITH_CONN_JS, preamble, body)


def test_p2p_still_setting_up_falls_back_after_initial_window():
    """A click issued while the bootstrap attempt is still negotiating only
    borrows the bounded 1.2s window: it then falls back to Relay and settles,
    and nothing is stranded when P2P later opens."""
    preamble = "global.MANIFEST_BEHAVIOR = 'ok';"
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  const timeline = [];
  globalThis.__tl = timeline;
  const mark = ev => timeline.push('t=' + String(Date.now()).padStart(6, '0') + ' ' + ev);
  await tick(); await tick();
   mark('initial attempt waiting for channel open after the answer');
  assert.ok(s.pc, 'initial attempt created a peer connection');
  assert.equal(s.isInitialAttempt, true, 'page bootstrap attempt is the initial one');
   assert.ok(s.pc.remoteDescription, 'answer applied before channel open');
  // Click while P2P is still setting up.
  const clickOp = window.fetch('https://mesh.test/api/session/sid/send_message', { method: 'POST', headers: { accept: 'text/event-stream' }, body: encB.encode('prompt') });
  await tick();
  mark('click waiting in the bounded initial window');
  assert.equal(relayBusiness().length, 0, 'initial window borrows the wait before Relay');
  advance(1200); await tick();
  mark('initial window elapsed; click fell back to Relay');
  assert.equal(relayBusiness().length, 1, 'click fell back to Relay after the window');
  assert.equal(requestOn(s.channel, '/api/session/sid/send_message'), null, 'nothing sent over P2P during setup');
  relayRecords[0].complete('data: {"fallback":true}\n\n');
  assert.equal(await (await clickOp).text(), 'data: {"fallback":true}\n\n', 'click settled via Relay');
  assert.equal(s.pending.size, 0, 'no stranded pending entries');
  // P2P later opens; the next click uses it.
  lastChannel().forceOpen();
  await tick(); await tick();
  mark('P2P opened after the fallback');
  assert.equal(s.channel.readyState, 'open', 'P2P open');
  const op2 = window.fetch('https://mesh.test/api/session/sid/send_message', { method: 'POST', headers: { accept: 'text/event-stream' }, body: encB.encode('prompt2') });
  await tick();
  const req = requestOn(s.channel, '/api/session/sid/send_message');
  assert.ok(req, 'next click went P2P');
  assert.equal(relayBusiness().length, 1, 'next click did not re-enter Relay');
  deliver(s.channel, { type: 'stream_chunk', id: req.id, status: 200, headers: { 'content-type': 'text/event-stream' } });
  await tick();
   const res = await op2;
   assert.equal(res.status, 200, 'next click streamed over P2P');
   deliver(s.channel, { type: 'stream_end', id: req.id });
   await res.text();
   assert.equal(s.streams.size, 0);
   assert.equal(s.pending.size, 0);
  console.log('TIMELINE\n' + timeline.join('\n'));
""" + FINISHER_TAIL
    run_switch(HARNESS_WITH_CONN_JS, preamble, body)


def test_p2p_close_settles_pending_and_never_replays_mutation():
    """When the open P2P channel closes, the pending click settles with the
    documented disconnect error, the transport enters backoff with Relay
    fallback, and the failed mutation is never replayed on the reconnected
    channel."""
    preamble = "global.MANIFEST_BEHAVIOR = 'defer';"
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  const timeline = [];
  globalThis.__tl = timeline;
  const mark = ev => timeline.push('t=' + String(Date.now()).padStart(6, '0') + ' ' + ev);
  await tick();
  global.MANIFEST_BEHAVIOR = 'ok';
  fireOnline(); advance(300); await tick(); await tick();
  lastChannel().forceOpen();
  await tick(); await tick();
  mark('P2P open');
  assert.equal(s.channel.readyState, 'open', 'P2P channel open');
  // Mutation parked on the channel, headers not yet arrived. Attach the
  // rejection handler immediately so a later disconnect cannot crash Node.
  const clickOp = window.fetch('https://mesh.test/api/session/sid/send_message', { method: 'POST', headers: { accept: 'text/event-stream' }, body: encB.encode('prompt') });
  const clickError = clickOp.then(() => null, e => e.message);
  await tick();
  const clickReq = requestOn(s.channel, '/api/session/sid/send_message');
  assert.ok(clickReq, 'mutation sent over P2P');
  assert.equal(s.pending.size, 1, 'mutation pending pre-header');
  // Channel dies.
  lastChannel().forceClose();
  await tick(); await tick();
  mark('P2P channel closed');
  assert.equal(await clickError, 'P2P disconnected; request outcome may be unknown', 'pending mutation settled with the disconnect error');
  assert.equal(s.pending.size, 0, 'pending cleared after the close');
  assert.equal(s.streams.size, 0, 'streams cleared after the close');
  assert.equal(s.closed, true, 'transport marked closed');
  assert.ok(s.reconnectTimer, 'backoff scheduled after the close');
  assert.equal(s.channel, null, 'dead channel released');
  assert.equal(createdChannels[0].onmessage, null, 'old channel handlers detached');
  // During backoff new requests go Relay and settle.
  const fallbackOp = window.fetch('https://mesh.test/api/session/test/models', { method: 'GET' });
  await tick();
  mark('backoff request went to Relay');
  assert.equal(relayBusiness().length, 1, 'backoff request went to Relay');
  relayRecords[0].complete('{"relay":2}');
  assert.equal(await (await fallbackOp).text(), '{"relay":2}', 'backoff request settled via Relay');
  // Reconnect opens a fresh channel; the old mutation is never replayed.
  global.MANIFEST_BEHAVIOR = 'ok';
  // The hint cooldown (NETWORK_COOLDOWN_MS) swallows a back-to-back fireOnline
  // right after the t=300 hint that opened this channel; drive the reconnect
  // with its own backoff timer instead (scheduled by the channel close at
  // t=300 + reconnectDelay).
  advance(3000); await tick(); await tick();
  mark('reconnect attempt running');
  assert.equal(createdChannels.length, 2, 'fresh channel created');
  assert.equal(requestOn(createdChannels[1], '/api/session/sid/send_message'), null, 'old mutation not replayed on the new channel');
  lastChannel().forceOpen();
  await tick(); await tick();
  mark('P2P reconnected');
  assert.equal(s.channel.readyState, 'open', 'P2P reconnected');
  const op2 = window.fetch('https://mesh.test/api/session/sid/send_message', { method: 'POST', headers: { accept: 'text/event-stream' }, body: encB.encode('prompt2') });
  await tick();
  const req2 = requestOn(s.channel, '/api/session/sid/send_message');
  assert.ok(req2, 'new mutation went P2P on the fresh channel');
   assert.equal(relayBusiness().length, 1, 'new mutation did not re-enter Relay');
   assert.equal(sentMessages(s.channel, 'stream_request').length, 1, 'only the new mutation was sent');
   assert.notEqual(req2.id, clickReq.id);
   deliver(s.channel, { type: 'stream_chunk', id: req2.id, status: 200, headers: { 'content-type': 'text/event-stream' } });
   deliver(s.channel, { type: 'stream_end', id: req2.id });
   await (await op2).text();
   assert.equal(s.streams.size, 0);
   assert.equal(s.pending.size, 0);
  console.log('TIMELINE\n' + timeline.join('\n'));
""" + FINISHER_TAIL
    run_switch(HARNESS_WITH_CONN_JS, preamble, body)


def test_relay_headers_pending_across_open_keep_original_requests():
    """P2P availability must not reissue native fetches still awaiting headers."""
    body = FINISHER + r"""
  const s = window.__ocmTransport;
  await tick();
  global.MANIFEST_BEHAVIOR = 'ok';
  fireOnline(); advance(300); await tick(); await tick();
  global.DEFER_RELAY_HEADERS = true;
  let settled = 0;
  const old = ['GET', 'POST'].map(method => window.fetch(
    'https://mesh.test/_mesh/device/device-a/api/session/test/message',
    { method, ...(method === 'POST' ? { body: 'mutation' } : {}) }
  ).then(response => { settled++; return response.text(); }));
  await tick();
  assert.equal(relayBusiness().length, 2);
  assert.equal(settled, 0, 'both native fetches await headers');
  assert.ok(s.pc.remoteDescription);
  lastChannel().forceOpen(); await tick(); await tick();
  assert.equal(settled, 0, 'opening P2P leaves old fetches in flight');
  const next = window.fetch('https://mesh.test/_mesh/device/device-a/api/session/test/next');
  await tick();
  const req = requestOn(s.channel, '/api/session/test/next');
  assert.ok(req);
  assert.equal(sentMessages(s.channel, 'request').length, 1);
  assert.equal(sentMessages(s.channel, 'stream_request').length, 0);
  relayRecords.forEach((record, i) => {
    assert.equal(record.aborted, false);
    record.releaseHeaders();
    record.complete('old-' + i);
  });
  assert.deepEqual(await Promise.all(old), ['old-0', 'old-1']);
  deliver(s.channel, { type: 'response', id: req.id, status: 200, body: encB.encode('new') });
  assert.equal(await (await next).text(), 'new');
  assert.equal(relayBusiness().length, 2);
  assert.equal(s.pending.size, 0);
  assert.equal(s.streams.size, 0);
""" + FINISHER_TAIL
    run_switch(HARNESS_WITH_CONN_JS, "global.MANIFEST_BEHAVIOR = 'defer';", body)
