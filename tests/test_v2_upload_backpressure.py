import subprocess

from src.static_adapter import TRANSPORT_ADAPTER


def test_large_relay_upload_obeys_consumer_backpressure():
    """切换 Relay 后也不能由 start 循环把源流一口气读完。"""
    adapter=TRANSPORT_ADAPTER.split('<script id="ocm-transport-adapter">',1)[1].split('</script>',1)[0].replace('__OCM_VERSION_JSON__','"test"')
    script=r'''
    const assert=require('node:assert/strict');
    global.window=global;
    global.location={origin:'https://mesh.test',host:'mesh.test',pathname:'/',href:'https://mesh.test/'};
    global.document={readyState:'loading',addEventListener(){}};
    global.history={pushState(){},replaceState(){}};
    global.localStorage={getItem(){return null}};
    global.addEventListener=()=>{};
    global.setTimeout=global.setInterval=()=>0;
    global.WebSocket=class {};
    let produced=0,complete=false;
    global.fetch=async request=>{
      if(!(request instanceof Request)) return new Promise(()=>{});
      const before=produced;
      for(let i=0;i<10;i++) await new Promise(setImmediate);
      assert.ok(produced<=before+1,`eager buffering: ${before} -> ${produced}`);
      const reader=request.body.getReader();let bytes=0,index=0;
      while(true){const {value,done}=await reader.read();if(done)break;
        assert.equal(value[0],index++);bytes+=value.length;
      }
      assert.equal(bytes,80*512*1024);assert.equal(index,80);
      return new Response(null,{status:204});
    };
    '''+adapter+r'''
    process.on('beforeExit',()=>assert.ok(complete));
    (async()=>{
      const s=window.__ocmTransport;s.manifest={device_id:'device-a'};
      s.channel={readyState:'open',bufferedAmount:0,send(){assert.fail('large body sent P2P')}};
      const body=new ReadableStream({pull(c){
        if(produced===80){c.close();return}
        const chunk=new Uint8Array(512*1024);chunk[0]=produced++;c.enqueue(chunk);
      }});
      const response=await fetch('https://mesh.test/_mesh/device/device-a/api/upload',{method:'POST',body,duplex:'half'});
      assert.equal(response.status,204);
    })().then(()=>{complete=true}).catch(e=>{complete=true;console.error(e);process.exitCode=1});
    '''
    result=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
