'use strict';
const $ = id => document.getElementById(id);
const avControls = window.OCSAV(data=>act(data));
const token = location.hash.slice(1);
let busy = false;
let revision = 0;
const dirty = new Set();
for(const id of ['relay','viewer']) $(id).addEventListener('input',()=>dirty.add(id));
$('dockurl').value = location.href;
async function api(data) {
  const r = await fetch('/api/admin', {method:data?'POST':'GET', headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:data?JSON.stringify(data):undefined});
  if(!r.ok) throw Error(await r.text());
  return r.json();
}
function render(s) {
  avControls.render(s,busy);
  $('badge').textContent = s.live ? '共有中' : s.sharing ? '映像待機中' : '停止中';
  $('start').disabled = busy || s.sharing;
  $('stop').disabled = busy || !s.sharing;
  $('message').textContent = s.error || (s.obs==='missing'||s.obs==='unloaded' ? 'OBSでcaster-share.luaを追加してください。' : s.obs.startsWith('error:') ? s.obs : s.live ? '本番映像を共有しています。' : s.sharing ? 'OBSからの映像を待っています。' : '共有を開始できます。');
  $('invite').value = s.invite_url;
  $('copy').disabled = !s.invite_url;
  $('publicstate').textContent='公開サーバー：'+s.public_state;
  if(!dirty.has('relay')&&document.activeElement!==$('relay'))$('relay').value=s.public.relay_url||'';
  if(!dirty.has('viewer')&&document.activeElement!==$('viewer'))$('viewer').value=s.public.viewer_url||'';
  $('count').textContent=s.viewers.filter(v=>v.state==='connected').length;
  $('viewers').replaceChildren();
  for(const v of s.viewers){
    const li=document.createElement('li'), label=document.createElement('span'), b=document.createElement('button');
    label.textContent=v.name+' · '+(v.state==='connected'?'視聴中':'接続中');
    b.textContent='切断';b.onclick=()=>act({action:'disconnect',id:v.id});li.append(label,b);$('viewers').append(li);
  }
  if(!s.viewers.length){const li=document.createElement('li');li.textContent='接続者はいません';$('viewers').append(li);}
}
async function act(data){if(busy)return false;busy=true;avControls.unavailable();revision++;$('save').disabled=true;$('error').textContent='';try{render(await api(data));return true;}catch(e){$('error').textContent=e.message;return false;}finally{busy=false;$('save').disabled=false;refresh();}}
async function refresh(){if(busy)return;const current=++revision;try{const state=await api();if(busy||current!==revision)return;render(state);if($('error').textContent.startsWith('管理画面に接続できません'))$('error').textContent='';}catch(e){if(!busy&&current===revision)$('error').textContent='管理画面に接続できません。START.cmdの起動と管理URLを確認してください。';}}
async function copy(id){const input=$(id);input.select();try{await navigator.clipboard.writeText(input.value);}catch(e){if(!document.execCommand('copy')){$('error').textContent='URLを選択してコピーしてください。';}}}
$('start').onclick=()=>act({action:'start'});$('stop').onclick=()=>act({action:'stop'});
$('rotate').onclick=()=>{if(confirm('招待URLを更新し、現在の接続者を切断しますか？'))act({action:'rotate'});};
$('save').onclick=async()=>{const ok=await act({action:'config',relay_url:$('relay').value,viewer_url:$('viewer').value,publisher_key:$('key').value});if(ok){dirty.clear();$('key').value='';refresh();}};$('copy').onclick=()=>copy('invite');$('copydock').onclick=()=>copy('dockurl');
refresh();setInterval(refresh,1500);
