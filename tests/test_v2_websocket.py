"""MeshWebSocket 关闭/生命周期回归（V2 浏览器适配器）。

覆盖 src/static_adapter.py 中 MeshWebSocket 的关闭语义，以
WHATWG WebSocket 规范行为为基准（Node undici 在 CONNECTING 期 close 存在
已知偏离，故以真实浏览器语义为准）：

- close() 在 CONNECTING 期（ws_open 尚未发出）→ 确定 CLOSED、恰好一次
  close(1006) 事件、无 error 事件、清理 state.sockets，且绝不触达 Agent。
- ws_open 已发出但未收到 ack 时 close() → close 帧排在已排队数据帧之后，
  等待 Agent 回 ws_closed 后确定终止，只触发一次 close。
- 防重复：终止后再次 close()/terminate 不得产生第二帧或第二个事件。
- 防陈旧输出：close 后到达的 ws_data/ws_opened 必须被丢弃。

测试采用 tests/test_v2_transport.py 相同的 Node 直跑适配器方式；通过注入
window.settle 暴露帧分发入口（不修改生产代码）。框架统一提供已接管的
window.WebSocket 替身与可控 P2P 通道。
"""

import subprocess
import textwrap

from src.static_adapter import TRANSPORT_ADAPTER

# 浏览器前置全局：真实定时器（测试需要等待异步帧排队刷出）。
_HEADER = """
const assert=require('node:assert/strict');
global.window=global;
global.location={origin:'https://mesh.test',host:'mesh.test',pathname:'/',href:'https://mesh.test/'};
global.document={readyState:'loading',getElementById:()=>null,head:{},body:null,addEventListener(){}};
global.history={pushState(){},replaceState(){}};
global.localStorage={getItem(){return null}};
global.addEventListener=()=>{};
global.fetch=()=>new Promise(()=>{}); // connectP2P 挂起：state.ready 默认不 resolve
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
    """把场景代码插入 Node 运行器并执行，失败时抛出断言信息。"""
    script = _HEADER + _adapter() + _FOOTER.replace('__SCENARIO__', textwrap.indent(scenario, '  '))
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or f'node exited {result.returncode}'


# 构造后等待 ws_open 入队刷出（构造器的 ws_open 在 state.ready 微任务链上）。
_WAIT_OPEN = """
for(let i=0;i<3;i++) await Promise.resolve();
"""


def test_close_during_connecting_terminates_locally_with_1006():
    """CONNECTING 期（ws_open 尚未发出）立即 close 必须确定终止。

    基准：WHATWG 规范中 close() 处于 CONNECTING 时"失败"连接 →
    CLOSED + 恰好一次 close(1006)、无 error；连接从未建立，不得向 Agent
    发出任何帧，且 state.sockets 必须清理。
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
    """ws_open 已发出但未 ack 时 close：排队二进制帧先于 close 帧，随后确定终止。

    场景：socket 打开后排队一个异步二进制帧并立即 close()。close 帧必须排在
    该数据帧之后（有界 DataChannel 保序），Agent 回 ws_closed 后终止为 CLOSED，
    全程只触发一次 close 事件并清理 state.sockets。
    """
    scenario = """
state.ready=Promise.resolve();
const ws=new window.WebSocket('wss://mesh.test/api/pty/p/connect');
""" + _WAIT_OPEN + """
assert.equal(framesOf('ws_open'),1,'ws_open 应已发出（未 ack 竞态前置）');
const events=[];
ws.addEventListener('open',()=>events.push('open'));
ws.addEventListener('close',e=>events.push('close:'+e.code));
window.settle({type:'ws_opened',id:ws.id});   // ack 到达：进入 OPEN
assert.equal(ws.readyState,1);
ws.send(new Blob([new Uint8Array([1,2,3])]));  // 异步序列化的二进制帧
ws.close(1000,'bye');
await new Promise(r=>setTimeout(r,0));         // 刷出帧队列
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
    """终止后重复 close、陈旧数据帧、迟到 open 均不得产出事件或帧。"""
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
ws.close(1001,'again');          // CLOSING 中的重复 close 必须无操作
await new Promise(r=>setTimeout(r,0));
assert.equal(framesOf('ws_close'),closes,'重复 close 不得再次发送 ws_close');
assert.throws(()=>ws.send('late'),/not open/,'close 后 send 必须抛错');
window.settle({type:'ws_closed',id:ws.id,code:1000});   // 正常终止
const closeEvents=events.filter(e=>e.startsWith('close:'));
assert.equal(closeEvents.length,1,'至多一次 close 事件');
assert.equal(ws.readyState,3);
window.settle({type:'ws_data',id:ws.id,kind:'text',data:'stale'});  // 陈旧输出
window.settle({type:'ws_opened',id:ws.id});
assert.ok(!events.includes('message'),'关闭后的陈旧数据帧必须被丢弃');
assert.equal(events.length,2,'终止后不得再有任何事件');
assert.equal(state.sockets.has(ws.id),false);
"""
    _run(scenario)


def test_close_send_failure_still_terminates():
    """OPEN 期 close 帧发送失败也必须确定终止（close 1006），不得滞留 CLOSING。"""
    scenario = """
state.ready=Promise.resolve();
const ws=new window.WebSocket('wss://mesh.test/api/pty/p/connect');
""" + _WAIT_OPEN + """
assert.equal(framesOf('ws_open'),1);
window.settle({type:'ws_opened',id:ws.id});
const events=[];
ws.addEventListener('error',()=>events.push('error'));
ws.addEventListener('close',e=>events.push('close:'+e.code));
channel.readyState='closed';     // 通道失效：ws_close 必然发送失败
ws.close(1000,'bye');
await new Promise(r=>setTimeout(r,0));
assert.equal(ws.readyState,3,'close 帧发送失败也必须确定 CLOSED');
assert.deepEqual(events,['close:1006'],'用户主动 close 失败不得产生 error 事件');
assert.equal(state.sockets.has(ws.id),false);
"""
    _run(scenario)


def test_double_failure_fires_single_close():
    """多个排队帧在同一通道故障下失败：只触发一次 error 与一次 close。"""
    scenario = """
state.ready=Promise.resolve();
const ws=new window.WebSocket('wss://mesh.test/api/pty/p/connect');
""" + _WAIT_OPEN + """
window.settle({type:'ws_opened',id:ws.id});
channel.send=()=>{throw new Error('boom');};   // 共享通道整体故障
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
    """传输级断线（failTransport）后，排队中的 send 任务失败不得重复触发事件。"""
    scenario = """
state.ready=Promise.resolve();
const ws=new window.WebSocket('wss://mesh.test/api/pty/p/connect');
""" + _WAIT_OPEN + """
window.settle({type:'ws_opened',id:ws.id});
const events=[];
ws.addEventListener('error',()=>events.push('error'));
ws.addEventListener('close',e=>events.push('close:'+e.code));
ws.send(new Uint8Array([9]));           // 排队：任务尚未运行
channel.send=()=>{throw new Error('boom');};
state.routeDeviceId='other';            // 设备切换触发 failTransport
history.replaceState({},'');
await new Promise(r=>setTimeout(r,0));  // 排队任务在断线清理之后运行
assert.equal(events.filter(e=>e==='error').length,1,'failTransport 后至多一次 error 事件');
assert.equal(events.filter(e=>e.startsWith('close:')).length,1,'failTransport 后至多一次 close 事件');
assert.equal(ws.readyState,3);
assert.equal(state.sockets.has(ws.id),false);
"""
    _run(scenario)