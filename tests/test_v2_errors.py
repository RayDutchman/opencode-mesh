import asyncio
import base64
import json
import subprocess

import httpx
import pytest

from src.main import Agent, INVALID_ENCODING_REASON
from src.static_adapter import TRANSPORT_ADAPTER


@pytest.mark.parametrize('kind', ['invalid', 'connection', 'size', 'protocol'])
def test_mesh_errors_have_json_content_type(monkeypatch, kind):
    """Mesh 自己生成的错误必须能由 SDK 按 JSON 解析。"""
    def handle(request):
        if kind == 'connection':
            raise httpx.ConnectError('unavailable', request=request)
        return httpx.Response(200, content=b'x' * 100)

    original = httpx.AsyncClient
    monkeypatch.setattr('src.main.httpx.AsyncClient', lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(handle)))

    async def scenario():
        agent = Agent({'opencode_url': 'http://localhost:4096', 'max_response_bytes': 10})
        item = {'id': 'error', 'method': 'POST', 'path': '/api/pty',
                'headers': {}, 'body': '!!!' if kind == 'invalid' else ''}
        if kind != 'protocol':
            return await agent.local_request(item)
        captured = []
        async def send(channel, message):
            captured.append(message)
        agent.p2p_send = send
        await agent._p2p_error(None, 'error', 400, INVALID_ENCODING_REASON)
        return captured[0]

    response = asyncio.run(scenario())
    assert response['status'] in (400, 502)
    assert response['headers']['content-type'] == 'application/json'
    assert json.loads(base64.b64decode(response['body']))['reason']


@pytest.mark.parametrize('after_headers', [False, True])
@pytest.mark.parametrize('reason', ['connection_failed', INVALID_ENCODING_REASON])
def test_p2p_stream_error_boundary(after_headers, reason):
    """首帧前错误返回 JSON Response，首帧后错误中断原始响应流。"""
    adapter = TRANSPORT_ADAPTER.split('<script id="ocm-transport-adapter">', 1)[1].split('</script>', 1)[0]
    adapter = adapter.replace('__OCM_VERSION_JSON__', '"test"').replace(
        'window.__ocmTransport = state;',
        'window.__ocmTransport = state; window.deliver = settleMessage;')
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
    """ + adapter + f'\nconst afterHeaders={json.dumps(after_headers)}; const reason={json.dumps(reason)};\n' + """
    let completed=false;
    process.on('beforeExit',()=>assert.ok(completed));
    (async()=>{
      const s=window.__ocmTransport;
      s.manifest={device_id:'device-a'};
      s.channel={readyState:'open',bufferedAmount:0,send(frame){
        const message=JSON.parse(Buffer.from(JSON.parse(frame).data,'base64'));
        if(message.type!=='stream_request') return;
        if(afterHeaders) deliver({type:'stream_chunk',id:message.id,status:200,headers:{'content-type':'text/event-stream'}});
        deliver({type:'stream_error',id:message.id,reason,error:'private diagnostic'});
      }};
      const response=await fetch('https://mesh.test/_mesh/device/device-a/api/event',{headers:{accept:'text/event-stream'}});
      if(afterHeaders){
        assert.equal(response.status,200);
        await assert.rejects(response.text(),/private diagnostic/);
      }else{
        assert.equal(response.status,reason==='connection_failed'?502:400);
        assert.equal(response.headers.get('content-type'),'application/json');
        assert.deepEqual(await response.json(),{error:reason,reason});
      }
      assert.equal(s.pending.size,0);assert.equal(s.streams.size,0);
    })().then(()=>{completed=true}).catch(e=>{completed=true;console.error(e);process.exitCode=1});
    """
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
