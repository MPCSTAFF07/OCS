"""Selection validation, persistence, FPS pacing, and Lua track isolation."""
import json
import sys
import time
from fractions import Fraction
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from av import VideoFrame
from aiortc import MediaStreamTrack
from lupa import LuaRuntime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Studio, MonitorTrack, create_app, validate_av

SETTINGS = dict(height=1080, fps=60, bitrate=8000, audio_mode='selected', audio_sources=['game'])


@pytest.mark.asyncio
async def test_av_api_validation_persistence_and_restart(tmp_path):
    s = Studio(tmp_path)
    (s.runtime/'obs-status.txt').write_text(f'{time.time()}\nidle')
    (s.runtime/'obs-details.json').write_text(json.dumps(dict(timestamp=time.time(), available_track=6, error='', sources=[dict(id='game',name='ゲーム')])) )
    async def opened(): pass
    s.open_media = opened
    async with TestClient(TestServer(create_app(s, bridge_enabled=False))) as c:
        headers = {'Authorization':'Bearer '+s.admin}
        assert (await c.post('/api/admin', json={'action':'av_config', **SETTINGS})).status == 401
        for field, value in [('height',1440), ('fps',120), ('fps',True), ('bitrate',15000), ('audio_sources','game'), ('audio_sources',[4])]:
            assert (await c.post('/api/admin',headers=headers,json={'action':'av_config',**SETTINGS,field:value})).status==400
        assert (await c.post('/api/admin',headers=headers,json={'action':'av_config',**SETTINGS,'audio_sources':['deleted']})).status==409
        await s.start()
        response = await c.post('/api/admin',headers=headers,json={'action':'av_config',**SETTINGS})
        assert response.status==200
        assert s.sharing and s.av==SETTINGS
        await s.open_task
        saved=json.loads((s.runtime/'av-settings.json').read_text())
        assert saved['sources']==[{'id':'game'}]
        s.public={'publisher_key':'p'*40}
        s.save_config()
        reloaded=Studio(tmp_path)
        assert reloaded.av==SETTINGS and reloaded.public['publisher_key']=='p'*40
        # Stale/missing Lua cannot accept selection and silently fall back to mixed audio.
        (s.runtime/'obs-details.json').unlink()
        assert (await c.post('/api/admin',headers=headers,json={'action':'av_config',**SETTINGS})).status==409


@pytest.mark.asyncio
async def test_frame_cap_keeps_pts_and_never_duplicates_frames(tmp_path):
    class Source(MediaStreamTrack):
        kind='video'
        def __init__(self, fps):
            super().__init__();self.pts=-1;self.fps=fps
        async def recv(self):
            self.pts+=1
            frame=VideoFrame(16,16,'yuv420p');frame.pts=self.pts;frame.time_base=Fraction(1,self.fps)
            return frame
    s=Studio(tmp_path);s.av['fps']=30
    track=MonitorTrack(Source(60),s)
    frames=[await track.recv() for _ in range(31)]
    assert [x.pts for x in frames]==list(range(0,61,2))
    s.av['fps']=60
    track=MonitorTrack(Source(30),s)
    frames=[await track.recv() for _ in range(31)]
    assert [x.pts for x in frames]==list(range(31))


def lua_harness(tmp_path):
    lua=LuaRuntime(unpack_returned_tuples=True)
    def to_lua(x):
        if isinstance(x,dict):return lua.table_from({k:to_lua(v) for k,v in x.items()})
        if isinstance(x,list):return lua.table_from([to_lua(v) for v in x])
        return x
    def to_python(x):
        if hasattr(x,'items'):
            items=dict(x.items())
            if items and all(isinstance(k,int) for k in items):return [to_python(items[i]) for i in sorted(items)]
            return {k:to_python(v) for k,v in items.items()}
        return x
    lua.globals().test_root=str(tmp_path)+'/'
    lua.globals().decode=lambda s:to_lua(json.loads(s))
    lua.globals().encode=lambda x:json.dumps(to_python(x),ensure_ascii=False)
    lua.execute('''
      function script_path() return test_root end
      captured={now=100,active=false};os.time=function() return captured.now end
      sources={{id="game",name="ゲーム",mask=63},{id="voice",name="実況・解説",mask=63}}
      cfg={Output={Mode="Advanced"},AdvOut={TrackIndex=1,VodTrackIndex=2,RecTracks=1}}
      obslua={OBS_SOURCE_AUDIO=2,LOG_INFO=1,LOG_ERROR=2,
        obs_data_create=function() return {} end, obs_data_release=function() end,
        obs_data_create_from_json=decode,obs_data_get_json=encode,
        obs_data_set_string=function(s,k,v) s[k]=v end,obs_data_set_int=function(s,k,v) s[k]=v end,
        obs_data_set_bool=function(s,k,v) s[k]=v end,obs_data_set_double=function(s,k,v) s[k]=v end,
        obs_data_get_int=function(s,k) return s[k] or 0 end,obs_data_get_string=function(s,k) return s[k] or "" end,
        obs_data_get_array=function(s,k) return s[k] end,obs_data_set_array=function(s,k,v) s[k]=v end,
        obs_data_array_create=function() return {} end,obs_data_array_release=function() end,
        obs_data_array_push_back=function(a,v) table.insert(a,v) end,
        obs_data_array_count=function(a) return #a end,obs_data_array_item=function(a,i) return a[i+1] end,
        obs_frontend_get_profile_config=function() return cfg end,
        config_get_uint=function(c,s,k) return c[s] and c[s][k] or 0 end,
        config_get_string=function(c,s,k) return c[s] and c[s][k] or "" end,
        obs_enum_sources=function() return sources end,source_list_release=function() end,
        obs_source_get_output_flags=function() return 2 end,obs_source_get_uuid=function(s) return s.id end,
        obs_source_get_name=function(s) return s.name end,obs_source_get_audio_mixers=function(s) return s.mask end,
        obs_source_set_audio_mixers=function(s,m) s.mask=m end,obs_source_muted=function() return false end,
        obs_source_active=function() return true end,
        obs_get_signal_handler=function() return {} end,
        signal_handler_connect=function(h,n,f) captured.created=f end,signal_handler_disconnect=function() end,
        calldata_source=function(c,n) return c[n] end,
        obs_video_info=function() return {fps_num=60,fps_den=1,output_width=1920,output_height=1080} end,
        obs_get_video_info=function() return true end,
        obs_output_create=function(id,name,s) captured.settings=s;return {} end,
        obs_output_set_media=function() end,obs_get_video=function() end,obs_get_audio=function() end,
        obs_output_set_mixer=function(o,m) captured.mixer=m end,
        obs_output_start=function() captured.started=true;return true end,
        obs_output_force_stop=function() captured.stopped=true end,obs_output_release=function() end,
        obs_output_active=function() return captured.active end,obs_output_get_last_error=function() return "" end,
        script_log=function() end,timer_add=function(f) captured.poll=f end,timer_remove=function() end
      }
    ''')
    lua.execute((Path(__file__).resolve().parents[1]/'caster-share.lua').read_text())
    return lua


def test_lua_only_selected_source_and_preserves_stream_record_tracks(tmp_path):
    runtime=tmp_path/'runtime';runtime.mkdir()
    (runtime/'av-settings.json').write_text(json.dumps({**SETTINGS,'sources':[{'id':'game'}]}))
    lua=lua_harness(tmp_path);lua.globals().script_load(lua.table_from({'track':2,'quality':0}))
    c=lua.globals().captured
    (runtime/'command.txt').write_text('1\nstart_av\n');c.poll()
    assert c.started and c.mixer==5
    assert c.settings.scale_width==1920 and c.settings.scale_height==1080 and c.settings.video_bitrate==8000
    assert lua.globals().sources[1].mask==63
    assert lua.globals().sources[2].mask==31  # Only reserved track 6 removed; tracks 1..5 untouched.
    # A newly created voice source is excluded immediately, before periodic polling.
    lua.execute('table.insert(sources,{id="new",name="new voice",mask=63});captured.created({source=sources[3]})')
    assert lua.globals().sources[3].mask==31
    # Preserve unrelated user edits when releasing the reserved bit.
    lua.globals().sources[2].mask=30
    (runtime/'command.txt').write_text('2\nstop\n');c.poll()
    assert lua.globals().sources[2].mask==62
    assert lua.globals().sources[3].mask==63
    # None selected means silence on the sharing track, not a fallback to all sources.
    (runtime/'av-settings.json').write_text(json.dumps({**SETTINGS,'sources':[]}))
    (runtime/'command.txt').write_text('3\nstart_av\n');c.poll()
    assert all(s.mask<32 for s in lua.globals().sources.values())
    # If recording starts using the reserved track, stop sharing and restore it.
    lua.execute('cfg.AdvOut.RecTracks=33')
    c.active=True;c.now=101;c.poll()
    assert c.stopped
    assert '共有用トラック' in (runtime/'obs-status.txt').read_text()
    # Exhausted tracks never change source routing or begin a new output.
    lua.execute('cfg.AdvOut.RecTracks=63;captured.started=false')
    masks=[s.mask for s in lua.globals().sources.values()]
    (runtime/'command.txt').write_text('4\nstart_av\n');c.poll()
    assert not c.started and masks==[s.mask for s in lua.globals().sources.values()]
    assert '空き音声トラック' in (runtime/'obs-status.txt').read_text()
    # Missing configuration must never fall back to the legacy mixed track.
    (runtime/'av-settings.json').unlink()
    (runtime/'command.txt').write_text('5\nstart_av\n');c.poll()
    assert not c.started and '設定ファイルがありません' in (runtime/'obs-status.txt').read_text()


@pytest.mark.asyncio
async def test_1080p60_webrtc_decodes_frames_without_30fps_quantization(tmp_path, monkeypatch):
    """Real encoder, RTP transport, decoder and 90kHz timestamps at 60fps."""
    import asyncio
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from aiortc.contrib.media import MediaRelay
    from aiortc.codecs import vpx
    monkeypatch.setattr('aioice.ice.get_host_addresses',lambda **kwargs:['127.0.0.1'])
    class FullHD(MediaStreamTrack):
        kind='video'
        def __init__(self):super().__init__();self.index=0;self.started=None
        async def recv(self):
            if self.started is None:self.started=time.monotonic()
            await asyncio.sleep(max(0,self.started+self.index/60-time.monotonic()))
            frame=VideoFrame(1920,1080,'yuv420p')
            for plane in frame.planes:plane.update(bytes([32+self.index%64 if plane==frame.planes[0] else 128])*plane.buffer_size)
            frame.pts=self.index*1500;frame.time_base=Fraction(1,90000);self.index+=1
            return frame
    s=Studio(tmp_path);s.sharing=True;s.av.update(SETTINGS)
    s.tracks=[FullHD()];s.last_frame['video']=time.monotonic()
    pc=RTCPeerConnection(RTCConfiguration(iceServers=[]));received={}
    pc.on('track',lambda t:received.update({t.kind:t}))
    try:
        async with TestClient(TestServer(create_app(s,bridge_enabled=False))) as c:
            pc.addTransceiver('video',direction='recvonly')
            await pc.setLocalDescription(await pc.createOffer())
            response=await c.post('/api/offer',headers={'Authorization':'Bearer '+s.invite},json=dict(type='offer',sdp=pc.localDescription.sdp))
            assert response.status==200,await response.text()
            answer=await response.json()
            assert 'VP8/90000' in answer['sdp']
            await pc.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'],type='answer'))
            frames=[await asyncio.wait_for(received['video'].recv(),10) for _ in range(12)]
            assert all((f.width,f.height)==(1920,1080) for f in frames)
            deltas=[b.pts-a.pts for a,b in zip(frames,frames[1:])]
            # Unbuffered relay may skip a frame during encoder startup. VP8's
            # default microsecond time base introduces <=1 RTP tick rounding.
            assert all(delta>0 for delta in deltas)
            assert sum(abs(delta-1500)<=1 for delta in deltas)>=8
            assert 90000 * (len(frames)-1) / (frames[-1].pts-frames[0].pts)>30
            assert all(f.time_base==Fraction(1,90000) for f in frames)
            # Network feedback can lower quality; high resolution must not stay capped at the old 1.5/3 Mbps.
            assert vpx.MAX_BITRATE==12_000_000
    finally:
        await pc.close()
