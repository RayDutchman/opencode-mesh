import asyncio
import base64
import time
import subprocess

import httpx
import pytest

from src.frontend import adapt_entry, asset_prefix, parse_asset_route
from src.main import Gateway
from src.static_adapter import TRANSPORT_ADAPTER


def test_asset_namespace_preserves_device_and_rejects_api():
    prefix = asset_prefix('gti')
    assert parse_asset_route(prefix + '/_assets/index-a.js') == ('gti', '/_assets/index-a.js')
    with pytest.raises(ValueError):
        parse_asset_route(prefix + '/api/session')


def test_entry_adapter_only_changes_bootstrap_getter():
    original = b'function a(){return location.origin}const config={currentServerUrl:a(),defaultServerUrl:b()};let link=location.origin;'
    result = adapt_entry('/_assets/index-test.js', original).decode()
    assert result.startswith('await window.__ocmBootstrap.ready;')
    assert 'function a(){return window.__ocmBootstrap.serverUrl}' in result
    assert 'let link=location.origin;' in result
    assert adapt_entry('/_assets/library-test.js', original) == original


@pytest.mark.parametrize('source', [b'unknown frontend', b'function a(){return location.origin}function b(){return location.origin}currentServerUrl;defaultServerUrl;'])
def test_unknown_entry_contract_fails_explicitly(source):
    with pytest.raises(ValueError, match='bootstrap'):
        adapt_entry('/_assets/index-test.js', source)


def test_gateway_serves_adapted_assets_and_rejects_implicit_api(tmp_path):
    async def scenario():
        gateway = Gateway({'registry_file': str(tmp_path / 'devices.json'),
                           'auth': {'username': 'test', 'password': 'test'}})
        gateway.registry.devices['gti'] = {'device_id': 'gti', 'name': 'gti', 'last_seen': time.time(), 'ws': object()}
        paths = []
        async def respond(ws, item, **kwargs):
            paths.append(item['path'])
            if item['path'].startswith('/_assets/'):
                body = b'function a(){return location.origin}currentServerUrl;defaultServerUrl;'
                mime = 'text/javascript'
            else:
                body = b'<html><head><script type="module" src="/_assets/index-test.js"></script></head></html>'
                mime = 'text/html'
            gateway.pending[item['id']].set_result({'status': 200, 'headers': {'content-type': mime},
                                                   'body': base64.b64encode(body).decode()})
        gateway.send_control = respond
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),
                                     base_url='http://test', auth=('test', 'test')) as client:
            response = await client.get('/api/session')
            assert response.status_code == 400
            assert paths == []
            old_key = base64.urlsafe_b64encode(b'http://test').decode().rstrip('=')
            response = await client.get('/server/' + old_key + '/session/ses_old', headers={'accept': 'text/html'})
            assert response.status_code == 307
            new_key = base64.urlsafe_b64encode(b'http://test/_mesh/device/gti').decode().rstrip('=')
            assert response.headers['location'] == '/server/' + new_key + '/session/ses_old'
            response = await client.get('/')
            assert asset_prefix('gti') + '/_assets/index-test.js' in response.text
            response = await client.get(asset_prefix('gti') + '/_assets/index-test.js')
            assert response.status_code == 200
            assert response.text.startswith('await window.__ocmBootstrap.ready;')
            assert paths[-1] == '/_assets/index-test.js'
    asyncio.run(scenario())


@pytest.mark.parametrize('default_probe_fails', [False, True, 'offline'])
def test_bootstrap_removes_origin_alias_without_late_reload(default_probe_fails):
    function = 'async function syncNativeServers' + TRANSPORT_ADAPTER.split('async function syncNativeServers', 1)[1].split('  function rejectEntry', 1)[0]
    import json
    script = 'const defaultProbeFails=' + json.dumps(default_probe_fails) + ';\n' + r'''
    const assert=require('node:assert/strict');
    const origin='https://mesh.test', target=origin+'/_mesh/device/gti';
    const store=new Map([
      ['opencode.global.dat:server',JSON.stringify({list:[
        {type:'http',displayName:'old alias',http:{url:origin}},
        {type:'http',displayName:'my desktop',http:{url:target}},
        {type:'http',displayName:'external',http:{url:'https://elsewhere.test'}}
      ],projects:{local:['keep']},custom:42})],
      ['opencode.settings.dat:defaultServerUrl',origin],
      ['opencode.pwa.last-route','/server/'+Buffer.from(origin).toString('base64url')+'/session/ses_old'],
      ['opencode.global.dat:layout',JSON.stringify({home:{selection:{server:origin}},tabs:{keep:1}})]
    ]);
    global.window={__ocmBootstrap:{}};
    const state={},location={origin,pathname:'/',reload(){throw Error('late reload forbidden')}};
    const localStorage={getItem:k=>store.get(k),setItem:(k,v)=>store.set(k,v)};
    const readJson=(k,f)=>JSON.parse(store.get(k)||'null')??f;
    const serverTabUrl=id=>origin+'/_mesh/device/'+id;
    const encodeServer=s=>Buffer.from(s).toString('base64url');
    const renderBar=()=>{};
    const nativeFetch=async url=>{if(defaultProbeFails&&url===target+'/api/info')throw Error('timeout');return url==='/_mesh/devices'?{
      ok:true,json:async()=>({default_device:defaultProbeFails==='offline'?'ehang':'gti',configured_default_device:'gti',devices:[
        {device_id:'gti',name:'new name',online:defaultProbeFails!=='offline'},{device_id:'ehang',online:true}
      ]})
    }:{ok:true,headers:new Headers({'content-type':'application/json'}),json:async()=>({version:'2.0.6'})}};
    ''' + function + r'''
    let complete=false;process.on('beforeExit',()=>assert.ok(complete));
    syncNativeServers().then(()=>{
      assert.equal(window.__ocmBootstrap.serverUrl,target);
      assert.equal(store.get('opencode.settings.dat:defaultServerUrl'),defaultProbeFails==='offline'?origin+'/_mesh/device/ehang':target);
      const result=JSON.parse(store.get('opencode.global.dat:server'));
      assert.equal(result.list.length,3);
      assert.equal(result.list.find(x=>x.http.url===target).displayName,'my desktop');
      assert.ok(result.list.some(x=>x.http.url==='https://elsewhere.test'));
      assert.ok(!result.list.some(x=>x.http.url===origin));
      assert.deepEqual(result.projects,{local:['keep']});assert.equal(result.custom,42);
      const layout=JSON.parse(store.get('opencode.global.dat:layout'));
      assert.equal(layout.home.selection.server,target);assert.deepEqual(layout.tabs,{keep:1});
      assert.equal(store.get('opencode.pwa.last-route'),'/server/'+encodeServer(target)+'/session/ses_old');
      complete=true;
    }).catch(e=>{complete=true;console.error(e);process.exitCode=1});
    '''
    result=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr


def test_bootstrap_retries_failed_discovery_before_starting_ui():
    adapter = TRANSPORT_ADAPTER.split('<script id="ocm-transport-adapter">', 1)[1].split('</script>', 1)[0].replace('__OCM_VERSION_JSON__', '"test"')
    script = r'''
    const assert=require('node:assert/strict');global.window=global;
    global.location={origin:'https://mesh.test',host:'mesh.test',pathname:'/',href:'https://mesh.test/'};
    global.document={readyState:'loading',getElementById:()=>null,head:{},body:null,addEventListener(){}};
    global.history={pushState(){},replaceState(){}};global.addEventListener=()=>{};
    const storage=new Map();global.localStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)};
    global.WebSocket=class {};
    let attempts=0,retryVisible=false;
    const timer=global.setTimeout;
    global.setTimeout=(callback,delay,...args)=>{
      if(delay===3000){retryVisible=!!window.__ocmBootstrap.error;return timer(callback,0,...args)}
      return timer(callback,delay,...args);
    };
    global.fetch=async url=>{
      if(url==='/_mesh/devices'){
        if(++attempts===1)throw Error('temporary network failure');
        return {ok:true,json:async()=>({default_device:'gti',devices:[{device_id:'gti',online:true}]})};
      }
      if(String(url).endsWith('/api/info'))return {ok:true,headers:new Headers({'content-type':'application/json'}),json:async()=>({version:'2.0.6'})};
      return new Promise(()=>{});
    };
    ''' + adapter + r'''
    window.__ocmBootstrap.ready.then(()=>{
      assert.equal(attempts,2);assert.ok(retryVisible);
      assert.equal(window.__ocmBootstrap.error,null);
      assert.equal(window.__ocmBootstrap.serverUrl,'https://mesh.test/_mesh/device/gti');process.exit(0);
    }).catch(error=>{console.error(error);process.exit(1)});
    '''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_home_relay_measurement_never_probes_gateway_as_server():
    function = 'async function measureRelayRtt' + TRANSPORT_ADAPTER.split('async function measureRelayRtt', 1)[1].split('  function renderBar', 1)[0]
    script = r'''
    const assert=require('node:assert/strict');
    const state={},document={hidden:false},RTT_MAX_MS=30000;
    let device=null;const currentDeviceId=()=>null,activeDeviceId=()=>device;
    const urls=[],nativeFetch=async url=>{urls.push(url);return {ok:true}};
    const renderBar=()=>{};
    ''' + function + r'''
    (async()=>{
      await measureRelayRtt();assert.deepEqual(urls,[]);
      device='ehang';await measureRelayRtt();
      assert.deepEqual(urls,['/_mesh/device/ehang/api/info']);
    })().catch(error=>{console.error(error);process.exitCode=1});
    '''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
