"""Verify authentication boundaries through real ASGI requests without touching production services."""
import asyncio
import json
import stat
import tempfile
from pathlib import Path


import httpx

from src.main import Gateway, forwarding_headers


async def main():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'registry.json'
        gateway = Gateway({'state_file': str(path), 'auth': {'username': 'test', 'password': 'test-pass'},
                           'enroll_token': 'test-enrollment'})
        transport = httpx.ASGITransport(app=gateway.app)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            denied = await client.get('/_mesh/devices')
            assert denied.status_code == 401 and 'Basic' in denied.headers['www-authenticate']
            assert (await client.get('/_mesh/devices', auth=('test', 'test-pass'))).status_code == 200
            body = {'device_id': 'test-device', 'enroll_token': 'test-enrollment'}
            first = await client.post('/_mesh/register', json=body)
            assert first.status_code == 200
            token = first.json()['agent_token']
            assert (await client.post('/_mesh/register', json=body)).status_code == 403
            owned = await client.post('/_mesh/register', json=body | {'agent_token': token})
            assert owned.status_code == 200 and owned.json()['agent_token'] == token
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert token in path.read_text()
            url = '/_mesh/deregister/test-device'
            assert (await client.delete(url, params={'token': token})).status_code == 403
            assert (await client.delete(url, headers={'X-Mesh-Agent-Token': token})).status_code == 200
            assert not json.loads(path.read_text())['devices']
        forwarded = forwarding_headers({'Authorization': 'secret', 'Cookie': 'secret',
                                         'Proxy-Authorization': 'secret', 'x-opencode-directory': '/project'})
        assert forwarded == {'x-opencode-directory': '/project'}
        print('PASS: Basic Auth, ownership proof, header isolation, deregistration, private state')


def test_authentication_ownership_and_private_state():
    asyncio.run(main())
