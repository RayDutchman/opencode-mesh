import subprocess

from src.static_adapter import TRANSPORT_ADAPTER


def test_mesh_device_menu_refreshes_safely_and_only_navigates_fresh_online_devices():
    start = TRANSPORT_ADAPTER.index('  function ensureBarStyle()')
    end = TRANSPORT_ADAPTER.index('  const requestPath =', start)
    menu = TRANSPORT_ADAPTER[start:end]
    script = r'''
const assert=require('node:assert/strict');
class Element {
  constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.parentNode=null;this.dataset={};this.attributes={};this.listeners={};this.className='';this.textContent='';this.disabled=false;this.style={};}
  append(...nodes){nodes.forEach(node=>{node.parentNode=this;this.children.push(node)});}
  appendChild(node){this.append(node);return node;}
  replaceChildren(...nodes){this.children=[];this.append(...nodes);}
  insertBefore(node,before){node.parentNode=this;const index=this.children.indexOf(before);this.children.splice(index<0?this.children.length:index,0,node);}
  remove(){if(!this.parentNode)return;this.parentNode.children.splice(this.parentNode.children.indexOf(this),1);this.parentNode=null;}
  setAttribute(key,value){this.attributes[key]=String(value);}
  getAttribute(key){return this.attributes[key]??null;}
  contains(node){for(;node;node=node.parentNode)if(node===this)return true;return false;}
  addEventListener(type,listener){(this.listeners[type]??=[]).push(listener);}
  dispatch(type,event={}){for(const listener of this.listeners[type]??[])listener({target:this,preventDefault(){},key:type,...event});}
  focus(){global.focused=this;document.activeElement=this;}
  getBoundingClientRect(){return {left:400,bottom:36};}
  querySelector(selector){return walk(this).find(node=>matches(node,selector))||null;}
  querySelectorAll(selector){return walk(this).filter(node=>matches(node,selector));}
}
const walk=root=>root.children.flatMap(node=>[node,...walk(node)]);
const matches=(node,selector)=>selector[0]==='.'?node.className.split(' ').includes(selector.slice(1)):selector[0]==='#'?node.id===selector.slice(1):node.tagName===selector.toUpperCase();
const body=new Element('body'),head=new Element('head');
const document={body,head,hidden:false,readyState:'complete',activeElement:null,createElement:tag=>new Element(tag),getElementById:id=>[body,head,...walk(body),...walk(head)].find(node=>node.id===id)||null,addEventListener(type,listener){(this.listeners[type]??=[]).push(listener)},listeners:{},dispatch(type,event={}){for(const listener of this.listeners[type]??[])listener(event)}};
global.document=document;global.window=global;global.innerWidth=420;global.innerHeight=800;global.location={assign(url){this.assigned=url}};global.AbortController=class{constructor(){this.signal={aborted:false}}abort(){this.signal.aborted=true}};
let requests=[];let responders=[];const nativeFetch=(url,options)=>{requests.push({url,options});return responders.shift()};
const state={devices:[{device_id:'device-a',name:'Old',online:true,upstream_health:'healthy',available:true}],defaultDevice:'device-a'};
let selectedDevice=null;
const rootHandoffDevice='device-b';
const currentDeviceId=()=>null;const selectedServerDeviceId=()=>selectedDevice;const activeDeviceId=()=>selectedServerDeviceId()||state.manifest?.device_id||state.defaultDevice;
const MESH_VERSION='test';
const BAR_CSS='';
const timers=[];const setTimeout=(fn,ms)=>{const timer={fn,ms};timers.push(timer);return timer};const clearTimeout=()=>{};
''' + menu + r'''
    const response=devices=>Promise.resolve({ok:true,json:()=>Promise.resolve({devices})});
const deferred=()=>{let resolve,reject;return {promise:new Promise((a,b)=>{resolve=a;reject=b}),resolve,reject}};
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  const button=ensureBar().querySelector('.ocm-device-menu-button');
  responders.push(response([]));button.dispatch('click');
  const initialPanel=document.getElementById('ocm-device-menu');
  assert.ok(initialPanel.querySelector('.ocm-device-menu-loading'),'initial discovery starts with loading');
  await settle();
  assert.equal(initialPanel.querySelectorAll('.ocm-device-menu-item').length,0,'an empty discovery result has no device rows');
  assert.equal(initialPanel.querySelector('.ocm-device-menu-loading'),null,'an empty discovery result exits loading');
  assert.equal(initialPanel.querySelector('.ocm-device-menu-error'),null,'an empty discovery result is not an error');
  responders.push(response([]));timers.filter(timer=>timer.ms===5000).at(-1).fn();
  assert.equal(initialPanel.querySelector('.ocm-device-menu-loading'),null,'a valid empty menu refresh stays silent');
  await settle();
  button.dispatch('click');
  const first=response([{device_id:'device-a',name:'<img>',online:true,upstream_health:'healthy',available:true},{device_id:'device-b',name:'Upstream unavailable',online:true,upstream_health:'unreachable',available:false},{device_id:'device-c',name:'Online',online:true,upstream_health:'healthy',available:true}]);
  responders.push(first);button.dispatch('click');
  const loadingPanel=document.getElementById('ocm-device-menu');assert.equal(loadingPanel.querySelector('.ocm-device-menu-loading').textContent,'Loading devices…','opening renders loading before discovery settles');
  await settle();
  const panel=document.getElementById('ocm-device-menu');const items=panel.querySelectorAll('.ocm-device-menu-item');
  assert.equal(button.getAttribute('aria-expanded'),'true');assert.equal(items.length,3);
  assert.deepEqual({credentials:requests[0].options.credentials,cache:requests[0].options.cache},{credentials:'same-origin',cache:'no-store'});
  assert.ok(timers.some(timer=>timer.ms===5000),'an open menu schedules its 5s status refresh');
  assert.equal(panel.style.position,'fixed','the menu is viewport-clamped instead of overflowing from its trigger');
  assert.equal(items[0].getAttribute('aria-current'),'true');assert.equal(items[0].children[1].textContent,'<img>');
  assert.equal(items[1].disabled,true);assert.equal(items[1].children[2].textContent,'OpenCode unavailable');items[1].dispatch('click');assert.equal(location.assigned,undefined);
  items[0].focus();const down={key:'ArrowDown',preventDefault(){}};
  panel.dispatch('keydown',down);document.dispatch('keydown',down);
  assert.equal(global.focused,items[2],'one bubbling key event moves once and skips disabled devices');
  document.dispatch('keydown',{key:'Home',preventDefault(){}});assert.equal(global.focused,items[0]);
  const originalRows=items.slice();const pending=deferred();responders.push(pending.promise);
  timers.filter(timer=>timer.ms===5000).at(-1).fn();await settle();
  assert.equal(panel.querySelectorAll('.ocm-device-menu-item')[0],originalRows[0],'a pending refresh keeps the existing DOM rows');
  assert.equal(originalRows[0].disabled,false,'a pending refresh keeps a previously online item clickable');
  assert.equal(originalRows[0].children[2].textContent,'Healthy','a pending refresh does not show Updating');
  assert.equal(global.focused,items[0],'a pending refresh preserves keyboard focus');
  pending.resolve({ok:true,json:()=>Promise.resolve({devices:[{device_id:'device-a',name:'<img>',online:true,upstream_health:'healthy',available:true},{device_id:'device-b',name:'Upstream unavailable',online:true,upstream_health:'unreachable',available:false},{device_id:'device-c',name:'Online',online:true,upstream_health:'healthy',available:true}]})});await settle();
  assert.equal(panel.querySelectorAll('.ocm-device-menu-item')[0],originalRows[0],'an unchanged response does not rebuild real DOM rows');
  state.defaultDevice='device-c';responders.push(response([{device_id:'device-a',name:'<img>',online:true,upstream_health:'healthy',available:true},{device_id:'device-b',name:'Offline',online:false,upstream_health:'unknown',available:false},{device_id:'device-c',name:'Online',online:true,upstream_health:'healthy',available:true}]));
  timers.filter(timer=>timer.ms===5000).at(-1).fn();await settle();
  assert.notEqual(panel.querySelectorAll('.ocm-device-menu-item')[0],originalRows[0],'a changed current device refreshes row semantics');
  assert.equal(panel.querySelectorAll('.ocm-device-menu-item')[2].getAttribute('aria-current'),'true','a changed current device updates aria-current');
  responders.push(response([{device_id:'device-a',name:'<img>',online:true,upstream_health:'healthy',available:true},{device_id:'device-b',name:'Offline',online:false,upstream_health:'unknown',available:false},{device_id:'device-c',name:'Online updated',online:true,upstream_health:'healthy',available:true}]));
  timers.filter(timer=>timer.ms===5000).at(-1).fn();await settle();
  assert.equal(document.getElementById('ocm-device-menu'),panel,'polling keeps the menu container stable');
  assert.equal(global.focused.dataset.ocmDeviceId,'device-a','polling restores keyboard focus to its device');
  document.getElementById('ocm-device-menu').querySelectorAll('.ocm-device-menu-item')[2].dispatch('click');assert.equal(location.assigned,'/?mesh_device=device-c');assert.equal(global.__xss,undefined);
  const second=deferred();responders.push(second.promise);button.dispatch('click');
  const reopenedPanel=document.getElementById('ocm-device-menu');
  assert.ok(reopenedPanel.querySelector('.ocm-device-menu-loading'),'reopened discovery explicitly displays loading');
  assert.equal(reopenedPanel.querySelectorAll('.ocm-device-menu-item').length,0,'reopened loading has no stale clickable rows');
  second.reject(Error('network'));await settle();
  assert.equal(reopenedPanel.querySelectorAll('.ocm-device-menu-item').length,0,'failed refresh does not retain stale status');
  assert.ok(reopenedPanel.querySelector('.ocm-device-menu-error'),'failed refresh replaces stale clickable state with an unavailable state');
  responders.push(response([]));timers.filter(timer=>timer.ms===5000).at(-1).fn();await settle();
  assert.equal(reopenedPanel.querySelector('.ocm-device-menu-error'),null,'a successful empty result exits error');
  assert.equal(reopenedPanel.querySelector('.ocm-device-menu-loading'),null,'a successful empty result does not remain loading');
  const old=deferred(),fresh=deferred();button.dispatch('click');responders.push(old.promise);button.dispatch('click');button.dispatch('click');responders.push(fresh.promise);button.dispatch('click');
  old.resolve({ok:true,json:()=>Promise.resolve({devices:[{device_id:'device-old',name:'Old result',online:true,upstream_health:'healthy',available:true}]})});
  fresh.resolve({ok:true,json:()=>Promise.resolve({devices:[{device_id:'device-new',name:'New result',online:true,upstream_health:'healthy',available:true}]})});
  await settle();
  assert.equal(document.getElementById('ocm-device-menu').querySelector('.ocm-device-menu-item').children[1].textContent,'New result','late response cannot overwrite a reopened menu');
  button.dispatch('click');const hungBody=deferred();responders.push(Promise.resolve({ok:true,json:()=>hungBody.promise}));button.dispatch('click');await settle();
  timers.filter(timer=>timer.ms===10000).at(-1).fn();await settle();
  assert.equal(document.getElementById('ocm-device-menu').querySelectorAll('.ocm-device-menu-item').length,0,'a hung response body reaches the discovery deadline and becomes unknown');
  responders.push(response([{device_id:'device-retry',name:'Retry result',online:true,upstream_health:'healthy',available:true}]));timers.filter(timer=>timer.ms===5000).at(-1).fn();await settle();
  hungBody.resolve({devices:[{device_id:'device-late',name:'Late result',online:true,upstream_health:'healthy',available:true}]});await settle();
  assert.equal(document.getElementById('ocm-device-menu').querySelector('.ocm-device-menu-item').children[1].textContent,'Retry result','a late body cannot overwrite the next refresh');
  document.dispatch('keydown',{key:'Escape',preventDefault(){this.prevented=true}});assert.equal(button.getAttribute('aria-expanded'),'false');assert.equal(global.focused,button);
  responders.push(response([{device_id:'device-a',name:'A',online:true,upstream_health:'healthy',available:true}]));button.dispatch('click');await Promise.resolve();await Promise.resolve();document.dispatch('pointerdown',{target:new Element('div')});assert.equal(button.getAttribute('aria-expanded'),'false');
  state.defaultDevice='device-a';responders.push(response([{device_id:'device-a',name:'A',online:true,upstream_health:'unreachable',available:false}]));button.dispatch('click');await settle();
  assert.equal(state.devices[0].upstream_health,'unreachable','menu discovery updates the shared device state');
  renderBar();assert.equal(button.dataset.health,'unavailable','an upstream failure changes the current-device indicator while P2P remains open');
  assert.equal(button.getAttribute('title'),'OpenCode unavailable');assert.match(button.getAttribute('aria-label'),/OpenCode unavailable/);assert.match(ensureBar().querySelector('.ocm-transport').innerHTML,/OpenCode unavailable.*Relay/,'the visible state remains distinct while retaining the real transport');
  const unavailable=document.getElementById('ocm-device-menu').querySelector('.ocm-device-menu-item');assert.equal(unavailable.disabled,true);unavailable.dispatch('click');assert.equal(location.assigned,'/?mesh_device=device-c');
  button.dispatch('click');responders.push(response([{device_id:'device-a',name:'Legacy',online:true}]));button.dispatch('click');await settle();
  const legacy=document.getElementById('ocm-device-menu').querySelector('.ocm-device-menu-item');assert.equal(legacy.disabled,false,'a control-online legacy device without health fields remains selectable');assert.equal(legacy.children[0].dataset.state,'unknown');legacy.dispatch('click');assert.equal(location.assigned,'/?mesh_device=device-a');
  responders.push(response([{device_id:'device-a',name:'Expired report',online:true,upstream_health:'unknown',available:false}]));button.dispatch('click');await settle();
  const expired=document.getElementById('ocm-device-menu').querySelector('.ocm-device-menu-item');assert.equal(expired.disabled,false);assert.equal(expired.children[0].dataset.state,'unknown');
  scheduleDeviceStatusRefresh();const openStatusTimer=timers.filter(timer=>timer.ms===5000).at(-1),beforeOpenStatus=requests.length;openStatusTimer.fn();assert.equal(requests.length,beforeOpenStatus,'an open menu supplies the status refresh without a duplicate background request');assert.notEqual(timers.filter(timer=>timer.ms===5000).at(-1),openStatusTimer,'the background scheduler continues while the menu is open');
  button.dispatch('click');scheduleDeviceStatusRefresh();const statusTimer=timers.filter(timer=>timer.ms===5000).at(-1);scheduleDeviceStatusRefresh();assert.equal(timers.filter(timer=>timer.ms===5000).at(-1),statusTimer,'closed-menu status refresh has one periodic owner');
  const background=deferred(),beforeBackground=requests.filter(request=>request.url==='/_mesh/devices').length;responders.push(background.promise);statusTimer.fn();refreshDeviceStatus();assert.equal(requests.filter(request=>request.url==='/_mesh/devices').length,beforeBackground+1,'menu and background refresh share one request');
  background.resolve({ok:true,json:()=>Promise.resolve({devices:[{device_id:'device-a',name:'A',online:true,upstream_health:'healthy',available:true}]})});await settle();renderBar();assert.equal(button.dataset.health,'healthy','a closed-menu refresh restores the current-device health indicator');
  state.devices.push({device_id:'device-b',name:'B',online:true,upstream_health:'healthy',available:true});
  state.manifest={device_id:'device-a'};state.handoffPending=true;
  renderBar();
  assert.equal(button.querySelector('.ocm-device-menu-label').textContent,'B','a pending handoff names its target, not the gateway default');
  assert.equal(button.dataset.health,'unknown','a pending handoff must not claim to be connected');
  assert.match(button.getAttribute('aria-label'),/Switching/);
  assert.doesNotMatch(ensureBar().querySelector('.ocm-transport').innerHTML,/Relay|P2P/,'pending transport has not been selected yet');
  state.devices=state.devices.filter(device=>device.device_id!=='device-b');renderBar();
  assert.equal(button.querySelector('.ocm-device-menu-label').textContent,'device-b','an undiscovered target never falls back to another device name');
  state.devices.push({device_id:'device-b',name:'B',online:true,upstream_health:'healthy',available:true});
  state.handoffPending=false;selectedDevice='device-b';renderBar();
  assert.equal(button.querySelector('.ocm-device-menu-label').textContent,'B','confirmed selection wins over a stale transport manifest');
  assert.equal(button.dataset.health,'healthy');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
