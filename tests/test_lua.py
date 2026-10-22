"""Lua executes against an OBS API stub; this does not replace testing real OBS."""
import time
from pathlib import Path
from lupa import LuaRuntime


def test_lua_start_stop_mixer_and_quality(tmp_path):
    runtime=tmp_path/'runtime';runtime.mkdir()
    lua=LuaRuntime(unpack_returned_tuples=True)
    lua.globals().test_root=str(tmp_path)+'/'
    lua.execute('''
      function script_path() return test_root end
      captured = {now=100, active=false}
      os.time=function() return captured.now end
      obslua = {
        obs_data_create=function() return {} end,
        obs_data_release=function() end,
        obs_data_set_string=function(s,k,v) s[k]=v end,
        obs_data_set_int=function(s,k,v) s[k]=v end,
        obs_data_get_int=function(s,k) return s[k] end,
        obs_output_create=function(id,name,s) captured.settings=s; captured.id=id; return {} end,
        obs_output_set_media=function() captured.media=true end,
        obs_get_video=function() return {} end, obs_get_audio=function() return {} end,
        obs_output_set_mixer=function(o,m) captured.mixer=m end,
        obs_output_start=function(o) captured.started=true; return true end,
        obs_output_force_stop=function() captured.stopped=true end,
        obs_output_release=function() captured.released=true end,
        obs_output_active=function() return captured.active end,
        obs_output_get_last_error=function() return "" end,
        timer_add=function(f,ms) captured.poll=f end, timer_remove=function() end
      }
    ''')
    lua.execute((Path(__file__).resolve().parents[1]/'caster-share.lua').read_text())
    lua.globals().script_update(lua.table_from({'track':2,'quality':1}))
    lua.globals().script_load(lua.table())
    (runtime/'command.txt').write_text('1\nstart\n')
    lua.globals().captured.poll()
    c=lua.globals().captured
    assert not c.stopped
    assert (runtime/'obs-status.txt').read_text().splitlines()[1]=='starting'
    c.now=101;c.active=True;c.poll()
    assert (runtime/'obs-status.txt').read_text().splitlines()[1]=='active'
    assert c.id=='ffmpeg_output' and c.started and c.media
    assert c.mixer==1
    assert c.settings['scale_width']==1280
    assert c.settings['format_name']=='mpegts'
    (runtime/'command.txt').write_text('2\nstop\n')
    c.poll()
    assert c.stopped and c.released
    # A fresh startup must wait, then report a useful error if initialization never completes.
    c.stopped=False;c.released=False;c.active=False;c.now=200
    (runtime/'command.txt').write_text('3\nstart\n');c.poll()
    c.stopped=False;c.released=False;c.now=202;c.poll()
    assert not c.stopped
    c.now=204;c.poll()
    assert c.stopped and c.released
    assert '出力を開始できませんでした' in (runtime/'obs-status.txt').read_text()
    # Once active, an unexpected stop must not get another startup grace window.
    c.now=300;c.active=True;(runtime/'command.txt').write_text('4\nstart\n');c.poll()
    c.stopped=False;c.active=False;c.now=301;c.poll()
    assert c.stopped
    assert '出力が停止しました' in (runtime/'obs-status.txt').read_text()
    lua.globals().script_unload()
