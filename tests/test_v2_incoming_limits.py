import subprocess

from src.static_adapter import TRANSPORT_ADAPTER


def test_p2p_incoming_assemblies_are_bounded_and_released_on_all_terminal_paths():
    adapter = TRANSPORT_ADAPTER.split('<script id="ocm-transport-adapter">', 1)[1].split('</script>', 1)[0]
    adapter = adapter.replace('__OCM_VERSION_JSON__', '"test"').replace(
        'window.__ocmTransport = state;',
        'window.__ocmTransport = state; window.__ocmSettle = settle;'
    )
    script = r'''
const assert=require('node:assert/strict');
global.window=global;
global.location={origin:'https://mesh.test',host:'mesh.test',pathname:'/',href:'https://mesh.test/'};
global.document={readyState:'loading',addEventListener(){}};
global.history={pushState(){},replaceState(){}};
global.localStorage={getItem(){return null},setItem(){}};
global.addEventListener=()=>{};
global.fetch=()=>new Promise(()=>{});
global.WebSocket=class {};
''' + adapter + r'''
const state=window.__ocmTransport;
const b64=text=>Buffer.from(text).toString('base64');
let frames=[];
const channel={readyState:'open',bufferedAmount:0,send(frame){frames.push(frame)}};
state.channel=channel;state.manifest={device_id:'device-a'};
channel.onmessage=event=>{try{window.__ocmSettle(JSON.parse(event.data))}catch(_){}};
const sent=type=>frames.map(frame=>JSON.parse(Buffer.from(JSON.parse(frame).data,'base64').toString())).find(item=>item.type===type);
(async()=>{
  // A canonical HTTP assembly is keyed by its envelope message_id, which equals
  // this request's id. Abort must release both the business state and assembly.
  const aborter=new AbortController();
  const normal=window.fetch('https://mesh.test/_mesh/device/device-a/api/session/test',{signal:aborter.signal});
  await Promise.resolve();await Promise.resolve();
  const request=sent('request');assert.ok(request);
  window.__ocmSettle({message_id:request.id,type:'response',id:request.id,sequence:0,data:b64('{"status":200'),final:false});
  aborter.abort();await assert.rejects(normal,/abort/i);
  assert.deepEqual({pending:state.pending.size,streams:state.streams.size,incoming:state.incoming.size},{pending:0,streams:0,incoming:0});

  // A timed-out SSE header fragment has the same cleanup guarantee.
  frames=[];
  const stream=window.fetch('https://mesh.test/_mesh/device/device-a/api/event',{headers:{accept:'text/event-stream'}});
  await Promise.resolve();await Promise.resolve();
  const streamRequest=sent('stream_request');assert.ok(streamRequest);
  window.__ocmSettle({message_id:streamRequest.id,type:'stream_chunk',id:streamRequest.id,status:200,headers:{},sequence:0,data:b64(''),final:false});
  state.pending.get(streamRequest.id).timer._onTimeout();
  await assert.rejects(stream,/timeout/i);
  assert.deepEqual({pending:state.pending.size,streams:state.streams.size,incoming:state.incoming.size},{pending:0,streams:0,incoming:0});

  // The real DataChannel callback must turn malformed base64 into an immediate
  // request failure and must not retain its partial assembly.
  frames=[];
  const malformed=window.fetch('https://mesh.test/_mesh/device/device-a/api/session/bad');
  await Promise.resolve();await Promise.resolve();
  const badRequest=sent('request');assert.ok(badRequest);
  channel.onmessage({data:JSON.stringify({message_id:badRequest.id,type:'response',id:badRequest.id,sequence:0,data:'!',final:false})});
  await assert.rejects(malformed,/invalid base64 encoding/i);
  assert.equal(state.incoming.size,0);

  // A finite unknown-ID experiment proves a peer cannot grow the map unbounded.
  for(let i=0;i<160;i++) channel.onmessage({data:JSON.stringify({message_id:'unknown-'+i,sequence:0,data:b64('x'),final:false})});
  assert.ok(state.incoming.size<=128,'unknown partial messages are bounded');
  for(const entry of state.incoming.values()) clearTimeout(entry.timer);
  state.incoming.clear();state.incomingBytes=0;

  // Fragmented text must still settle normally, and strict sequence checking
  // remains active rather than silently accepting a jump.
  frames=[];
  const complete=window.fetch('https://mesh.test/_mesh/device/device-a/api/session/complete');
  await Promise.resolve();await Promise.resolve();
  const completeRequest=sent('request');assert.ok(completeRequest);
  channel.onmessage({data:JSON.stringify({message_id:completeRequest.id,type:'response',id:completeRequest.id,status:200,headers:{},sequence:0,data:b64('{"ok":'),final:false})});
  channel.onmessage({data:JSON.stringify({message_id:completeRequest.id,sequence:1,data:b64('true}'),final:true})});
  assert.equal(await (await complete).text(),'{"ok":true}');
  assert.equal(state.incoming.size,0);

  // Completed WebSocket logical messages reuse their socket ID, but each is a
  // fresh complete envelope with sequence zero and no retained assembly.
  const received=[];
  state.sockets.set('socket-1',{readyState:1,binaryType:'blob',dispatch(type,event){if(type==='message')received.push(event.data)}});
  channel.onmessage({data:JSON.stringify({message_id:'ws-message-1',sequence:0,data:b64(JSON.stringify({type:'ws_data',id:'socket-1',kind:'text',data:'one'})),final:true})});
  channel.onmessage({data:JSON.stringify({message_id:'ws-message-1',sequence:0,data:b64(JSON.stringify({type:'ws_data',id:'socket-1',kind:'text',data:'two'})),final:true})});
  assert.deepEqual(received,['one','two']);
  assert.equal(state.incoming.size,0);
  process.exit(0);
})().catch(error=>{console.error(error);process.exit(1)});
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_p2p_incoming_byte_total_entry_and_ttl_limits_are_bounded_before_decode():
    adapter = TRANSPORT_ADAPTER.split('<script id="ocm-transport-adapter">', 1)[1].split('</script>', 1)[0]
    adapter = adapter.replace('__OCM_VERSION_JSON__', '"test"').replace(
        'const MAX_P2P_BYTES = 64 * 1024 * 1024;', 'const MAX_P2P_BYTES = 8;'
    ).replace(
        'const MAX_INCOMING_MESSAGES = 128;', 'const MAX_INCOMING_MESSAGES = 3;'
    ).replace(
        'const INCOMING_ASSEMBLY_TTL_MS = 120000;', 'const INCOMING_ASSEMBLY_TTL_MS = 1;'
    ).replace(
        'window.__ocmTransport = state;',
        'window.__ocmTransport = state; window.__ocmSettle = settle; window.__ocmFailTransport = failTransport;'
    )
    script = r'''
const assert=require('node:assert/strict');
global.window=global;global.location={origin:'https://mesh.test',host:'mesh.test',pathname:'/',href:'https://mesh.test/'};
global.document={readyState:'loading',getElementById(){return null},body:null,addEventListener(){}};global.history={pushState(){},replaceState(){}};
global.localStorage={getItem(){return null},setItem(){}};global.addEventListener=()=>{};
global.fetch=()=>new Promise(()=>{});global.WebSocket=class {};
''' + adapter + r'''
const state=window.__ocmTransport;
const b64=text=>Buffer.from(text).toString('base64');
(async()=>{
  // 12 encoded characters exceed the 8-byte budget before atob() can allocate
  // a decoded over-limit payload. The failed unknown ID is not retained.
  window.__ocmSettle({message_id:'oversize',sequence:0,data:'AAAAAAAAAAAA',final:false});
  assert.equal(state.incoming.size,0);
  // Two 4-byte partial assemblies fit the global 8-byte budget; a third does not.
  for(const id of ['a','b','c']) window.__ocmSettle({message_id:id,sequence:0,data:b64('1234'),final:false});
  assert.equal(state.incoming.size,2);assert.equal(state.incomingBytes,8);
  await new Promise(resolve=>setTimeout(resolve,10));
  assert.equal(state.incoming.size,0);assert.equal(state.incomingBytes,0);
  window.__ocmSettle({message_id:'disconnect',sequence:0,data:b64('x'),final:false});
  assert.equal(state.incoming.size,1);
  window.__ocmFailTransport(new Error('disconnect'));
  assert.equal(state.incoming.size,0);assert.equal(state.incomingBytes,0);
  process.exit(0);
})().catch(error=>{console.error(error);process.exit(1)});
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_p2p_incoming_errors_require_a_trusted_business_mapping_and_tombstone_cancelled_ids():
    adapter = TRANSPORT_ADAPTER.split('<script id="ocm-transport-adapter">', 1)[1].split('</script>', 1)[0]
    adapter = adapter.replace('__OCM_VERSION_JSON__', '"test"').replace(
        'window.__ocmTransport = state;',
        'window.__ocmTransport = state; window.__ocmSettle = settle;'
    )
    script = r'''
const assert=require('node:assert/strict');
global.window=global;global.location={origin:'https://mesh.test',host:'mesh.test',pathname:'/',href:'https://mesh.test/'};
global.document={readyState:'loading',addEventListener(){}};global.history={pushState(){},replaceState(){}};
global.localStorage={getItem(){return null},setItem(){}};global.addEventListener=()=>{};
global.fetch=()=>new Promise(()=>{});global.WebSocket=class {};
''' + adapter + r'''
const state=window.__ocmTransport;
const b64=text=>Buffer.from(text).toString('base64');
let frames=[];
const channel={readyState:'open',bufferedAmount:0,send(frame){frames.push(frame)}};
state.channel=channel;state.manifest={device_id:'device-a'};
channel.onmessage=event=>{try{window.__ocmSettle(JSON.parse(event.data))}catch(_){}};
const sent=type=>frames.map(frame=>JSON.parse(Buffer.from(JSON.parse(frame).data,'base64').toString())).find(item=>item.type===type);
(async()=>{
  // A malformed unknown envelope may claim another request ID, but its envelope
  // key is not the canonical response mapping and must not cancel that request.
  const controller=new AbortController();
  const request=window.fetch('https://mesh.test/_mesh/device/device-a/api/session/a',{signal:controller.signal});
  await Promise.resolve();await Promise.resolve();
  const sentRequest=sent('request');assert.ok(sentRequest);
  channel.onmessage({data:JSON.stringify({message_id:'evil',type:'response',id:sentRequest.id,sequence:0,data:'!',final:false})});
  await Promise.resolve();
  assert.equal(state.pending.has(sentRequest.id),true);
  assert.equal(state.incoming.has('evil'),false);
  controller.abort();await assert.rejects(request,/abort/i);

  // The legacy bare stream_chunk path is still accepted, but malformed bodies
  // must fail only the active stream identified by its existing state.
  frames=[];
  const stream=window.fetch('https://mesh.test/_mesh/device/device-a/api/event',{headers:{accept:'text/event-stream'}});
  await Promise.resolve();await Promise.resolve();
  const streamRequest=sent('stream_request');assert.ok(streamRequest);
  channel.onmessage({data:JSON.stringify({type:'stream_chunk',id:streamRequest.id,status:200,headers:{},body:''})});
  const streamResponse=await stream;
  const streamBody=streamResponse.text();
  channel.onmessage({data:JSON.stringify({type:'stream_chunk',id:streamRequest.id,body:'!'})});
  await assert.rejects(streamBody,/invalid base64 encoding/i);
  assert.equal(state.pending.has(streamRequest.id),false);
  assert.equal(state.streams.has(streamRequest.id),false);

  // An abort cannot be followed by a new non-final canonical assembly for the
  // same generated request ID; the bounded tombstone drops it before allocation.
  frames=[];
  const lateController=new AbortController();
  const late=window.fetch('https://mesh.test/_mesh/device/device-a/api/session/late',{signal:lateController.signal});
  await Promise.resolve();await Promise.resolve();
  const lateRequest=sent('request');assert.ok(lateRequest);
  lateController.abort();await assert.rejects(late,/abort/i);
  channel.onmessage({data:JSON.stringify({message_id:lateRequest.id,type:'response',id:lateRequest.id,sequence:0,data:b64('partial'),final:false})});
  assert.equal(state.incoming.has(lateRequest.id),false);
  process.exit(0);
})().catch(error=>{console.error(error);process.exit(1)});
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
