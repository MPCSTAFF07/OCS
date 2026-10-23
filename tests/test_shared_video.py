"""Moving video, shared encoding, slow readers, and real multi-viewer WebRTC."""
import asyncio
import sys
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path

import pytest
from av import VideoFrame
from aiortc import MediaStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import Studio, MonitorTrack, create_app
from shared_video import SharedVP8, SharedVP8Encoder, bind_frame, frame_hub


def moving(index, hub, width=320, height=180):
    frame=VideoFrame(width,height,'yuv420p')
    for i,plane in enumerate(frame.planes):
        plane.update(bytes([32+index%160 if i==0 else 128])*plane.buffer_size)
    frame.pts=index*1500;frame.time_base=Fraction(1,90000);bind_frame(frame,hub)
    return frame


def test_parallel_viewers_encode_each_frame_once_and_keep_memory_bounded():
    hub=SharedVP8();viewers=[SharedVP8Encoder() for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for index in range(20):
            frame=moving(index,hub)
            results=list(pool.map(lambda encoder:encoder.encode(frame),viewers))
            assert all(result==results[0] for result in results)
    assert hub.sequence==20 and hub.hits==60
    assert len(hub.cache)==8
    # Late requests for evicted frames cannot encode backwards or corrupt others.
    assert viewers[0].encode(moving(0,hub))==([],0)
    assert hub.sequence==20


def test_late_join_and_skipped_frames_request_shared_keyframe():
    hub=SharedVP8();fast=SharedVP8Encoder();late=SharedVP8Encoder()
    assert fast.encode(moving(0,hub))[0]
    frame=moving(1,hub);assert fast.encode(frame)[0]
    assert not late.encode(frame)[0]  # Initial P frame cannot be decoded independently.
    frame=moving(2,hub)
    assert fast.encode(frame)[0] and late.encode(frame)[0]
    assert hub.cache[id(frame)][1].keyframe
    fast.encode(moving(3,hub))  # The slow viewer misses this dependency.
    frame=moving(4,hub);fast.encode(frame)
    assert not late.encode(frame)[0]
    frame=moving(5,hub)
    assert fast.encode(frame)[0] and late.encode(frame)[0]
    assert hub.cache[id(frame)][1].keyframe
    # Receiver-driven bandwidth feedback still reaches the shared encoder.
    late.target_bitrate=900000
    frame=moving(6,hub);fast.encode(frame);late.encode(frame)
    assert hub.encoder.target_bitrate==900000


@pytest.mark.asyncio
async def test_two_1080p60_viewers_share_encoder_and_decode_motion(tmp_path,monkeypatch):
    monkeypatch.setattr('aioice.ice.get_host_addresses',lambda **kwargs:['127.0.0.1'])
    original_encode=SharedVP8Encoder.encode
    fallback=[]
    def observed(self,frame,force_keyframe=False):
        if not isinstance(frame_hub(frame) or self.hub,SharedVP8):fallback.append(frame.pts)
        return original_encode(self,frame,force_keyframe)
    monkeypatch.setattr(SharedVP8Encoder,'encode',observed)
    s=Studio(tmp_path);s.sharing=True
    # Actual moving texture, H264 decoding and pacing, rather than flat-color frames.
    from aiortc.contrib.media import MediaPlayer
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg needed for moving 1080p source')
    clip=tmp_path/'moving.mp4'
    process=await asyncio.create_subprocess_exec('ffmpeg','-hide_banner','-loglevel','error',
        '-f','lavfi','-i','testsrc2=size=1920x1080:rate=60','-frames:v','120',
        '-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',str(clip))
    assert await process.wait()==0
    player=MediaPlayer(str(clip))
    s.player=player
    s.tracks=[MonitorTrack(player.video,s)]
    s.last_frame['video']=time.monotonic()
    clients=[]
    try:
        async with TestClient(TestServer(create_app(s,bridge_enabled=False))) as client:
            async def join():
                pc=RTCPeerConnection(RTCConfiguration(iceServers=[]));clients.append(pc);received={}
                pc.on('track',lambda track:received.update(video=track))
                pc.addTransceiver('video',direction='recvonly')
                await pc.setLocalDescription(await pc.createOffer())
                response=await client.post('/api/offer',headers={'Authorization':'Bearer '+s.invite},json={'type':'offer','sdp':pc.localDescription.sdp})
                assert response.status==200,await response.text()
                answer=await response.json();assert 'VP8/90000' in answer['sdp']
                await pc.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'],type=answer['type']))
                return received
            viewers=await asyncio.gather(join(),join())
            async def receive(dest):
                frames=[await asyncio.wait_for(dest['video'].recv(),10) for _ in range(40)]
                assert all((f.width,f.height)==(1920,1080) for f in frames)
                assert all(b.pts>a.pts for a,b in zip(frames,frames[1:]))
                assert frames[0].to_ndarray().mean()!=frames[-1].to_ndarray().mean()
                return frames
            frames=await asyncio.gather(*(receive(v) for v in viewers))
            stats=s.video_encoder.stats()
            if stats is None:
                await asyncio.sleep(.05);stats=s.video_encoder.stats()
            assert stats['encoded_frames']>30 and stats['reused_frames']>15,stats
            assert stats['encoded_fps']>30,stats
            print('shared encoder statistics:',stats)
            assert not fallback
    finally:
        await asyncio.gather(*(pc.close() for pc in clients))
