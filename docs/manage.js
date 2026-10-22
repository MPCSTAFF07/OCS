'use strict';
const $=id=>document.getElementById(id);
let endpoint='',key='',timer=null,busy=false,generation=0;

async function api(data){
  const controller=new AbortController();
  const timeout=setTimeout(()=>controller.abort(),35000);
  try{
    const r=await fetch(endpoint+'/api/admin',{
      method:data?'POST':'GET',credentials:'omit',redirect:'error',cache:'no-store',referrerPolicy:'no-referrer',
      headers:{Authorization:'Bearer '+key,'Content-Type':'application/json'},
      body:data?JSON.stringify(data):undefined,signal:controller.signal
    });
    if(!r.ok){
      let message=await r.text();
      try{message=JSON.parse(message).error||message;}catch(e){}
      const error=Error(message);error.status=r.status;throw error;
    }
    return r.json();
  }finally{clearTimeout(timeout);}
}
function unavailable(){
  $('badge').textContent='未接続';
  for(const id of ['start','stop','rotate','copy'])$(id).disabled=true;
  $('invite').value='';$('count').textContent='0';$('viewers').replaceChildren();
}
function render(s){
  $('badge').textContent=s.live?'共有中':s.sharing?'映像待機中':'停止中';
  $('message').textContent=s.error||(s.obs==='missing'||s.obs==='unloaded'?'OBSのLuaスクリプトとの接続待ちです。':s.obs.startsWith('error:')?s.obs:s.live?'本番映像を共有しています。':s.sharing?'OBSからの映像を待っています。':'共有を開始できます。');
  $('start').disabled=busy||s.sharing;$('stop').disabled=busy||!s.sharing;
  $('rotate').disabled=busy;$('copy').disabled=!s.invite_url;
  $('invite').value=s.invite_url;$('connection').textContent='配信PC：'+s.public_state+' / OBS：'+s.obs;
  $('count').textContent=s.viewers.filter(v=>v.state==='connected').length;
  $('viewers').replaceChildren();
  for(const v of s.viewers){
    const li=document.createElement('li'),name=document.createElement('span'),button=document.createElement('button');
    name.textContent=v.name+' · '+(v.state==='connected'?'視聴中':'接続中');
    button.textContent='切断';button.disabled=busy;button.onclick=()=>action({action:'disconnect',id:v.id});
    li.append(name,button);$('viewers').append(li);
  }
  if(!s.viewers.length){const li=document.createElement('li');li.textContent='接続者はいません';$('viewers').append(li);}
}
function logOut(){
  generation++;clearTimeout(timer);timer=null;key='';endpoint='';busy=false;
  unavailable();$('panel').hidden=true;$('login').hidden=false;$('connect').disabled=false;
  $('admin-key').value='';$('connection').textContent='';$('message').textContent='';$('error').textContent='';
}
function errorMessage(e){
  if(e.name==='AbortError')return '接続がタイムアウトしました。配信PCと公開サーバーを確認してください。';
  if(e instanceof TypeError)return '公開サーバーへ接続できません。中継URL・HTTPS・CORS設定を確認してください。';
  return e.message;
}
async function poll(){
  if(!key||busy)return;
  const version=generation;
  try{
    const state=await api();if(version!==generation)return;
    render(state);$('error').textContent='';
  }catch(e){
    if(version!==generation)return;
    if(e.status===401)logOut();else unavailable();
    $('error').textContent=errorMessage(e);
  }finally{
    if(key&&version===generation){clearTimeout(timer);timer=setTimeout(poll,2000);}
  }
}
async function action(data){
  if(busy||!key)return;
  busy=true;const version=++generation;clearTimeout(timer);unavailable();
  try{const state=await api(data);if(version===generation){busy=false;render(state);$('error').textContent='';}}
  catch(e){if(version===generation){if(e.status===401)logOut();$('error').textContent=errorMessage(e);}}
  finally{if(version===generation){busy=false;poll();}}
}
$('login').onsubmit=async e=>{
  e.preventDefault();
  const enteredKey=$('admin-key').value.trim();
  let url;
  try{
    url=new URL($('relay').value.trim());
    if(url.protocol!=='https:'||url.username||url.password||url.search||url.hash)throw Error();
    if(enteredKey.length<32)throw Error();
  }catch(e){$('error').textContent='中継URLはhttps://、管理キーはAWS側のadmin_keyを入力してください。';return;}
  generation++;clearTimeout(timer);endpoint=url.href.replace(/\/$/,'');key=enteredKey;
  $('admin-key').value='';$('login').hidden=true;$('panel').hidden=false;$('error').textContent='';
  unavailable();await poll();
};
$('start').onclick=()=>action({action:'start'});$('stop').onclick=()=>action({action:'stop'});
$('rotate').onclick=()=>{if(confirm('招待URLを更新し、現在の接続者を切断しますか？'))action({action:'rotate'});};
$('copy').onclick=async()=>{
  try{await navigator.clipboard.writeText($('invite').value);}
  catch(e){$('invite').select();if(!document.execCommand('copy'))$('error').textContent='招待URLを選択してコピーしてください。';}
};
$('logout').onclick=logOut;
window.addEventListener('pagehide',()=>{generation++;key='';clearTimeout(timer);});
