import json
import asyncio
import sys
import time
from pathlib import Path

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer,TestClient

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from relay.app import Broker,create_app as relay_app
from server import Studio,create_app as local_app
from publisher import run_bridge


@pytest.mark.asyncio
async def test_management_authentication_and_offline_state():
    b=Broker('p'*40,'s'*48,'turn.example',['https://test.github.io'],admin_key='a'*40)
    async with TestClient(TestServer(relay_app(b))) as client:
        for key in ('','p'*40,'s'*48,'viewer-invite'):
            assert (await client.get('/api/admin',headers={'Authorization':'Bearer '+key})).status==401
        good={'Authorization':'Bearer '+'a'*40,'Origin':'https://test.github.io'}
        response=await client.get('/api/admin',headers=good)
        assert response.status==503
        assert response.headers['Access-Control-Allow-Origin']=='https://test.github.io'
        assert (await client.get('/api/admin',headers={**good,'Origin':'https://evil.example'})).status==403


@pytest.mark.asyncio
async def test_management_commands_cross_broker_and_outbound_bridge(tmp_path):
    s=Studio(tmp_path)
    b=Broker('p'*40,'s'*48,'turn.example',['https://test.github.io'],admin_key='a'*40)
    local=TestServer(local_app(s,bridge_enabled=False));await local.start_server()
    public=TestServer(relay_app(b));await public.start_server()
    s.public={'relay_url':str(public.make_url('')).rstrip('/'),'viewer_url':'https://test.github.io/OCS/','publisher_key':'p'*40}
    task=asyncio.create_task(run_bridge(s,local.port))
    ah={'Authorization':'Bearer '+'a'*40}
    try:
        for _ in range(100):
            if b.online() and b.remote_admin:break
            await asyncio.sleep(.02)
        assert b.online() and b.remote_admin
        async with ClientSession() as client:
            response=await client.get(public.make_url('/api/admin'),headers=ah)
            assert response.status==200
            state=await response.json()
            assert state['public']['viewer_url']=='https://test.github.io/OCS/'
            assert 'publisher_key' not in state['public'] and s.admin not in str(state)
            assert (await client.get(public.make_url('/api/admin'),headers={'Authorization':'Bearer '+s.invite})).status==401
            assert (await client.post(public.make_url('/api/admin'),headers=ah,json={'action':'config','publisher_key':'evil'})).status==400
            previous=s.invite
            response=await client.post(public.make_url('/api/admin'),headers=ah,json={'action':'rotate'})
            assert response.status==200 and s.invite!=previous
            response=await client.post(public.make_url('/api/admin'),headers=ah,json={'action':'disconnect','id':'missing'})
            assert response.status==200
            # Start traverses the remote control path and reports a real local precondition.
            response=await client.post(public.make_url('/api/admin'),headers=ah,json={'action':'start'})
            assert response.status==409
            assert 'caster-share.lua' in (await response.json())['error']
            # Supply a live test heartbeat and a no-op input opener to exercise start/stop.
            (s.runtime/'obs-status.txt').write_text(f'{time.time()}\nidle')
            async def opened():pass
            s.open_media=opened
            response=await client.post(public.make_url('/api/admin'),headers=ah,json={'action':'start'})
            assert response.status==200 and s.sharing
            response=await client.post(public.make_url('/api/admin'),headers=ah,json={'action':'stop'})
            assert response.status==200 and not s.sharing
            # AV changes traverse both whitelists; viewer tokens still cannot administer them.
            (s.runtime/'obs-details.json').write_text(json.dumps(dict(timestamp=time.time(),available_track=6,error='',sources=[dict(id='game',name='ゲーム')])))
            av=dict(action='av_config',height=1080,fps=60,bitrate=8000,audio_mode='selected',audio_sources=['game'])
            response=await client.post(public.make_url('/api/admin'),headers=ah,json=av)
            assert response.status==200 and s.av['audio_sources']==['game']
            assert (await response.json())['av']['height']==1080
            assert (await client.post(public.make_url('/api/admin'),headers={'Authorization':'Bearer '+s.invite},json=av)).status==401
            assert s.public['publisher_key']=='p'*40
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True)
        await public.close();await local.close()


@pytest.mark.asyncio
async def test_old_publisher_reports_upgrade_needed():
    b=Broker('p'*40,'s'*48,'turn.example',[],admin_key='a'*40)
    async with TestClient(TestServer(relay_app(b))) as client:
        ws=await client.ws_connect('/ws/publisher',headers={'Authorization':'Bearer '+'p'*40})
        await ws.receive_json()
        await ws.send_json({'type':'status','token':'v'*43,'sharing':False,'live':False})
        await asyncio.sleep(.02)
        response=await client.get('/api/admin',headers={'Authorization':'Bearer '+'a'*40})
        assert response.status==409 and 'publisher.py' in await response.text()
        await ws.close()


def test_management_keys_cannot_reuse_streaming_credentials():
    with pytest.raises(ValueError):Broker('p'*40,'s'*48,'turn.example',[],admin_key='p'*40)
