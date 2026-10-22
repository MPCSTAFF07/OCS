import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const code=fs.readFileSync(new URL('../web/dock.js',import.meta.url),'utf8');
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function setup(){
  const nodes=new Map();
  function node(){return {value:'',textContent:'',disabled:false,listeners:{},addEventListener(type,fn){this.listeners[type]=fn;},replaceChildren(){},append(){},select(){}};}
  const get=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};
  const state={live:false,sharing:false,error:'',obs:'idle',invite_url:'',public_state:'未設定',public:{},viewers:[]};
  let tick,fail=false,deferGet=null;const posts=[];
  const ctx={window:{OCSAV:()=>({render(){},unavailable(){},reset(){}})},document:{getElementById:get,createElement:node,activeElement:null},location:{hash:'#test',href:'http://localhost/dock#test'},navigator:{},confirm:()=>true,setInterval:fn=>{tick=fn;},fetch:async(url,opt)=>{
    if(opt.method==='POST'){
      const data=JSON.parse(opt.body);posts.push(data);
      if(fail)return {ok:false,text:async()=> 'validation failed'};
      if(data.action==='config')state.public={relay_url:data.relay_url,viewer_url:data.viewer_url};
    }else if(deferGet){const deferred=deferGet;deferGet=null;return deferred;}
    const snapshot=structuredClone(state);
    return {ok:true,json:async()=>snapshot};
  }};
  vm.runInNewContext(code,ctx);
  return {get,state,posts,ctx,tick:()=>tick(),fail:()=>{fail=true;},defer:promise=>{deferGet=promise;}};
}
function input(h,id,value){h.get(id).value=value;h.get(id).listeners.input?.();h.ctx.document.activeElement=h.get(id);}

test('editing survives polls while moving between fields and saves both URLs',async()=>{
  const h=setup();await settle();
  input(h,'relay','https://mpc-official.duckdns.org/caster');
  input(h,'viewer','https://mpcstaff07.github.io/OCS/');
  h.ctx.document.activeElement=null;await h.tick();
  assert.equal(h.get('relay').value,'https://mpc-official.duckdns.org/caster');
  assert.equal(h.get('viewer').value,'https://mpcstaff07.github.io/OCS/');
  h.get('key').value='private-key';await h.get('save').onclick();await settle();
  assert.equal(h.posts[0].relay_url,'https://mpc-official.duckdns.org/caster');
  assert.equal(h.posts[0].viewer_url,'https://mpcstaff07.github.io/OCS/');
  assert.equal(h.get('key').value,'');
});

test('failed save preserves entered URLs and publisher key',async()=>{
  const h=setup();await settle();h.fail();
  input(h,'relay','https://relay.example/caster');input(h,'viewer','https://pages.example/OCS/');
  h.get('key').value='keep-me';await h.get('save').onclick();await settle();await h.tick();
  assert.equal(h.get('relay').value,'https://relay.example/caster');
  assert.equal(h.get('viewer').value,'https://pages.example/OCS/');
  assert.equal(h.get('key').value,'keep-me');
  assert.equal(h.get('error').textContent,'validation failed');
});

test('late poll cannot overwrite state after a save',async()=>{
  const h=setup();await settle();
  let release;h.defer(new Promise(resolve=>{release=resolve;}));const poll=h.tick();
  input(h,'relay','https://relay.example/caster');input(h,'viewer','https://pages.example/OCS/');
  await h.get('save').onclick();await settle();
  release({ok:true,json:async()=>({...h.state,public:{}})});await poll;
  assert.equal(h.get('relay').value,'https://relay.example/caster');
});
