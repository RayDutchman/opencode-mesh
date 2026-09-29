import asyncio
import base64
import json
import time
import subprocess

import httpx
import pytest

from src.frontend import adapt_entry, asset_prefix, parse_asset_route
from src.main import Gateway
from src.static_adapter import TRANSPORT_ADAPTER


@pytest.mark.parametrize('quote', ['"', "'", '`'])
@pytest.mark.parametrize('dependency', ['panel.css', 'panel.js'])
def test_preload_dependencies_stay_with_their_source_device(quote, dependency):
    source = ('var asset=function(e){return'+quote+'/'+quote+'+e};').encode()
    adapted = adapt_entry('/_assets/preload-helper-test.js', source, 'device-b')
    script = adapted.decode() + f'\nconsole.log(asset("_assets/{dependency}"));'
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == asset_prefix('device-b') + '/_assets/' + dependency
    assert adapt_entry('/_assets/ordinary.js', source, 'device-b') == source


@pytest.mark.parametrize('source', [
    b'unknown shape',
    b'var a=function(e){return`/`+e},b=function(e){return`/`+e};',
])
def test_unknown_preload_contract_fails_explicitly(source):
    with pytest.raises(ValueError, match='preload'):
        adapt_entry('/_assets/preload-helper-test.js', source, 'device-b')


def test_asset_namespace_preserves_device_and_rejects_api():
    prefix = asset_prefix('device-a')
    assert parse_asset_route(prefix + '/_assets/index-a.js') == ('device-a', '/_assets/index-a.js')
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
        gateway.registry.devices['device-a'] = {'device_id': 'device-a', 'name': 'Device A', 'last_seen': time.time(), 'ws': object()}
        paths = []
        async def respond(ws, item, **kwargs):
            paths.append(item['path'])
            if item['path'].startswith('/_assets/'):
                body = b'function a(){return location.origin}currentServerUrl;defaultServerUrl;'
                if '/preload-helper-' in item['path']:
                    body = b'var asset=function(e){return`/`+e};'
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
            new_key = base64.urlsafe_b64encode(b'http://test/_mesh/device/device-a').decode().rstrip('=')
            assert response.headers['location'] == '/server/' + new_key + '/session/ses_old'
            response = await client.get('/')
            assert asset_prefix('device-a') + '/_assets/index-test.js' in response.text
            response = await client.get(asset_prefix('device-a') + '/_assets/index-test.js')
            assert response.status_code == 200
            assert response.text.startswith('await window.__ocmBootstrap.ready;')
            assert paths[-1] == '/_assets/index-test.js'
            response = await client.get(asset_prefix('device-a') + '/_assets/preload-helper-test.js')
            assert response.status_code == 200
            assert asset_prefix('device-a') + '/' in response.text
    asyncio.run(scenario())


@pytest.mark.parametrize('default_probe_fails', [False, True, 'offline'])
def test_bootstrap_removes_origin_alias_without_late_reload(default_probe_fails):
    function = 'async function syncNativeServers' + TRANSPORT_ADAPTER.split('async function syncNativeServers', 1)[1].split('  function rejectEntry', 1)[0]
    import json
    script = 'const defaultProbeFails=' + json.dumps(default_probe_fails) + ';\n' + r'''
    const assert=require('node:assert/strict');
    const origin='https://mesh.test', target=origin+'/_mesh/device/device-a';
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
      ok:true,json:async()=>({default_device:defaultProbeFails==='offline'?'device-b':'device-a',configured_default_device:'device-a',devices:[
        {device_id:'device-a',name:'new name',online:defaultProbeFails!=='offline',upstream_health:defaultProbeFails==='offline'?'unknown':'healthy',available:defaultProbeFails!=='offline'},{device_id:'device-b',online:true,upstream_health:'healthy',available:true}
      ]})
    }:{ok:true,headers:new Headers({'content-type':'application/json'}),json:async()=>({version:'2.0.6'})}};
    ''' + function + r'''
    let complete=false;process.on('beforeExit',()=>assert.ok(complete));
    syncNativeServers().then(()=>{
      const selected=defaultProbeFails==='offline'?origin+'/_mesh/device/device-b':target;
      assert.equal(window.__ocmBootstrap.serverUrl,selected);
      assert.equal(store.get('opencode.settings.dat:defaultServerUrl'),defaultProbeFails==='offline'?origin+'/_mesh/device/device-b':target);
      const result=JSON.parse(store.get('opencode.global.dat:server'));
      assert.equal(result.list.length,3);
      assert.equal(result.list.find(x=>x.http.url===target).displayName,'my desktop');
      assert.ok(result.list.some(x=>x.http.url==='https://elsewhere.test'));
      assert.ok(!result.list.some(x=>x.http.url===origin));
      assert.deepEqual(result.projects,{local:['keep']});assert.equal(result.custom,42);
      const layout=JSON.parse(store.get('opencode.global.dat:layout'));
      assert.equal(layout.home.selection.server,selected);assert.deepEqual(layout.tabs,{keep:1});
      assert.equal(store.get('opencode.pwa.last-route'),'/server/'+encodeServer(selected)+'/session/ses_old');
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
        return {ok:true,json:async()=>({default_device:'device-a',devices:[{device_id:'device-a',online:true,upstream_health:'healthy',available:true}]})};
      }
      if(String(url).endsWith('/api/info'))return {ok:true,headers:new Headers({'content-type':'application/json'}),json:async()=>({version:'2.0.6'})};
      return new Promise(()=>{});
    };
    ''' + adapter + r'''
    window.__ocmBootstrap.ready.then(()=>{
      assert.equal(attempts,2);assert.ok(retryVisible);
      assert.equal(window.__ocmBootstrap.error,null);
      assert.equal(window.__ocmBootstrap.serverUrl,'https://mesh.test/_mesh/device/device-a');process.exit(0);
    }).catch(error=>{console.error(error);process.exit(1)});
    '''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_root_handoff_selects_only_a_discovered_online_v2_device_and_consumes_its_query():
    function = 'async function syncNativeServers' + TRANSPORT_ADAPTER.split('async function syncNativeServers', 1)[1].split('  function rejectEntry', 1)[0]
    script = r'''
    const assert=require('node:assert/strict');
    const origin='https://mesh.test', defaultUrl=origin+'/_mesh/device/device-a', selectedUrl=origin+'/_mesh/device/device-b';
    const store=new Map([
      ['opencode.global.dat:server',JSON.stringify({list:[{type:'http',displayName:'external',http:{url:'https://elsewhere.test'}}],projects:{keep:1}})],
      ['opencode.settings.dat:defaultServerUrl','https://elsewhere.test'],
      ['opencode.global.dat:layout',JSON.stringify({home:{directory:'/legacy',selection:{server:defaultUrl},other:'keep'},tabs:{keep:1}})]
    ]);
    const localStorage={getItem:k=>store.get(k),setItem:(k,v)=>store.set(k,v)};
    const location={origin,pathname:'/',search:'?keep=yes&mesh_device=device-b',hash:'#section',href:origin+'/?keep=yes&mesh_device=device-b#section'};
    const history={state:{x:1},replaceState(state,title,url){this.replaced=url;location.href=origin+url;const parsed=new URL(location.href);location.search=parsed.search;location.hash=parsed.hash;}};
    global.window={__ocmBootstrap:{}};
    const state={routeDeviceId:'device-a',defaultDevice:null,transportStarted:true};
    const readJson=(k,f)=>JSON.parse(store.get(k)||'null')??f;
    const serverTabUrl=id=>origin+'/_mesh/device/'+id;
    const encodeServer=s=>Buffer.from(s).toString('base64url');
    const renderBar=()=>{};
    let reconnects=0;const reconnectForDevice=()=>{reconnects++;state.routeDeviceId=state.defaultDevice;};
    const fetches=[];const nativeFetch=async url=>{fetches.push(String(url));if(url==='/_mesh/devices')return {ok:true,json:async()=>({configured_default_device:'device-a',default_device:'device-a',devices:[{device_id:'device-a',online:true,upstream_health:'healthy',available:true},{device_id:'device-b',online:true,upstream_health:'healthy',available:true}]})};return {ok:true,headers:new Headers({'content-type':'application/json'}),json:async()=>({version:'2.0.18'})};};
    ''' + function + r'''
    syncNativeServers().then(()=>{
      assert.equal(window.__ocmBootstrap.serverUrl,selectedUrl);
      assert.equal(state.defaultDevice,'device-b');assert.equal(state.routeDeviceId,'device-b');assert.equal(reconnects,1);
      assert.equal(store.get('opencode.settings.dat:defaultServerUrl'),'https://elsewhere.test','external default preference is not overwritten');
      const layout=JSON.parse(store.get('opencode.global.dat:layout'));
      assert.equal(layout.home.selection.server,selectedUrl);assert.equal(layout.home.directory,undefined);assert.equal(layout.home.other,'keep');assert.deepEqual(layout.tabs,{keep:1});
      assert.equal(history.replaced,'/?keep=yes#section');
      assert.ok(fetches.includes(selectedUrl+'/api/info'),'the chosen device is verified through its actual device route');
    }).catch(error=>{console.error(error);process.exitCode=1});
    '''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_root_handoff_allows_unknown_legacy_agent_only_after_v2_probe():
    function = 'async function syncNativeServers' + TRANSPORT_ADAPTER.split('async function syncNativeServers', 1)[1].split('  function rejectEntry', 1)[0]
    script = r'''
    const assert=require('node:assert/strict');const origin='https://mesh.test',target=origin+'/_mesh/device/device-b';
    const store=new Map([['opencode.global.dat:server',JSON.stringify({list:[]})],['opencode.global.dat:layout',JSON.stringify({home:{selection:{server:origin+'/_mesh/device/device-a'}}})]]);
    const localStorage={getItem:k=>store.get(k),setItem:(k,v)=>store.set(k,v)};
    const location={origin,pathname:'/',search:'?mesh_device=device-b',hash:'',href:origin+'/?mesh_device=device-b'};
    const history={state:null,replaceState(state,title,url){this.replaced=url;}};global.window={__ocmBootstrap:{}};
    const state={routeDeviceId:'device-a',transportStarted:false};const readJson=(k,f)=>JSON.parse(store.get(k)||'null')??f;const serverTabUrl=id=>origin+'/_mesh/device/'+id;const encodeServer=s=>Buffer.from(s).toString('base64url');const renderBar=()=>{};const reconnectForDevice=()=>{throw Error('handoff starts after bootstrap')};
    const nativeFetch=async url=>url==='/_mesh/devices'?{ok:true,json:async()=>({configured_default_device:'device-a',default_device:'device-a',devices:[{device_id:'device-a',online:true,upstream_health:'healthy',available:true},{device_id:'device-b',online:true}]})}:{ok:true,headers:new Headers({'content-type':'application/json'}),json:async()=>({version:'2.0.18'})};
    ''' + function + r'''
    syncNativeServers().then(()=>{
      assert.equal(window.__ocmBootstrap.serverUrl,target);assert.equal(state.defaultDevice,'device-b');assert.equal(history.replaced,'/');
    }).catch(error=>{console.error(error);process.exitCode=1});
    '''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('handoff', [True, False])
def test_unknown_handoff_probe_failure_keeps_the_explicit_target_retryable(handoff):
    function = 'async function syncNativeServers' + TRANSPORT_ADAPTER.split('async function syncNativeServers', 1)[1].split('  function rejectEntry', 1)[0]
    script = r'''
    const assert=require('node:assert/strict');const origin='https://mesh.test';
    const store=new Map([['opencode.global.dat:server',JSON.stringify({list:[]})],['opencode.global.dat:layout',JSON.stringify({home:{selection:{server:origin+'/_mesh/device/device-a'}}})]]);
    const localStorage={getItem:k=>store.get(k),setItem:(k,v)=>store.set(k,v)};const location={origin,pathname:'/',search:'?mesh_device=device-b',hash:'',href:origin+'/?mesh_device=device-b'};
    const history={replaceState(){throw Error('failed unknown handoff must retain its query')},state:null};global.window={__ocmBootstrap:{}};
    const state={routeDeviceId:'device-a'};const readJson=(k,f)=>JSON.parse(store.get(k)||'null')??f;const serverTabUrl=id=>origin+'/_mesh/device/'+id;const encodeServer=s=>Buffer.from(s).toString('base64url');const renderBar=()=>{};const reconnectForDevice=()=>{throw Error('must not fall back')};
    const nativeFetch=async url=>url==='/_mesh/devices'?{ok:true,json:async()=>({configured_default_device:'device-a',default_device:'device-a',devices:[{device_id:'device-a',online:true,upstream_health:'healthy',available:true},{device_id:'device-b',online:true,upstream_health:'unknown',available:false}]})}:url.includes('/device-a/')?{ok:true,headers:new Headers({'content-type':'application/json'}),json:async()=>({version:'2.0.18'})}:Promise.reject(Error('legacy probe failed'));
    ''' + ('' if handoff else "location.search='';") + function + r'''
    syncNativeServers().then(()=>{throw Error('failed unknown handoff must not select another device')},error=>{
      assert.match(String(error),/handoff|verified/i);assert.equal(window.__ocmBootstrap.serverUrl,undefined);
      assert.equal(JSON.parse(store.get('opencode.global.dat:layout')).home.selection.server,origin+'/_mesh/device/device-a');
    }).catch(error=>{console.error(error);process.exitCode=1});
    '''
    if not handoff:
        script = script.replace("configured_default_device:'device-a'", "configured_default_device:'device-b'")
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('devices', [
    "[{device_id:'device-a',online:true,upstream_health:'healthy',available:true}]",
    "[{device_id:'device-a',online:true,upstream_health:'healthy',available:true},{device_id:'device-b',online:false,upstream_health:'unknown',available:false}]",
    "[{device_id:'device-a',online:true,upstream_health:'healthy',available:true},{device_id:'device-b',online:true,upstream_health:'healthy',available:true}]",
])
def test_invalid_root_handoff_never_falls_back_or_consumes_the_retryable_query(devices):
    function = 'async function syncNativeServers' + TRANSPORT_ADAPTER.split('async function syncNativeServers', 1)[1].split('  function rejectEntry', 1)[0]
    # The three cases are absent, offline, and V2-incompatible respectively.
    incompatible = devices.endswith("online:true,upstream_health:'healthy',available:true}]") and "device-b" in devices
    script = r'''
    const assert=require('node:assert/strict');const origin='https://mesh.test';
    const store=new Map([['opencode.global.dat:server',JSON.stringify({list:[]})],['opencode.global.dat:layout',JSON.stringify({home:{selection:{server:origin+'/_mesh/device/device-a'}}})]]);
    const localStorage={getItem:k=>store.get(k),setItem:(k,v)=>store.set(k,v)};
    const location={origin,pathname:'/',search:'?mesh_device=device-b&keep=yes',hash:'#retry',href:origin+'/?mesh_device=device-b&keep=yes#retry'};
    const history={replaceState(){throw Error('invalid handoff must retain its query')},state:null};global.window={__ocmBootstrap:{}};
    const state={routeDeviceId:'device-a'};const readJson=(k,f)=>JSON.parse(store.get(k)||'null')??f;const serverTabUrl=id=>origin+'/_mesh/device/'+id;const encodeServer=s=>Buffer.from(s).toString('base64url');const renderBar=()=>{};const reconnectForDevice=()=>{throw Error('must not reconnect to another device')};
    const nativeFetch=async url=>url==='/_mesh/devices'?{ok:true,json:async()=>({configured_default_device:'device-a',default_device:'device-a',devices:__DEVICES__})}:{ok:true,headers:new Headers({'content-type':'application/json'}),json:async()=>({version:__VERSION__})};
    '''.replace('__DEVICES__', devices).replace('__VERSION__', "'1.9.0'" if incompatible else "'2.0.18'") + function + r'''
    syncNativeServers().then(()=>{throw Error('invalid handoff unexpectedly selected a fallback')},error=>{
      assert.match(String(error),/handoff/i);assert.equal(location.search,'?mesh_device=device-b&keep=yes');
      assert.equal(JSON.parse(store.get('opencode.global.dat:layout')).home.selection.server,origin+'/_mesh/device/device-a');
    }).catch(error=>{console.error(error);process.exitCode=1});
    '''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_session_deep_link_does_not_let_handoff_query_override_its_device():
    function = 'async function syncNativeServers' + TRANSPORT_ADAPTER.split('async function syncNativeServers', 1)[1].split('  function rejectEntry', 1)[0]
    key = base64.urlsafe_b64encode(b'https://mesh.test/_mesh/device/device-a').decode().rstrip('=')
    script = r'''
    const assert=require('node:assert/strict');const origin='https://mesh.test',route=__ROUTE__;
    const store=new Map([['opencode.global.dat:server',JSON.stringify({list:[]})],['opencode.global.dat:layout',JSON.stringify({home:{selection:{server:origin+'/_mesh/device/device-a'}}})]]);
    const localStorage={getItem:k=>store.get(k),setItem:(k,v)=>store.set(k,v)};const location={origin,pathname:route,search:'?mesh_device=device-b',hash:'',href:origin+route+'?mesh_device=device-b'};const history={replaceState(){throw Error('deep-link query must remain')},state:null};global.window={__ocmBootstrap:{}};
    const state={routeDeviceId:'device-a'};const readJson=(k,f)=>JSON.parse(store.get(k)||'null')??f;const serverTabUrl=id=>origin+'/_mesh/device/'+id;const encodeServer=s=>Buffer.from(s).toString('base64url');const renderBar=()=>{};const reconnectForDevice=()=>{throw Error('deep-link device must not be changed')};
    const nativeFetch=async url=>url==='/_mesh/devices'?{ok:true,json:async()=>({configured_default_device:'device-a',default_device:'device-a',devices:[{device_id:'device-a',online:true,upstream_health:'healthy',available:true},{device_id:'device-b',online:true,upstream_health:'healthy',available:true}]})}:{ok:true,headers:new Headers({'content-type':'application/json'}),json:async()=>({version:'2.0.18'})};
    '''.replace('__ROUTE__', json.dumps('/server/' + key + '/session/ses_keep')) + function + r'''
    syncNativeServers().then(()=>{assert.equal(window.__ocmBootstrap.serverUrl,origin+'/_mesh/device/device-a');assert.equal(location.search,'?mesh_device=device-b')}).catch(error=>{console.error(error);process.exitCode=1});
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
      device='device-b';await measureRelayRtt();
      assert.deepEqual(urls,['/_mesh/device/device-b/api/info']);
    })().catch(error=>{console.error(error);process.exitCode=1});
    '''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
