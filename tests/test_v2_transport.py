import asyncio
import base64
import json
import subprocess

import httpx
import pytest

from src.main import Agent, forwarding_headers
from src.static_adapter import TRANSPORT_ADAPTER


def test_relay_reframes_decoded_chunked_body():
    """After the proxy reads a chunked body, the HTTP client must decide the framing format anew."""
    headers = forwarding_headers({
        'Content-Type': 'application/json',
        'Transfer-Encoding': 'chunked',
        'Connection': 'keep-alive, X-Hop',
        'X-Hop': 'private',
        'Keep-Alive': 'timeout=5',
        'TE': 'trailers',
        'Trailer': 'Digest',
    })
    request = httpx.Request('POST', 'http://localhost/api/pty', headers=headers, content=b'{}')
    assert request.content == b'{}'
    assert request.headers['content-type'] == 'application/json'
    assert request.headers['content-length'] == '2'
    for name in ('transfer-encoding', 'connection', 'x-hop', 'keep-alive', 'te', 'trailer'):
        assert name not in request.headers


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('body', [b'', b'{}', b'{"model":{"modelID":"test","providerID":"test"}}'])
def test_agent_preserves_body_and_reframes_headers(monkeypatch, stream, body):
    """Both HTTP and streaming channels carry raw business bytes and never guess business fields."""
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, content=b'ok', headers={'content-type': 'text/plain'})
    client_type = httpx.AsyncClient
    monkeypatch.setattr('src.main.httpx.AsyncClient', lambda **kwargs: client_type(
        **kwargs, transport=httpx.MockTransport(handle)))

    class Control:
        async def send(self, message):
            json.loads(message)

    async def scenario():
        agent = Agent({'opencode_url': 'http://localhost:4096'})
        item = {'id': 'test', 'method': 'POST', 'path': '/api/session/ses_test/model',
                'headers': {'content-type': 'application/json', 'transfer-encoding': 'chunked'},
                'body': base64.b64encode(body).decode()}
        if stream:
            await agent.local_stream(item, Control())
        else:
            await agent.local_request(item)
    asyncio.run(scenario())
    assert len(requests) == 1
    assert requests[0].content == body
    assert 'transfer-encoding' not in requests[0].headers


def test_browser_scopes_websocket_and_preserves_explicit_server():
    """A bare origin keeps the default Server; explicit entries are unaffected by page switches."""
    helpers = TRANSPORT_ADAPTER.split('  const requestPath =', 1)[1].split('  function rejectEntry', 1)[0]
    script = """
    const assert=require('node:assert/strict');
    const server='https://mesh.test/_mesh/device/device-b';
    const location={origin:'https://mesh.test',href:'https://mesh.test/server/'+Buffer.from(server).toString('base64url')+'/session/ses_test'};
    location.pathname=new URL(location.href).pathname;
    const state={manifest:{device_id:'device-a'},defaultDevice:'device-a'};
    const localStorage={getItem:()=>null};
    """ + 'const requestPath =' + helpers + """
    assert.equal(scopeNativeRequest('wss://mesh.test/api/pty/p/connect')[0],
      'wss://mesh.test/_mesh/device/device-a/api/pty/p/connect');
    assert.equal(scopeNativeRequest('https://mesh.test/api/session/original/form')[0],
      'https://mesh.test/_mesh/device/device-a/api/session/original/form');
    assert.equal(scopeNativeRequest('wss://other.test/api/pty/p/connect')[0],
      'wss://other.test/api/pty/p/connect');
    assert.equal(scopeNativeRequest('https://mesh.test/_mesh/device/device-a/api/info')[0],
      'https://mesh.test/_mesh/device/device-a/api/info');
    """
    subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)


def test_v2_adapter_keeps_native_xhr_and_eventsource():
    """Run the whole adapter; native interfaces unused by V2 must not be replaced."""
    adapter = TRANSPORT_ADAPTER.split('<script id="ocm-transport-adapter">', 1)[1].split('</script>', 1)[0]
    adapter = adapter.replace('__OCM_VERSION_JSON__', '"test"')
    script = """
    const assert=require('node:assert/strict');
    global.window=global;
    global.location={origin:'https://mesh.test',pathname:'/',href:'https://mesh.test/'};
    global.document={readyState:'loading',addEventListener(){}};
    global.history={pushState(){},replaceState(){}};
    global.localStorage={getItem(){return null}};
    global.addEventListener=()=>{};
    global.setTimeout=global.setInterval=()=>0;
    global.fetch=()=>new Promise(()=>{});
    global.WebSocket=class {};
    const XHR=global.XMLHttpRequest=class {};
    const ES=global.EventSource=class {};
    """ + adapter + """
    assert.equal(global.XMLHttpRequest,XHR);
    assert.equal(global.EventSource,ES);
    // When the SDK builds absolute API paths for two Servers at once, each keeps its explicit base.
    assert.equal(new URL('/api/session/old/form','https://mesh.test/_mesh/device/device-a').href,
      'https://mesh.test/_mesh/device/device-a/api/session/old/form');
    assert.equal(new URL('/api/session','https://mesh.test/_mesh/device/device-b').href,
      'https://mesh.test/_mesh/device/device-b/api/session');
    assert.equal(new URL('/api/info','https://external.test/base').href,
      'https://external.test/api/info');
    assert.equal(new URL('https://external.test/api/info','https://mesh.test/_mesh/device/device-a').href,
      'https://external.test/api/info');
    """
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_discovery_preserves_native_server_names_and_skips_v1():
    """Device discovery only adds V2 entries and preserves user names and user-created Servers."""
    helpers = 'const requestPath =' + TRANSPORT_ADAPTER.split('  const requestPath =', 1)[1].split('  function rejectEntry', 1)[0]
    script = """
    const assert=require('node:assert/strict');
    const location={origin:'https://mesh.test',href:'https://mesh.test/',pathname:'/',reload(){}};
    const original=[{type:'http',displayName:'My workstation',http:{url:'https://mesh.test/_mesh/device/device-a'}},
      {type:'http',displayName:'External',http:{url:'https://external.test'}}];
    const storage=new Map([['opencode.global.dat:server',JSON.stringify({list:original})]]);
    const localStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)};
    const state={}; const window={__ocmBootstrap:{}}; const renderBar=()=>{};
    const nativeFetch=async url=>{
      if(url==='/_mesh/devices') return Response.json({devices:[
        {device_id:'device-a',name:'Renamed host',online:true},
        {device_id:'device-b',name:'Device B',online:true},
        {device_id:'legacy',name:'legacy',online:true}],default_device:'device-a'});
      if(url.includes('legacy')) return new Response('<html>V1</html>',{headers:{'content-type':'text/html'}});
      return Response.json({version:'2.0.6'});
    };
    """ + helpers + """
    (async()=>{
      await syncNativeServers();
      const store=JSON.parse(storage.get('opencode.global.dat:server'));
      assert.deepEqual(store.list.slice(0,2),original);
      assert.equal(store.list.length,3);
      assert.equal(store.list[2].http.url,'https://mesh.test/_mesh/device/device-b');
    })().catch(e=>{console.error(e);process.exitCode=1});
    """
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('scenario', ['switch', 'abort', 'stream'])
def test_p2p_upload_keeps_device_and_cancellation(scenario):
    """After a switch or cancel, the streaming request body must not keep sending mutations."""
    adapter = TRANSPORT_ADAPTER.split('<script id="ocm-transport-adapter">', 1)[1].split('</script>', 1)[0]
    script = """
    const assert=require('node:assert/strict');
    global.window=global;
    global.location={origin:'https://mesh.test',host:'mesh.test',pathname:'/',href:'https://mesh.test/'};
    global.document={readyState:'loading',addEventListener(){}};
    global.history={pushState(){},replaceState(){}};
    global.localStorage={getItem(){return null}};
    global.addEventListener=()=>{};
    global.setTimeout=global.setInterval=()=>0;
    global.fetch=()=>new Promise(()=>{});
    global.WebSocket=class {};
    """ + adapter.replace('__OCM_VERSION_JSON__', '"test"') + f'\nconst scenario={json.dumps(scenario)};\n' + """
    let completed=false;
    process.on('beforeExit',()=>assert.ok(completed,'async assertions did not complete'));
    (async()=>{
      const state=window.__ocmTransport;
      state.manifest={device_id:'device-a'};
      const sent=[];
      const channel={readyState:'open',bufferedAmount:0,send:()=>assert.fail('closed old channel sent')};
      state.channel=channel;
      let controller;
      const cancellation=new AbortController();
      if(scenario==='abort') cancellation.abort();
      const body=new ReadableStream({start(c){controller=c}});
      const operation=window.fetch('https://mesh.test/_mesh/device/device-a/api/session/test/prompt',
        {method:'POST',body,duplex:'half',signal:cancellation.signal,
          headers:scenario==='stream'?{accept:'text/event-stream'}:{}});
      channel.readyState='closed';
      state.channel={readyState:'open',bufferedAmount:0,send(frame){
        sent.push(frame);
        const msg=JSON.parse(Buffer.from(JSON.parse(frame).data,'base64'));
        state.pending.get(msg.id)?.resolve({status:204,body:''});
      }};
      controller.enqueue(new TextEncoder().encode('{}'));controller.close();
      await assert.rejects(operation,scenario==='abort'?/abort/i:/channel unavailable/);
      assert.equal(sent.length,0);
    })().then(()=>{completed=true}).catch(e=>{completed=true;console.error(e);process.exitCode=1});
    """
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_bounded_probe_discards_on_abort_without_replay():
    """Abort during probing rejects immediately; the underlying stream is released; no mutation is sent.

    The current implementation buffers fully with clone().arrayBuffer() before checking
    the abort signal, so abort cannot interrupt an incomplete stream read; a bounded
    probe must respond to cancellation while reading.
    """
    adapter = TRANSPORT_ADAPTER.split('<script id="ocm-transport-adapter">', 1)[1].split('</script>', 1)[0]
    script = """
    const assert=require('node:assert/strict');
    global.window=global;
    global.location={origin:'https://mesh.test',host:'mesh.test',pathname:'/',href:'https://mesh.test/'};
    global.document={readyState:'loading',addEventListener(){}};
    global.history={pushState(){},replaceState(){}};
    global.localStorage={getItem(){return null}};
    global.addEventListener=()=>{};
    global.setTimeout=global.setInterval=()=>0;
    global.fetch=()=>new Promise(()=>{});
    global.WebSocket=class {};
    """ + adapter.replace('__OCM_VERSION_JSON__', '"test"') + """
    let completed=false;
    process.on('beforeExit',()=>assert.ok(completed,'async assertions did not complete'));
    (async()=>{
      const s=window.__ocmTransport;
      s.manifest={device_id:'device-a'};
      s.channel={readyState:'open',bufferedAmount:0,send:frame=>{throw new Error('send must not run on abort')}};
      const ac=new AbortController();
      let released=false;
      // Stream paused after pushing just 1 byte: the probe reads the first block and parks on a pending read
      const body=new ReadableStream({start(c){c.enqueue(new Uint8Array(1));},cancel(){released=true;}});
      const operation=window.fetch('https://mesh.test/_mesh/device/device-a/api/session/test/prompt',
        {method:'POST',body,duplex:'half',signal:ac.signal});
      // setImmediate is not covered by the adapter mock; trigger the cancel after the probe parks on a pending read
      setImmediate(()=>ac.abort(new DOMException('Aborted','AbortError')));
      await assert.rejects(operation,/abort/i);
      await new Promise(r=>setImmediate(r));
      assert.ok(released,'探测中止后底层请求体流必须被释放');
    })().then(()=>{completed=true}).catch(e=>{completed=true;console.error(e);process.exitCode=1});
    """
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_bounded_probe_relays_oversize_stream_without_full_buffering():
    """An unknown-size stream over the P2P limit immediately goes to Relay and releases the remaining source stream without full buffering.

    A never-ending stream: the bounded probe reads about the limit's worth of bytes and
    then enters Relay; a full-buffering implementation would make arrayBuffer() wait
    forever, so the test would time out (behavior regression test).
    """
    adapter = TRANSPORT_ADAPTER.split('<script id="ocm-transport-adapter">', 1)[1].split('</script>', 1)[0]
    script = """
    const assert=require('node:assert/strict');
    global.window=global;
    global.location={origin:'https://mesh.test',host:'mesh.test',pathname:'/',href:'https://mesh.test/'};
    global.document={readyState:'loading',addEventListener(){}};
    global.history={pushState(){},replaceState(){}};
    global.localStorage={getItem(){return null}};
    global.addEventListener=()=>{};
    global.setTimeout=global.setInterval=()=>0;
    global.relayInput=null;
    global.enqueued=0;
    global.released=false;
    // Real nativeFetch: intercept device discovery; immediately cancel Relay request bodies and return 204
    global.fetch=async input=>{
      if(typeof input==='string'&&input.startsWith('/_mesh/')) return new Promise(()=>{});
      global.relayInput=input;
      if(input&&input.body){try{await input.body.cancel();}catch(_){}}
      return new Response(null,{status:204});
    };
    global.WebSocket=class {};
    """ + adapter.replace('__OCM_VERSION_JSON__', '"test"') + """
    let completed=false;
    process.on('beforeExit',()=>assert.ok(completed,'async assertions did not complete'));
    (async()=>{
      const s=window.__ocmTransport;
      s.manifest={device_id:'device-a'};
      s.channel={readyState:'open'};
      // Never-ending push source; setImmediate is unaffected by the adapter mock; the cancel callback stops the pump and marks release
      const body=new ReadableStream({start(c){
        const pump=()=>{if(global.released)return;c.enqueue(new Uint8Array(1024*1024));global.enqueued+=1024*1024;if(!global.released)setImmediate(pump);};
        pump();
      },cancel(){global.released=true;}});
      const url='https://mesh.test/_mesh/device/device-a/api/upload';
      assert.equal((await window.fetch(url,{method:'POST',body,duplex:'half'})).status,204);
      assert.ok(global.relayInput instanceof Request,'Relay 必须收到 Request');
      // The bounded probe reads only about the limit's worth of bytes (just over 33MiB); it must never grow without bound
      assert.ok(global.enqueued < 33*1024*1024+4*1024*1024,'探测必须在上限附近停止');
      assert.ok(global.released,'转入 Relay 后残余源流必须被释放');
    })().then(()=>{completed=true}).catch(e=>{completed=true;console.error(e);process.exitCode=1});
    """
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_large_request_falls_back_with_original_body():
    """After a streaming upload exceeds the P2P limit, Relay receives the same request body and device address."""
    adapter = TRANSPORT_ADAPTER.split('<script id="ocm-transport-adapter">', 1)[1].split('</script>', 1)[0]
    script = """
    const assert=require('node:assert/strict');
    global.window=global;
    global.location={origin:'https://mesh.test',host:'mesh.test',pathname:'/',href:'https://mesh.test/'};
    global.document={readyState:'loading',addEventListener(){}};
    global.history={pushState(){},replaceState(){}};
    global.localStorage={getItem(){return null}};
    global.addEventListener=()=>{};
    global.setTimeout=global.setInterval=()=>0;
    global.fetch=async(input,init)=>{
      if(typeof input==='string'&&input.startsWith('/_mesh/')) return new Promise(()=>{});
      const req=new Request(input,init);
      assert.equal(req.url,'https://mesh.test/_mesh/device/device-a/api/upload');
      const bytes=new Uint8Array(await req.arrayBuffer());
      assert.equal(bytes.length,32*1024*1024+1);
      assert.equal(bytes[bytes.length-1],42);
      return new Response(null,{status:204});
    };
    global.WebSocket=class {};
    """ + adapter.replace('__OCM_VERSION_JSON__', '"test"') + """
    let completed=false;
    process.on('beforeExit',()=>assert.ok(completed,'async assertions did not complete'));
    (async()=>{
      const s=window.__ocmTransport;
      s.manifest={device_id:'device-a'};s.channel={readyState:'open'};
      const body=new Uint8Array(32*1024*1024+1);body[body.length-1]=42;
      const request=new Request('https://mesh.test/_mesh/device/device-a/api/upload',{method:'POST',body});
      assert.equal((await window.fetch(request)).status,204);
    })().then(()=>{completed=true}).catch(e=>{completed=true;console.error(e);process.exitCode=1});
    """
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
