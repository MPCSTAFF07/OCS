'use strict';
// Shared by the loopback dock and GitHub Pages owner management.
window.OCSAV = function(send){
  const get=id=>document.getElementById(id);
  let dirty=false, signature='', rows=new Map();
  const field=get('av-fields');
  field.addEventListener('change',()=>{dirty=true;});
  field.addEventListener('input',()=>{dirty=true;});
  function render(s,busy=false){
    const cfg=s.av, info=s.obs_audio;
    field.disabled=busy||!cfg||!info||!!info.error||!info.available_track;
    if(!cfg||!info){get('av-info').textContent='配信PCのOCS・LuaとAWSの中継を更新すると設定できます。';return;}
    const native=info.native_fps?Number(info.native_fps).toFixed(2).replace(/\.00$/,'')+'FPS':'確認中';
    get('av-info').textContent=info.error||'OBS：'+(info.native_width||'?')+'×'+(info.native_height||'?')+' / '+native+' · 共有専用トラック：'+(info.available_track||'空きなし');
    if(s.performance?.encoded_frames>0){get('av-info').textContent+=' · OCS圧縮：'+s.performance.encoded_fps+'FPS / '+s.performance.encode_ms+'ms';}
    if(!dirty){
      get('av-height').value=cfg.height;get('av-fps').value=cfg.fps;get('av-bitrate').value=cfg.bitrate;
    }
    const sources=info.sources||[];
    const next=JSON.stringify(sources.map(x=>[x.id,x.name,x.muted,x.active]));
    const selected=dirty?new Set([...rows].filter(([,input])=>input.checked).map(([id])=>id)):new Set(cfg.audio_sources);
    if(next!==signature){
      signature=next;rows=new Map();get('audio-sources').replaceChildren();
      for(const source of sources){
        const label=document.createElement('label'),input=document.createElement('input'),name=document.createElement('span');
        label.className='audio-choice';input.type='checkbox';input.checked=selected.has(source.id);
        name.textContent=source.name+(source.muted?'（OBSでミュート中）':!source.active?'（非アクティブ）':'');
        label.append(input,name);get('audio-sources').append(label);rows.set(source.id,input);
      }
    }else if(!dirty){for(const [id,input] of rows)input.checked=selected.has(id);}
    get('av-note').textContent=cfg.audio_mode==='legacy'?'未設定：現在はLuaで指定した音声トラックを使用中です。載せる音源を選んで保存してください。':'チェックした音源だけを共有します。全て外すと無音になります。';
    get('av-save').textContent=s.sharing?'保存して共有を再開始':'映像・音声設定を保存';
  }
  get('av-save').onclick=async()=>{
    const ok=await send({action:'av_config',height:Number(get('av-height').value),fps:Number(get('av-fps').value),bitrate:Number(get('av-bitrate').value),audio_mode:'selected',audio_sources:[...rows].filter(([,input])=>input.checked).map(([id])=>id)});
    if(ok)dirty=false;
  };
  return {render,unavailable(){field.disabled=true;},reset(){dirty=false;signature='';rows.clear();get('audio-sources').replaceChildren();field.disabled=true;}};
};

