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
      captured = {}
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
        obs_output_active=function() return true end,
        timer_add=function(f,ms) captured.poll=f end, timer_remove=function() end
      }
    ''')
    lua.execute((Path(__file__).resolve().parents[1]/'caster-share.lua').read_text())
    lua.globals().script_update(lua.table_from({'track':2,'quality':1}))
    lua.globals().script_load(lua.table())
    (runtime/'command.txt').write_text('1\nstart\n')
    lua.globals().captured.poll()
    c=lua.globals().captured
    assert c.id=='ffmpeg_output' and c.started and c.media
    assert c.mixer==1
    assert c.settings['scale_width']==1280
    assert c.settings['format_name']=='mpegts'
    (runtime/'command.txt').write_text('2\nstop\n')
    c.poll()
    assert c.stopped and c.released
    lua.globals().script_unload()
