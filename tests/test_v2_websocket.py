"""MeshWebSocket close/lifecycle regression tests (V2 browser adapter).

Covers the close semantics of MeshWebSocket in src/static_adapter.py against
WHATWG WebSocket spec behavior (Node undici has a known deviation for close
during CONNECTING, so real browser semantics are the reference):

- close() during CONNECTING (ws_open not yet sent) -> definitive CLOSED, exactly
  one close(1006) event, no error event, state.sockets cleaned up, and the
  Agent is never touched.
- close() after ws_open was sent but before the ack -> the close frame is queued
  after pending data frames; the socket terminates once the Agent replies
  ws_closed, firing close exactly once.
- De-duplication: closing/terminating again after termination must not produce a
  second frame or a second event.
- Stale output: ws_data/ws_opened arriving after close must be dropped.

The tests run the adapter directly under Node like tests/test_v2_transport.py;
window.settle is injected to expose the frame-dispatch entry point (production
code is not modified). The harness provides the adopted window.WebSocket
stand-in and a controllable P2P channel.
"""

import subprocess
import textwrap

from src.static_adapter import TRANSPORT_ADAPTER

# Browser prelude globals: real timers (the test must wait for queued async frames to flush).
_HEADER = """
const assert=require('node:assert/strict');
global.window=global;
global.location={origin:'https://mesh.test',host:'mesh.test',pathname:'/',href:'https://mesh.test/'};
global.document={readyState:'loading',getElementById:()=>null,head:{},body:null,addEventListener(){}};
global.history={pushState(){},replaceState(){}};
global.localStorage={getItem(){return null}};
global.addEventListener=()=>{};
global.fetch=()=>new Promise(()=>{}); // connectP2P parks: state.ready does not resolve by default
global.WebSocket=class {};
"""

_FOOTER = """
let completed=false;
process.on('beforeExit',()=>assert.ok(completed,'async assertions did not complete'));
(async()=>{
  const state=window.__ocmTransport;
  const sent=[];
  const channel={readyState:'open',bufferedAmount:0,send(frame){sent.push(frame);}};
  state.channel=channel;
  const decode=f=>JSON.parse(Buffer.from(JSON.parse(f).data,'base64'));
  const framesOf=t=>sent.map(decode).filter(m=>m.type===t).length;
__SCENARIO__
})().then(()=>{completed=true;process.exit(0);})
 .catch(e=>{completed=true;console.error(e);process.exit(1);});
"""


def _adapter() -> str:
    adapter = TRANSPORT_ADAPTER.split('<script id="ocm-transport-adapter">', 1)[1].split('</script>', 1)[0]
    adapter = adapter.replace('__OCM_VERSION_JSON__', '"test"').replace(
        'window.__ocmTransport = state;',
        'window.__ocmTransport = state; window.settle = settle;')
    return adapter


def _run(scenario: str) -> None:
    """Insert the scenario into the Node runner and execute it; raise with assertion details on failure."""
    script = _HEADER + _adapter() + _FOOTER.replace('__SCENARIO__', textwrap.indent(scenario, '  '))
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or f'node exited {result.returncode}'


# After construction, wait for ws_open to flush from the queue (the constructor's ws_open runs on the state.ready microtask chain).
_WAIT_OPEN = """
for(let i=0;i<3;i++) await Promise.resolve();
"""


def test_close_during_connecting_terminates_locally_with_1006():
    """An immediate close during CONNECTING (ws_open not yet sent) must terminate definitively.

    Baseline: per WHATWG, close() while CONNECTING "fails" the connection ->
    CLOSED plus exactly one close(1006) and no error; the connection never
    existed, so no frames may reach the Agent and state.sockets must be cleaned.
    """
    scenario = """
const ws=new window.WebSocket('wss://mesh.test/api/pty/p/connect');
const events=[];
ws.addEventListener('open',()=>events.push('open'));
ws.addEventListener('error',()=>events.push('error'));
ws.addEventListener('close',e=>events.push('close:'+e.code));
ws.close(1000,'bye');
assert.equal(ws.readyState,3,'CONNECTING 立即 close 必须确定 CLOSED');
assert.deepEqual(events,['close:1006'],'只应触发一次 close(1006)，且无 error 事件');
assert.equal(state.sockets.has(ws.id),false,'socket 必须从 state.sockets 清理');
assert.equal(sent.length,0,'ws_open 尚未发出时不得向 Agent 发送任何帧');
"""
    _run(scenario)


def test_close_after_open_sent_order_and_terminal():
    """close after ws_open was sent but before the ack: queued binary frames precede the close frame, then termination is definitive.

    Scenario: after the socket opens, queue one async binary frame and close
    immediately. The close frame must follow that data frame (the bounded
    DataChannel keeps order); once the Agent replies ws_closed the socket ends
    as CLOSED, firing close exactly once and cleaning up state.sockets.
    """
    scenario = """
state.ready=Promise.resolve();
const ws=new window.WebSocket('wss://mesh.test/api/pty/p/connect');
""" + _WAIT_OPEN + """
assert.equal(framesOf('ws_open'),1,'ws_open 应已发出（未 ack 竞态前置）');
const events=[];
ws.addEventListener('open',()=>events.push('open'));
ws.addEventListener('close',e=>events.push('close:'+e.code));
window.settle({type:'ws_opened',id:ws.id});   // ack arrives: enter OPEN
assert.equal(ws.readyState,1);
ws.send(new Blob([new Uint8Array([1,2,3])]));  // asynchronously serialized binary frame
ws.close(1000,'bye');
await new Promise(r=>setTimeout(r,0));         // flush the frame queue
const kinds=sent.map(decode).map(m=>m.type);
const iData=kinds.indexOf('ws_data'), iClose=kinds.lastIndexOf('ws_close');
assert.ok(iData>=0&&iData<iClose,`已排队数据帧必须先于 close 帧：${JSON.stringify(kinds)}`);
window.settle({type:'ws_closed',id:ws.id,code:1000});
assert.equal(ws.readyState,3,'Agent 回 ws_closed 后必须确定 CLOSED');
assert.deepEqual(events,['open','close:1000'],'恰好 open 一次、close 一次');
assert.equal(state.sockets.has(ws.id),false,'terminate 后 socket 必须清理');
"""
    _run(scenario)


def test_close_is_idempotent_and_drops_stale_output():
    """After termination, repeated close, stale data frames, and late open must produce no events or frames."""
    scenario = """
state.ready=Promise.resolve();
const ws=new window.WebSocket('wss://mesh.test/api/pty/p/connect');
""" + _WAIT_OPEN + """
const events=[];
ws.addEventListener('open',()=>events.push('open'));
ws.addEventListener('message',()=>events.push('message'));
ws.addEventListener('close',e=>events.push('close:'+e.code));
window.settle({type:'ws_opened',id:ws.id});
ws.close(1000,'bye');
await new Promise(r=>setTimeout(r,0));
const closes=framesOf('ws_close');
ws.close(1001,'again');          // a repeated close while CLOSING must be a no-op
await new Promise(r=>setTimeout(r,0));
assert.equal(framesOf('ws_close'),closes,'重复 close 不得再次发送 ws_close');
assert.throws(()=>ws.send('late'),/not open/,'close 后 send 必须抛错');
window.settle({type:'ws_closed',id:ws.id,code:1000});   // normal termination
const closeEvents=events.filter(e=>e.startsWith('close:'));
assert.equal(closeEvents.length,1,'至多一次 close 事件');
assert.equal(ws.readyState,3);
window.settle({type:'ws_data',id:ws.id,kind:'text',data:'stale'});  // stale output
window.settle({type:'ws_opened',id:ws.id});
assert.ok(!events.includes('message'),'关闭后的陈旧数据帧必须被丢弃');
assert.equal(events.length,2,'终止后不得再有任何事件');
assert.equal(state.sockets.has(ws.id),false);
"""
    _run(scenario)


def test_close_send_failure_still_terminates():
    """A failed close-frame send during OPEN must still terminate definitively (close 1006), never lingering in CLOSING."""
    scenario = """
state.ready=Promise.resolve();
const ws=new window.WebSocket('wss://mesh.test/api/pty/p/connect');
""" + _WAIT_OPEN + """
assert.equal(framesOf('ws_open'),1);
window.settle({type:'ws_opened',id:ws.id});
const events=[];
ws.addEventListener('error',()=>events.push('error'));
ws.addEventListener('close',e=>events.push('close:'+e.code));
channel.readyState='closed';     // channel is gone: the ws_close send must fail
ws.close(1000,'bye');
await new Promise(r=>setTimeout(r,0));
assert.equal(ws.readyState,3,'close 帧发送失败也必须确定 CLOSED');
assert.deepEqual(events,['close:1006'],'用户主动 close 失败不得产生 error 事件');
assert.equal(state.sockets.has(ws.id),false);
"""
    _run(scenario)


def test_double_failure_fires_single_close():
    """Multiple queued frames failing on the same broken channel: only one error and one close fire."""
    scenario = """
state.ready=Promise.resolve();
const ws=new window.WebSocket('wss://mesh.test/api/pty/p/connect');
""" + _WAIT_OPEN + """
window.settle({type:'ws_opened',id:ws.id});
channel.send=()=>{throw new Error('boom');};   // the shared channel fails as a whole
const events=[];
ws.addEventListener('error',()=>events.push('error'));
ws.addEventListener('close',e=>events.push('close:'+e.code));
ws.send(new Uint8Array([1]));
ws.send(new Uint8Array([2]));
await new Promise(r=>setTimeout(r,0));
assert.equal(events.filter(e=>e==='error').length,1,'至多一次 error 事件');
assert.equal(events.filter(e=>e.startsWith('close:')).length,1,'至多一次 close 事件');
assert.equal(ws.readyState,3);
assert.equal(state.sockets.has(ws.id),false);
"""
    _run(scenario)


def test_transport_failure_and_queued_send_single_close():
    """After a transport-level disconnect (failTransport), a queued send task failing must not re-trigger events."""
    scenario = """
state.ready=Promise.resolve();
const ws=new window.WebSocket('wss://mesh.test/api/pty/p/connect');
""" + _WAIT_OPEN + """
window.settle({type:'ws_opened',id:ws.id});
const events=[];
ws.addEventListener('error',()=>events.push('error'));
ws.addEventListener('close',e=>events.push('close:'+e.code));
ws.send(new Uint8Array([9]));           // queued: the task has not run yet
channel.send=()=>{throw new Error('boom');};
state.routeDeviceId='other';            // device switch triggers failTransport
history.replaceState({},'');
await new Promise(r=>setTimeout(r,0));  // the queued task runs after disconnect cleanup
assert.equal(events.filter(e=>e==='error').length,1,'failTransport 后至多一次 error 事件');
assert.equal(events.filter(e=>e.startsWith('close:')).length,1,'failTransport 后至多一次 close 事件');
assert.equal(ws.readyState,3);
assert.equal(state.sockets.has(ws.id),false);
"""
    _run(scenario)