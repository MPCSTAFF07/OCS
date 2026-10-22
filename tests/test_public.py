import asyncio
import base64
import hashlib
import hmac
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from relay.app import Broker,create_app as relay_app
from deploy.install import nginx_include
from publisher import run_bridge
from server import Studio,create_app as studio_app


def test_nginx_insert_only_matching_https_block():
    text='''server { listen 80; server_name other.example; }
server {
 listen 443 ssl;
 server_name stream.example;
 location /existing { proxy_pass http://127.0.0.1:8765; }
}
'''
    result=nginx_include(text,'stream.example')
    assert result.count('include /etc/obs-caster-share/nginx-location.conf;')==1
    assert 'proxy_pass http://127.0.0.1:8765;' in result
    assert nginx_include(result,'stream.example')==result
    with pytest.raises(ValueError):nginx_include(text,'missing.example')


@pytest.mark.asyncio
async def test_broker_auth_cors_forwarding_revocation_and_disconnect():
    b=Broker('p'*40,'s'*48,'turn.example',['https://test.github.io'])
    async with TestClient(TestServer(relay_app(b))) as c:
        assert (await c.get('/api/status')).status==401
        assert (await c.get('/health',headers={'Origin':'https://evil.example'})).status==403
        pre=await c.options('/api/offer',headers={'Origin':'https://test.github.io','Access-Control-Request-Headers':'authorization,content-type'})
        assert pre.status==204 and pre.headers['Access-Control-Allow-Origin']=='https://test.github.io'
        with pytest.raises(Exception):await c.ws_connect('/ws/publisher',headers={'Authorization':'Bearer wrong'})
        ws=await c.ws_connect('/ws/publisher',headers={'Authorization':'Bearer '+'p'*40})
        hello=await ws.receive_json()
        ice=hello['ice_servers'][1]
        expected=base64.b64encode(hmac.new(('s'*48).encode(),ice['username'].encode(),hashlib.sha1).digest()).decode()
        assert ice['credential']==expected
        assert ice['urls']==['turn:turn.example:3479?transport=tcp']
        token='v'*43
        await ws.send_json({'type':'status','token':token,'sharing':True,'live':True,'error':''})
        await asyncio.sleep(.03)
        vh={'Authorization':'Bearer '+token,'Origin':'https://test.github.io'}
        state=await c.get('/api/status',headers=vh)
        assert state.status==200 and (await state.json())['live']
        request=asyncio.create_task(c.post('/api/offer',headers=vh,json={'type':'offer','sdp':'test','name':'実況'}))
        forwarded=await ws.receive_json()
        assert forwarded['type']=='offer' and forwarded['token']==token
        assert forwarded['data']['name']=='実況'
        await ws.send_json({'type':'result','request_id':forwarded['request_id'],'status':200,'data':{'type':'answer','sdp':'reply','id':'session'}})
        response=await request
        assert response.status==200 and (await response.json())['sdp']=='reply'
        await ws.send_json({'type':'status','token':'n'*43,'sharing':False,'live':False})
        await asyncio.sleep(.03)
        assert (await c.get('/api/status',headers=vh)).status==401
        await ws.close()
        await asyncio.sleep(.03)
        new={'Authorization':'Bearer '+'n'*43}
        assert not (await (await c.get('/api/status',headers=new)).json())['live']


@pytest.mark.asyncio
async def test_outbound_publisher_bridge_and_key_not_exposed(tmp_path,monkeypatch):
    monkeypatch.setattr('aioice.ice.get_host_addresses',lambda **kwargs:['127.0.0.1'])
    s=Studio(tmp_path)
    b=Broker('p'*40,'s'*48,'turn.example',['https://test.github.io'])
    # Actual loopback WebRTC below. TURN credentials are checked in the prior test;
    # this environment does not have an external TURN server.
    b.ice=lambda publisher=False:[]
    relay=TestServer(relay_app(b));await relay.start_server()
    local=TestServer(studio_app(s,bridge_enabled=False));await local.start_server()
    s.public={'relay_url':str(relay.make_url('')).rstrip('/'),'viewer_url':'https://test.github.io/obs/','publisher_key':'p'*40}
    task=asyncio.create_task(run_bridge(s,local.port))
    try:
        for _ in range(100):
            if s.public_state=='接続済み' and b.token:break
            await asyncio.sleep(.03)
        assert s.public_state=='接続済み'
        assert b.token==s.invite
        assert 'publisher_key' not in s.status()['public']
        # A leave request crosses public HTTP -> WS -> local HTTP -> WS -> public HTTP.
        import aiohttp
        async with aiohttp.ClientSession() as client:
            async with client.post(relay.make_url('/api/leave'),headers={'Authorization':'Bearer '+s.invite},json={'id':'none'}) as r:
                assert r.status==200 and (await r.json())=={'ok':True}
            from aiortc import RTCPeerConnection,RTCConfiguration,RTCSessionDescription,VideoStreamTrack,AudioStreamTrack
            import time
            s.sharing=True;s.tracks=[AudioStreamTrack(),VideoStreamTrack()]
            s.last_frame['video']=time.monotonic()
            for _ in range(100):
                if b.state['live']:break
                await asyncio.sleep(.02)
            pc=RTCPeerConnection(RTCConfiguration(iceServers=[]));received={}
            pc.on('track',lambda t:received.update({t.kind:t}))
            try:
                pc.addTransceiver('video',direction='recvonly');pc.addTransceiver('audio',direction='recvonly')
                await pc.setLocalDescription(await pc.createOffer())
                async with client.post(relay.make_url('/api/offer'),headers={'Authorization':'Bearer '+s.invite},json={'type':'offer','sdp':pc.localDescription.sdp,'name':'実況'}) as r:
                    assert r.status==200,await r.text()
                    answer=await r.json()
                await pc.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'],type=answer['type']))
                assert (await asyncio.wait_for(received['video'].recv(),5)).width>0
                assert (await asyncio.wait_for(received['audio'].recv(),5)).samples>0
                assert len(s.peers)==1
                async with client.post(relay.make_url('/api/leave'),headers={'Authorization':'Bearer '+s.invite},json={'id':answer['id']}) as r:
                    assert r.status==200
                assert not s.peers
            finally:await pc.close()
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True)
        await relay.close();await local.close()
