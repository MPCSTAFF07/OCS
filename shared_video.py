"""Encode each VP8 frame once; preserve per-viewer RTP and REMB handling.

aiortc's encoder factory is intercepted only for frames registered by the OCS monitor in
a bounded frame registry. Audio and all other video tracks retain stock aiortc behavior.
"""
import threading
import time
import os
from fractions import Fraction

import av
from collections import OrderedDict
from dataclasses import dataclass

from aiortc import codecs
from aiortc.codecs.vpx import Vp8Encoder, VpxPayloadDescriptor


_FRAMES = OrderedDict()


def bind_frame(frame, hub):
    # PyAV can clear opaque metadata and rescale pts during encode. Keep a small
    # identity registry instead; strong references prevent recycled-object IDs.
    _FRAMES[id(frame)] = (frame, hub)
    while len(_FRAMES)>16:
        _FRAMES.popitem(last=False)


def frame_hub(frame):
    item = _FRAMES.get(id(frame))
    return item[1] if item is not None and item[0] is frame else None


@dataclass(frozen=True)
class Encoded:
    sequence: int
    payloads: list
    timestamp: int
    keyframe: bool


class FastVP8Encoder(Vp8Encoder):
    """Realtime settings with a bounded worker count, avoiding CPU oversubscription."""
    def encode(self, frame, force_keyframe=False):
        if (self.codec is None or self.codec.width != frame.width
                or self.codec.height != frame.height
                or abs(self.target_bitrate-self.codec.bit_rate)/self.codec.bit_rate > .1):
            codec = av.CodecContext.create("libvpx", "w")
            codec.width, codec.height = frame.width, frame.height
            codec.bit_rate = self.target_bitrate
            codec.pix_fmt = "yuv420p"
            codec.time_base = Fraction(1,90000)
            codec.framerate = Fraction(60,1)
            codec.gop_size = 60
            codec.qmin, codec.qmax = 2, 56
            codec.thread_count = min(4, os.cpu_count() or 1)
            codec.options = {"deadline":"realtime", "cpu-used":"8", "lag-in-frames":"0",
                             "bufsize":str(self.target_bitrate), "minrate":str(self.target_bitrate),
                             "maxrate":str(self.target_bitrate), "auto-alt-ref":"0"}
            self.codec = codec
        return super().encode(frame, force_keyframe)


class SharedVP8:
    def __init__(self):
        self.lock = threading.Lock()
        self.encoder = FastVP8Encoder()
        self.cache = OrderedDict()
        self.targets = {}
        self.keyframe_requested = True
        self.sequence = 0
        self.last_stamp = None
        self.hits = 0
        self.encode_ms = 0.0
        self.samples = []
        self.snapshot = dict(mode='shared-vp8', encoded_frames=0, reused_frames=0,
                             encode_ms=0.0, encoded_fps=0.0, updated=0)

    def request_keyframe(self):
        with self.lock:
            self.keyframe_requested = True

    def encode(self, frame, force_keyframe, viewer, target):
        stamp = id(frame)  # PyAV mutates pts/time_base while encoding.
        with self.lock:
            now = time.monotonic()
            self.targets[viewer] = (target, now)
            self.targets = {k:v for k,v in self.targets.items() if now-v[1] < 3}
            self.encoder.target_bitrate = min(v[0] for v in self.targets.values())
            self.keyframe_requested |= force_keyframe
            if stamp in self.cache:
                self.hits += 1
                return self.cache[stamp][1]
            media_time = float(frame.pts * frame.time_base)
            if self.last_stamp is not None and media_time <= self.last_stamp:
                # Never run the stateful encoder backwards for a delayed viewer.
                self.keyframe_requested = True
                return None
            started = time.monotonic()
            payloads, timestamp = self.encoder.encode(frame, self.keyframe_requested)
            self.keyframe_requested = False
            elapsed = (time.monotonic() - started) * 1000
            self.encode_ms = elapsed if not self.sequence else self.encode_ms * .9 + elapsed * .1
            self.sequence += 1
            self.last_stamp = media_time
            keyframe = False
            if payloads:
                _, data = VpxPayloadDescriptor.parse(payloads[0])
                keyframe = bool(data) and not (data[0] & 1)
            result = Encoded(self.sequence, payloads, timestamp, keyframe)
            self.cache[stamp] = (frame, result)  # Retain identity until bounded eviction.
            while len(self.cache) > 8:
                self.cache.popitem(last=False)
            self.samples.append(time.monotonic())
            self.samples = [x for x in self.samples if self.samples[-1] - x < 3]
            fps = (len(self.samples)-1)/(self.samples[-1]-self.samples[0]) if len(self.samples)>1 else 0
            self.snapshot = dict(mode='shared-vp8', encoded_frames=self.sequence,
                                 reused_frames=self.hits, encode_ms=round(self.encode_ms,1),
                                 encoded_fps=round(fps,1), updated=time.monotonic())
            return result

    def close(self):
        with self.lock:
            self.cache.clear()
            self.targets.clear()
            self.encoder.codec = None
            for key, (_, hub) in list(_FRAMES.items()):
                if hub is self:
                    _FRAMES.pop(key, None)

    def stats(self):
        # Immutable snapshots avoid waiting for the worker encoder in the event loop.
        state = dict(self.snapshot)
        if time.monotonic()-state.pop('updated')>2:
            state['encoded_fps'] = 0
        return state


class SharedVP8Encoder(Vp8Encoder):
    def __init__(self):
        super().__init__()
        self.viewer = object()
        self.sequence = None
        self.waiting_for_keyframe = True
        self.hub = None

    def encode(self, frame, force_keyframe=False):
        hub = frame_hub(frame) or self.hub
        if not isinstance(hub, SharedVP8):
            return super().encode(frame, force_keyframe)
        self.hub = hub
        result = hub.encode(frame, force_keyframe, self.viewer, self.target_bitrate)
        if result is None:
            return [], 0
        gap = self.sequence is not None and result.sequence != self.sequence + 1
        self.sequence = result.sequence
        if result.keyframe:
            self.waiting_for_keyframe = False
        elif gap or self.waiting_for_keyframe:
            # Unbuffered relays may drop raw frames for a slow connection. Do not
            # send dependent deltas until a fresh shared keyframe is available.
            self.waiting_for_keyframe = True
            hub.request_keyframe()
            return [], result.timestamp
        return result.payloads, result.timestamp


def enable_shared_vp8():
    codecs.Vp8Encoder = SharedVP8Encoder
