import asyncio
import base64
import json
import subprocess

import httpx
import pytest

from src.main import Agent, forwarding_headers
from src.static_adapter import TRANSPORT_ADAPTER


def test_relay_reframes_decoded_chunked_body():
    """代理读完分块体后必须让 HTTP 客户端重新决定帧格式。"""
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
    """HTTP 与流式通道均传输原始业务字节，不猜测业务字段。"""
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
        agent = Agent({'opencode_url': 'http://localhost:40960'})
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
    """裸 origin 保持默认 Server，显式入口不受页面切换影响。"""
    helpers = TRANSPORT_ADAPTER.split('  const requestPath =', 1)[1].split('  function rejectEntry', 1)[0]
    script = """
    const assert=require('node:assert/strict');
    const server='https://mesh.test/_mesh/device/ehang';
    const location={origin:'https://mesh.test',href:'https://mesh.test/server/'+Buffer.from(server).toString('base64url')+'/session/ses_test'};
    location.pathname=new URL(location.href).pathname;
    const state={manifest:{device_id:'gti'},defaultDevice:'gti'};
    const localStorage={getItem:()=>null};
    """ + 'const requestPath =' + helpers + """
    assert.equal(scopeNativeRequest('wss://mesh.test/api/pty/p/connect')[0],
      'wss://mesh.test/_mesh/device/gti/api/pty/p/connect');
    assert.equal(scopeNativeRequest('https://mesh.test/api/session/original/form')[0],
      'https://mesh.test/_mesh/device/gti/api/session/original/form');
    assert.equal(scopeNativeRequest('wss://other.test/api/pty/p/connect')[0],
      'wss://other.test/api/pty/p/connect');
    assert.equal(scopeNativeRequest('https://mesh.test/_mesh/device/gti/api/info')[0],
      'https://mesh.test/_mesh/device/gti/api/info');
    """
    subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)


def test_v2_adapter_keeps_native_xhr_and_eventsource():
    """执行整个适配器，V2 未使用的原生接口不应被替换。"""
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
    // SDK 同时构造两个 Server 的绝对 API 路径时，各自保留明确基址。
    assert.equal(new URL('/api/session/old/form','https://mesh.test/_mesh/device/gti').href,
      'https://mesh.test/_mesh/device/gti/api/session/old/form');
    assert.equal(new URL('/api/session','https://mesh.test/_mesh/device/ehang').href,
      'https://mesh.test/_mesh/device/ehang/api/session');
    assert.equal(new URL('/api/info','https://external.test/base').href,
      'https://external.test/api/info');
    assert.equal(new URL('https://external.test/api/info','https://mesh.test/_mesh/device/gti').href,
      'https://external.test/api/info');
    """
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_discovery_preserves_native_server_names_and_skips_v1():
    """设备发现只补充 V2 入口，保留用户名称与自建 Server。"""
    helpers = 'const requestPath =' + TRANSPORT_ADAPTER.split('  const requestPath =', 1)[1].split('  function rejectEntry', 1)[0]
    script = """
    const assert=require('node:assert/strict');
    const location={origin:'https://mesh.test',href:'https://mesh.test/',pathname:'/',reload(){}};
    const original=[{type:'http',displayName:'My workstation',http:{url:'https://mesh.test/_mesh/device/gti'}},
      {type:'http',displayName:'External',http:{url:'https://external.test'}}];
    const storage=new Map([['opencode.global.dat:server',JSON.stringify({list:original})]]);
    const localStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)};
    const state={}; const renderBar=()=>{};
    const nativeFetch=async url=>{
      if(url==='/_mesh/devices') return Response.json({devices:[
        {device_id:'gti',name:'Renamed host',online:true},
        {device_id:'ehang',name:'ehang',online:true},
        {device_id:'legacy',name:'legacy',online:true}],default_device:'gti'});
      if(url.includes('legacy')) return new Response('<html>V1</html>',{headers:{'content-type':'text/html'}});
      return Response.json({version:'2.0.6'});
    };
    """ + helpers + """
    (async()=>{
      await syncNativeServers();
      const store=JSON.parse(storage.get('opencode.global.dat:server'));
      assert.deepEqual(store.list.slice(0,2),original);
      assert.equal(store.list.length,3);
      assert.equal(store.list[2].http.url,'https://mesh.test/_mesh/device/ehang');
    })().catch(e=>{console.error(e);process.exitCode=1});
    """
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('scenario', ['switch', 'abort', 'stream'])
def test_p2p_upload_keeps_device_and_cancellation(scenario):
    """流式请求体在切换或取消后，不得继续发送 mutation。"""
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
      state.manifest={device_id:'gti'};
      const sent=[];
      const channel={readyState:'open',bufferedAmount:0,send:()=>assert.fail('closed old channel sent')};
      state.channel=channel;
      let controller;
      const cancellation=new AbortController();
      if(scenario==='abort') cancellation.abort();
      const body=new ReadableStream({start(c){controller=c}});
      const operation=window.fetch('https://mesh.test/_mesh/device/gti/api/session/test/prompt',
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


def test_large_request_falls_back_with_original_body():
    """Request 流式上传超过 P2P 上限后，Relay 接收相同请求体和设备地址。"""
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
      assert.equal(req.url,'https://mesh.test/_mesh/device/gti/api/upload');
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
      s.manifest={device_id:'gti'};s.channel={readyState:'open'};
      const body=new Uint8Array(32*1024*1024+1);body[body.length-1]=42;
      const request=new Request('https://mesh.test/_mesh/device/gti/api/upload',{method:'POST',body});
      assert.equal((await window.fetch(request)).status,204);
    })().then(()=>{completed=true}).catch(e=>{completed=true;console.error(e);process.exitCode=1});
    """
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
