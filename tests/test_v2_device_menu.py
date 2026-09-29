import subprocess

from src.static_adapter import TRANSPORT_ADAPTER


def test_mesh_device_menu_refreshes_safely_and_only_navigates_fresh_online_devices():
    start = TRANSPORT_ADAPTER.index('  function ensureBarStyle()')
    end = TRANSPORT_ADAPTER.index('  function currentDeviceInfo()', start)
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
const state={devices:[{device_id:'device-a',name:'Old',online:true}],defaultDevice:'device-a'};
const currentDeviceId=()=>null;const selectedServerDeviceId=()=>null;const activeDeviceId=()=>state.defaultDevice;
const transportInfo=()=>({kind:'relay',label:'Relay'});const MESH_VERSION='test';
const BAR_CSS='';
const timers=[];const setTimeout=(fn,ms)=>{const timer={fn,ms};timers.push(timer);return timer};const clearTimeout=()=>{};
''' + menu + r'''
const response=devices=>Promise.resolve({ok:true,json:()=>Promise.resolve({devices})});
const deferred=()=>{let resolve,reject;return {promise:new Promise((a,b)=>{resolve=a;reject=b}),resolve,reject}};
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  const button=ensureBar().querySelector('.ocm-device-menu-button');
  const first=response([{device_id:'device-a',name:'<img>',online:true},{device_id:'device-b',name:'Offline',online:false},{device_id:'device-c',name:'Online',online:true}]);
  responders.push(first);button.dispatch('click');await settle();
  const panel=document.getElementById('ocm-device-menu');const items=panel.querySelectorAll('.ocm-device-menu-item');
  assert.equal(button.getAttribute('aria-expanded'),'true');assert.equal(items.length,3);
  assert.deepEqual({credentials:requests[0].options.credentials,cache:requests[0].options.cache},{credentials:'same-origin',cache:'no-store'});
  assert.ok(timers.some(timer=>timer.ms===3000),'an open menu schedules its 3s status refresh');
  assert.equal(panel.style.position,'fixed','the menu is viewport-clamped instead of overflowing from its trigger');
  assert.equal(items[0].getAttribute('aria-current'),'true');assert.equal(items[0].children[1].textContent,'<img>');
  assert.equal(items[1].disabled,true);items[1].dispatch('click');assert.equal(location.assigned,undefined);
  items[0].focus();const down={key:'ArrowDown',preventDefault(){}};
  panel.dispatch('keydown',down);document.dispatch('keydown',down);
  assert.equal(global.focused,items[2],'one bubbling key event moves once and skips disabled devices');
  document.dispatch('keydown',{key:'Home',preventDefault(){}});assert.equal(global.focused,items[0]);
  responders.push(response([{device_id:'device-a',name:'<img>',online:true},{device_id:'device-b',name:'Offline',online:false},{device_id:'device-c',name:'Online updated',online:true}]));
  timers.find(timer=>timer.ms===3000).fn();await settle();
  assert.equal(document.getElementById('ocm-device-menu'),panel,'polling keeps the menu container stable');
  assert.equal(global.focused.dataset.ocmDeviceId,'device-a','polling restores keyboard focus to its device');
  document.getElementById('ocm-device-menu').querySelectorAll('.ocm-device-menu-item')[2].dispatch('click');assert.equal(location.assigned,'/?mesh_device=device-c');assert.equal(global.__xss,undefined);
  const second=deferred();responders.push(second.promise);button.dispatch('click');
  assert.equal(document.getElementById('ocm-device-menu').querySelectorAll('.ocm-device-menu-item').every(item=>item.disabled),true,'stale green devices are disabled while refreshing');
  second.reject(Error('network'));await Promise.resolve();await Promise.resolve();
  assert.equal(document.getElementById('ocm-device-menu').querySelectorAll('.ocm-device-menu-item').length,0,'failed refresh does not retain stale status');
  const old=deferred(),fresh=deferred();button.dispatch('click');responders.push(old.promise);button.dispatch('click');button.dispatch('click');responders.push(fresh.promise);button.dispatch('click');
  old.resolve({ok:true,json:()=>Promise.resolve({devices:[{device_id:'device-old',name:'Old result',online:true}]})});
  fresh.resolve({ok:true,json:()=>Promise.resolve({devices:[{device_id:'device-new',name:'New result',online:true}]})});
  await settle();
  assert.equal(document.getElementById('ocm-device-menu').querySelector('.ocm-device-menu-item').children[1].textContent,'New result','late response cannot overwrite a reopened menu');
  button.dispatch('click');const hungBody=deferred();responders.push(Promise.resolve({ok:true,json:()=>hungBody.promise}));button.dispatch('click');await settle();
  timers.filter(timer=>timer.ms===10000).at(-1).fn();await settle();
  assert.equal(document.getElementById('ocm-device-menu').querySelectorAll('.ocm-device-menu-item').length,0,'a hung response body reaches the discovery deadline and becomes unknown');
  responders.push(response([{device_id:'device-retry',name:'Retry result',online:true}]));timers.filter(timer=>timer.ms===3000).at(-1).fn();await settle();
  hungBody.resolve({devices:[{device_id:'device-late',name:'Late result',online:true}]});await settle();
  assert.equal(document.getElementById('ocm-device-menu').querySelector('.ocm-device-menu-item').children[1].textContent,'Retry result','a late body cannot overwrite the next refresh');
  document.dispatch('keydown',{key:'Escape',preventDefault(){this.prevented=true}});assert.equal(button.getAttribute('aria-expanded'),'false');assert.equal(global.focused,button);
  responders.push(response([{device_id:'device-a',name:'A',online:true}]));button.dispatch('click');await Promise.resolve();await Promise.resolve();document.dispatch('pointerdown',{target:new Element('div')});assert.equal(button.getAttribute('aria-expanded'),'false');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
