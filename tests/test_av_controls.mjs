import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const code=fs.readFileSync(new URL('../web/av-controls.js',import.meta.url),'utf8');
function setup(){
  const nodes=new Map(),sent=[];
  function node(){return {value:'',textContent:'',disabled:false,children:[],listeners:{},addEventListener(t,f){this.listeners[t]=f;},append(...x){this.children.push(...x);},replaceChildren(){this.children=[];}};}
  const get=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};
  const ctx={window:{},document:{getElementById:get,createElement:node}};vm.runInNewContext(code,ctx);
  let success=true;
  const controls=ctx.window.OCSAV(async data=>{sent.push(JSON.parse(JSON.stringify(data)));return success;});
  const state={sharing:true,av:{height:1080,fps:60,bitrate:8000,audio_mode:'selected',audio_sources:['game']},obs_audio:{available_track:6,error:'',native_fps:60,native_width:1920,native_height:1080,sources:[{id:'game',name:'ゲーム',active:true},{id:'voice',name:'実況・解説',active:true}]}};
  return {get,sent,controls,state,fail(){success=false;}};
}
test('source selection survives refreshed labels and polling, empty selection remains silent',async()=>{
  const h=setup();h.controls.render(h.state);
  assert.equal(h.get('av-fields').disabled,false);
  const inputs=()=>h.get('audio-sources').children.map(label=>label.children[0]);
  assert.deepEqual(inputs().map(x=>x.checked),[true,false]);
  inputs()[0].checked=false;h.get('av-fields').listeners.change();h.get('av-fps').value='30';
  h.state.obs_audio.sources[1].muted=true;h.controls.render(h.state);
  assert.deepEqual(inputs().map(x=>x.checked),[false,false]);assert.equal(h.get('av-fps').value,'30');
  await h.get('av-save').onclick();
  assert.deepEqual(h.sent[0],{action:'av_config',height:1080,fps:30,bitrate:8000,audio_mode:'selected',audio_sources:[]});
});
test('old Lua, disconnected manager and missing free track cannot save',()=>{
  const h=setup();h.controls.render({});assert.equal(h.get('av-fields').disabled,true);
  h.controls.render(h.state);assert.equal(h.get('av-fields').disabled,false);
  h.controls.unavailable();assert.equal(h.get('av-fields').disabled,true);
  h.state.obs_audio.available_track=0;h.controls.render(h.state);assert.equal(h.get('av-fields').disabled,true);
  h.controls.reset();assert.equal(h.get('audio-sources').children.length,0);
});
