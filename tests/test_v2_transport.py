import asyncio
import base64
import json

import httpx
import pytest

from src.main import Agent, forwarding_headers


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
