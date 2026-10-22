'use strict';
const $=id=>document.getElementById(id);
const fragment=new URLSearchParams(location.hash.slice(1));
const token=fragment.get('token')||'', rawApi=fragment.get('api')||'';
let apiBase='';
try{const u=new URL(rawApi);if(u.protocol==='https:'&&!u.username&&!u.password&&!u.search&&!u.hash)apiBase=u.href.replace(/\/$/,'');}catch(e){}
let pc=null, session=null, wanted=false, connecting=false, retry=null, generation=0, statsTimer=null;
const video=$('video');
function show(text){$('status').textContent=text;$('overlay').textContent=text==='視聴中'?'':text;}
async function api(path,data){const r=await fetch(apiBase+'/api/'+path,{method:data?'POST':'GET',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:data?JSON.stringify(data):undefined});if(!r.ok){const e=Error(await r.text());e.status=r.status;throw e;}return r.json();}
async function dispose(){const old=pc,id=session;pc=null;session=null;clearInterval(statsTimer);if(old)old.close();video.srcObject=null;if(id){try{await api('leave',{id});}catch(e){}}}
function again(){if(wanted&&!retry)retry=setTimeout(()=>{retry=null;connect();},2500);}
async function iceReady(p){if(p.iceGatheringState==='complete')return;await new Promise((resolve,reject)=>{const t=setTimeout(()=>{p.removeEventListener('icegatheringstatechange',changed);reject(Error('接続情報の収集がタイムアウトしました。'));},20000);function changed(){if(p.iceGatheringState==='complete'){clearTimeout(t);p.removeEventListener('icegatheringstatechange',changed);resolve();}}p.addEventListener('icegatheringstatechange',changed);});}
async function connect(){
  if(!wanted)return;if(connecting){again();return;}connecting=true;const gen=generation;
  try{
    await dispose();
    if(!wanted||gen!==generation)return;
    const state=await api('status');
    if(!wanted||gen!==generation)return;
    if(!state.live){show(state.sharing?'映像の到着待ち':'共有開始待ち');$('message').textContent=state.error||'共有が始まると自動で接続します。';again();return;}
    show('接続中');
    const p=new RTCPeerConnection({iceServers:state.ice_servers||[],iceTransportPolicy:'relay'});pc=p;
    const stream=new MediaStream();video.srcObject=stream;
    p.addTransceiver('video',{direction:'recvonly'});p.addTransceiver('audio',{direction:'recvonly'});
    p.ontrack=e=>{stream.addTrack(e.track);video.play().catch(()=>{$('message').textContent='音声を再生するには映像をクリックしてください。';});};
    p.onconnectionstatechange=()=>{
      if(p!==pc)return;
      if(p.connectionState==='connected'){show('視聴中');$('message').textContent='OBSの本番映像を受信しています。';}
      if(['failed','disconnected','closed'].includes(p.connectionState)&&wanted){show('再接続中');again();}
    };
    await p.setLocalDescription(await p.createOffer());await iceReady(p);
    if(!wanted||gen!==generation)return;
    const answer=await api('offer',{sdp:p.localDescription.sdp,type:'offer',name:$('name').value.trim()||'実況解説'});
    if(!wanted||gen!==generation){api('leave',{id:answer.id}).catch(()=>{});return;}
    session=answer.id;await p.setRemoteDescription(answer);
    statsTimer=setInterval(async()=>{
      try{const stats=await p.getStats();let line='';stats.forEach(r=>{if(r.type==='inbound-rtp'&&r.kind==='video')line=`${r.frameWidth||'—'}×${r.frameHeight||'—'} / ${r.framesPerSecond||'—'} fps`;});$('stats').textContent=line;}catch(e){}
    },2000);
  }catch(e){
    if(gen!==generation||!wanted)return;
    if(e.status===401){wanted=false;$('join').disabled=false;$('leave').disabled=true;show('招待URLを確認してください');}
    else{show('再接続待ち');again();}
    $('message').textContent=e.message;
  }finally{connecting=false;}
}
$('join').onclick=()=>{if(wanted)return;wanted=true;generation++;$('join').disabled=true;$('leave').disabled=false;connect();};
$('leave').onclick=async()=>{wanted=false;generation++;clearTimeout(retry);retry=null;await dispose();show('退出しました');$('join').disabled=false;$('leave').disabled=true;$('message').textContent='再び視聴するには視聴開始を押してください。';$('stats').textContent='';};
$('volume').oninput=e=>{video.volume=Number(e.target.value);};video.onclick=()=>video.play().catch(()=>{});
$('full').onclick=()=>{(video.requestFullscreen?video.requestFullscreen():Promise.reject()).catch(()=>{$('message').textContent='このブラウザでは全画面を開始できません。';});};
window.addEventListener('pagehide',()=>{wanted=false;if(pc)pc.close();if(session)fetch(apiBase+'/api/leave',{method:'POST',keepalive:true,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify({id:session})}).catch(()=>{});});
if(!token||!apiBase){$('join').disabled=true;show('招待URLから開いてください');}
