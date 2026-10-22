"""Run with: python -m pytest -q (ffmpeg required for media integration)."""
import asyncio
import shutil
import sys
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Studio, create_app


@pytest.mark.asyncio
async def test_api_auth_and_configuration(tmp_path):
    s = Studio(tmp_path)
    async with TestClient(TestServer(create_app(s,bridge_enabled=False))) as c:
        assert (await c.get('/api/admin')).status == 401
        assert (await c.get('/api/status')).status == 401
        h = {'Authorization': 'Bearer ' + s.admin}
        assert (await c.post('/api/admin', headers=h, json={'action': 'start'})).status == 409
        for url in ['http://example.com', 'https://user:pass@example.com', 'https://example.com?q=x']:
            assert (await c.post('/api/admin', headers=h, json={'action': 'config', 'relay_url': url, 'viewer_url':'https://test.github.io/obs/','publisher_key':'p'*40})).status == 400
        r = await c.post('/api/admin', headers=h, json={'action': 'config', 'relay_url': 'https://relay.example.com/caster', 'viewer_url':'https://test.github.io/obs/','publisher_key':'p'*40})
        assert r.status == 200
        result = await r.json()
        assert result['invite_url'].startswith('https://test.github.io/obs/#api=')
        assert 'publisher_key' not in result['public']
        old = s.invite
        assert (await c.post('/api/admin', headers=h, json={'action': 'rotate'})).status == 200
        assert (await c.get('/api/status', headers={'Authorization': 'Bearer '+old})).status == 401
        assert (await c.get('/api/status', headers={'Authorization': 'Bearer '+s.invite})).status == 200
        assert (await c.get('/assets/server.py')).status == 404


@pytest.mark.asyncio
async def test_missing_udp_times_out_and_can_stop(tmp_path):
    s=Studio(tmp_path)
    (s.runtime/'obs-status.txt').write_text(f'{time.time()}\nidle')
    await s.start()
    await asyncio.wait_for(s.open_task,12)
    assert not s.sharing
    assert s.error
    assert (s.runtime/'command.txt').read_text().splitlines()[1]=='stop'
    await s.stop()


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg needed')
async def test_udp_input_two_webrtc_viewers_and_restart(tmp_path, monkeypatch):
    # Restrict ICE to real loopback sockets; sandbox forbids interface enumeration.
    monkeypatch.setattr("aioice.ice.get_host_addresses", lambda **kwargs: ["127.0.0.1"])
    s = Studio(tmp_path)
    # Simulates only the Lua heartbeat; the actual media uses FFmpeg, UDP, and WebRTC.
    async def heartbeat():
        while True:
            (s.runtime / 'obs-status.txt').write_text(f'{time.time()}\nactive', encoding='utf-8')
            await asyncio.sleep(.3)
    beat = asyncio.create_task(heartbeat())
    process = None
    clients = []
    received = []

    async def ffmpeg():
        return await asyncio.create_subprocess_exec(
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-re',
            '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=15',
            '-f', 'lavfi', '-i', 'sine=frequency=1000:sample_rate=48000',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency', '-g', '15',
            '-c:a', 'aac', '-b:a', '96k', '-f', 'mpegts',
            'udp://127.0.0.1:18791?pkt_size=1316', stderr=asyncio.subprocess.PIPE)

    try:
        async with TestClient(TestServer(create_app(s,bridge_enabled=False))) as c:
            await asyncio.sleep(.05)
            ah = {'Authorization': 'Bearer '+s.admin}
            vh = {'Authorization': 'Bearer '+s.invite}
            r = await c.post('/api/admin', headers=ah, json={'action':'start'})
            assert r.status == 200
            process = await ffmpeg()
            for _ in range(100):
                if s.status()['live']:
                    break
                await asyncio.sleep(.1)
            assert s.status()['live'], s.status()
            for name in ['実況', '解説']:
                pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
                clients.append(pc)
                tracks = {}
                pc.on('track', lambda t, dest=tracks: dest.update({t.kind:t}))
                pc.addTransceiver('video', direction='recvonly')
                pc.addTransceiver('audio', direction='recvonly')
                await pc.setLocalDescription(await pc.createOffer())
                r = await c.post('/api/offer', headers=vh, json={
                    'sdp':pc.localDescription.sdp, 'type':'offer', 'name':name})
                assert r.status == 200, await r.text()
                answer = await r.json()
                await pc.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'],type='answer'))
                video = await asyncio.wait_for(tracks['video'].recv(), 10)
                audio = await asyncio.wait_for(tracks['audio'].recv(), 10)
                assert (video.width, video.height) == (320,180)
                assert audio.samples > 0
                first = video.to_ndarray(format='rgb24')
                await asyncio.sleep(.2)
                video2 = await asyncio.wait_for(tracks['video'].recv(), 3)
                assert video2.pts > video.pts
                assert (first != video2.to_ndarray(format='rgb24')).any()
                received.append(answer['id'])
            result = await (await c.get('/api/admin',headers=ah)).json()
            assert sum(v['state']=='connected' for v in result['viewers']) == 2
            assert (await c.post('/api/admin',headers=ah,json={'action':'disconnect','id':received[0]})).status==200
            assert len(s.peers)==1
            old=s.invite
            await c.post('/api/admin',headers=ah,json={'action':'rotate'})
            assert len(s.peers)==0
            assert (await c.post('/api/offer',headers={'Authorization':'Bearer '+old},json={})).status==401
            await c.post('/api/admin',headers=ah,json={'action':'stop'})
            assert not s.sharing and s.player is None
            process.terminate();await process.wait();process=None
            # Receiver can bind and receive again after shutdown.
            await asyncio.sleep(.2)
            assert (await c.post('/api/admin',headers=ah,json={'action':'start'})).status==200
            process=await ffmpeg()
            for _ in range(100):
                if s.status()['live']:break
                await asyncio.sleep(.1)
            assert s.status()['live'],s.status()
            await c.post('/api/admin',headers=ah,json={'action':'stop'})
    finally:
        beat.cancel()
        await asyncio.gather(beat,return_exceptions=True)
        for pc in clients:await pc.close()
        if process and process.returncode is None:
            process.terminate();await process.wait()
