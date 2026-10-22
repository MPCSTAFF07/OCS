import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../docs/manage.js',import.meta.url),'utf8');
function setup(){
  const nodes=new Map(),requests=[],timers=new Map();let sequence=0,status=200,deferred=null;
  const state={live:false,sharing:false,error:'',obs:'idle',invite_url:'https://pages.example/#token=invite',public_state:'接続済み',viewers:[]};
  const make=()=>({value:'',textContent:'',hidden:false,disabled:false,children:[],replaceChildren(){this.children=[];},append(...items){this.children.push(...items);},select(){}});
  const get=id=>{if(!nodes.has(id))nodes.set(id,make());return nodes.get(id);};
  const context={URL,AbortController,console,document:{getElementById:get,createElement:make,execCommand:()=>true},
    navigator:{clipboard:{writeText:async()=>{}}},window:{addEventListener(){},OCSAV:()=>({render(){},unavailable(){},reset(){}})},confirm:()=>true,
    setTimeout:(fn,delay)=>{const id=++sequence;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id),
    fetch:async(url,options)=>{
      requests.push({url,...options});
      if(deferred){const promise=deferred;deferred=null;return promise;}
      if(status!==200)return {ok:false,status,text:async()=>status===401?'管理キーが一致しません':'配信PCが未接続です。'};
      if(options.body){const action=JSON.parse(options.body).action;if(action==='start')state.sharing=true;if(action==='stop')state.sharing=false;}
      const snapshot=structuredClone(state);return {ok:true,json:async()=>snapshot};
    }};
  vm.runInNewContext(source,context);
  async function login(){get('relay').value='https://relay.example/caster';get('admin-key').value='a'.repeat(43);await get('login').onsubmit({preventDefault(){}});}
  return {get,requests,timers,state,login,status:code=>{status=code;},defer:promise=>{deferred=promise;}};
}

test('management requires login and keeps credentials out of URLs',async()=>{
  const h=setup();assert.equal(h.requests.length,0);await h.login();
  assert.equal(h.requests[0].url,'https://relay.example/caster/api/admin');
  assert.equal(h.requests[0].headers.Authorization,'Bearer '+'a'.repeat(43));
  assert.equal(h.get('admin-key').value,'');assert.equal(h.get('login').hidden,true);
  assert.equal(h.get('start').disabled,false);
  await h.get('start').onclick();assert.equal(h.state.sharing,true);
  await h.get('stop').onclick();assert.equal(h.state.sharing,false);
  h.get('logout').onclick();await new Promise(resolve=>setImmediate(resolve));assert.equal(h.get('panel').hidden,true);assert.equal(h.get('invite').value,'');
  assert.equal(h.timers.size,0);
});

test('wrong management key returns to login; offline PC keeps controls disabled',async()=>{
  const h=setup();h.status(401);await h.login();
  assert.equal(h.get('login').hidden,false);assert.match(h.get('error').textContent,/管理キー/);
  h.status(503);await h.login();assert.equal(h.get('start').disabled,true);assert.equal(h.get('stop').disabled,true);
  assert.match(h.get('error').textContent,/配信PC/);
});

test('logging out while a status request is pending prevents old responses showing again',async()=>{
  const h=setup();let release;h.defer(new Promise(resolve=>{release=resolve;}));
  const pending=h.login();h.get('logout').onclick();
  release({ok:true,json:async()=>h.state});await pending;
  assert.equal(h.get('panel').hidden,true);assert.equal(h.get('invite').value,'');assert.equal(h.timers.size,0);
});
