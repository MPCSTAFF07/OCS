"""OBS Caster Share: loopback control, MPEG-TS input, authenticated WebRTC output."""
import asyncio
import contextlib
import json
import logging
import os
import secrets
import socket
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit, urlencode

from aiohttp import web
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCRtpSender, RTCSessionDescription, MediaStreamTrack
from aiortc.contrib.media import MediaBlackhole, MediaPlayer, MediaRelay

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("caster-share")


class MonitorTrack(MediaStreamTrack):
    def __init__(self, source, state):
        super().__init__()
        self.kind = source.kind
        self.source = source
        self.state = state

    async def recv(self):
        frame = await self.source.recv()
        self.state.last_frame[self.kind] = time.monotonic()
        return frame


class Studio:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(exist_ok=True)
        self.admin = secrets.token_urlsafe(32)
        self.invite = secrets.token_urlsafe(32)
        self.sharing = False
        self.error = ""
        self.base_url = ""
        self.public = {}
        self.public_state = "未設定"
        self.ice_servers = []
        config = self.runtime / "config.json"
        if config.exists():
            with contextlib.suppress(ValueError):
                loaded = json.loads(config.read_text("utf-8"))
                self.public = loaded.get("public", {})
        self.player = None
        self.relay = MediaRelay()
        self.tracks = []
        self.sink = None
        self.open_task = None
        self.last_frame = {}
        self.started_at = time.monotonic()
        self.peers = {}
        self.lock = asyncio.Lock()
        self.command("stop")

    def command(self, action):
        tmp = self.runtime / "command.tmp"
        tmp.write_text(f"{time.time_ns()}\n{action}\n", encoding="utf-8")
        os.replace(tmp, self.runtime / "command.txt")

    def obs_status(self):
        try:
            stamp, message = (self.runtime / "obs-status.txt").read_text("utf-8").split("\n", 1)
            if time.time() - float(stamp) < 5:
                return message.strip()
        except (OSError, ValueError):
            pass
        return "missing"

    def status(self):
        live = self.sharing and time.monotonic() - self.last_frame.get("video", 0) < 3
        invite_url = ""
        if self.public.get("viewer_url") and self.public.get("relay_url"):
            invite_url = self.public["viewer_url"] + "#" + urlencode({"api": self.public["relay_url"], "token": self.invite})
        return dict(sharing=self.sharing, live=live, error=self.error,
                    public_state=self.public_state,
                    public={k:v for k,v in self.public.items() if k != "publisher_key"},
                    obs=self.obs_status(), base_url=self.base_url,
                    invite_url=invite_url,
                    viewers=[dict(id=k, name=v[1], state=v[0].connectionState)
                             for k, v in self.peers.items()])

    async def open_media(self):
        try:
            player = await asyncio.to_thread(
                MediaPlayer, "udp://127.0.0.1:18791?fifo_size=4096&overrun_nonfatal=1",
                format="mpegts", options={"timeout": "4000000", "rw_timeout": "4000000",
                                           "probesize": "65536", "analyzeduration": "500000"},
                timeout=5)
            if not self.sharing:
                for t in (player.video, player.audio):
                    if t:
                        t.stop()
                return
            self.player = player
            self.tracks = [MonitorTrack(t, self) for t in (player.audio, player.video) if t]
            if not player.video:
                raise RuntimeError("OBSから映像を受信できませんでした。")
            self.sink = MediaBlackhole()
            for t in self.tracks:
                self.sink.addTrack(self.relay.subscribe(t, buffered=False))
            await self.sink.start()
        except Exception as exc:
            LOG.exception("OBS input failed")
            self.error = "OBSの映像を受信できません。スクリプト設定とOBSログを確認して、停止→開始してください。"
            self.command("stop")
            self.sharing = False

    async def start(self):
        async with self.lock:
            if self.sharing:
                return
            await self._stop()
            if self.obs_status() in ("missing", "unloaded"):
                raise web.HTTPConflict(text="OBSでcaster-share.luaを追加してください。")
            self.sharing = True
            self.error = ""
            self.last_frame.clear()
            self.started_at = time.monotonic()
            # The receiver starts opening immediately; OBS polls the command every 250 ms.
            self.open_task = asyncio.create_task(self.open_media())
            self.command("start")

    async def _stop(self):
        self.sharing = False
        self.command("stop")
        if self.open_task:
            # FFmpeg's interrupt timeout bounds opening; don't abandon a reader thread.
            await self.open_task
            self.open_task = None
        peers, self.peers = self.peers, {}
        await asyncio.gather(*(p[0].close() for p in peers.values()))
        if self.sink:
            await self.sink.stop()
            self.sink = None
        for t in self.tracks:
            t.stop()
        self.tracks.clear()
        if self.player:
            for t in (self.player.video, self.player.audio):
                if t:
                    t.stop()
            self.player = None
        self.relay = MediaRelay()
        self.last_frame.clear()

    async def stop(self):
        async with self.lock:
            await self._stop()


def authenticated(request, token):
    value = request.headers.get("Authorization", "")
    if not secrets.compare_digest(value, "Bearer " + token):
        raise web.HTTPUnauthorized(text="招待URLが無効、または管理キーが一致しません。")


@web.middleware
async def headers(request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc
    response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                             "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                             "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; media-src 'self' blob:; object-src 'none'; frame-ancestors 'none'"})
    if isinstance(response, web.HTTPException):
        raise response
    return response


STUDIO = web.AppKey("studio", Studio)
EXPIRY = web.AppKey("expiry", set)
WATCHDOG = web.AppKey("watchdog", asyncio.Task)
BRIDGE = web.AppKey("bridge", asyncio.Task)


def create_app(studio=None, *, bridge_enabled=True, local_port=8790):
    s = studio or Studio()
    app = web.Application(middlewares=[headers], client_max_size=128 * 1024)
    app[STUDIO] = s

    async def page(request):
        return web.FileResponse(s.root / "web" / ("dock.html" if request.path == "/dock" else "view.html"))

    async def asset(request):
        name = request.match_info["name"]
        if name not in ("style.css", "dock.js", "view.js"):
            raise web.HTTPNotFound()
        return web.FileResponse(s.root / "web" / name)

    async def admin(request):
        authenticated(request, s.admin)
        if request.method == "GET":
            return web.json_response(s.status())
        data = await request.json()
        action = data.get("action")
        if action == "start":
            await s.start()
        elif action == "stop":
            await s.stop()
        elif action == "rotate":
            # Rotation revokes existing sessions as well as the old URL.
            s.invite = secrets.token_urlsafe(32)
            peers, s.peers = s.peers, {}
            await asyncio.gather(*(p[0].close() for p in peers.values()))
        elif action == "disconnect":
            item = s.peers.pop(str(data.get("id")), None)
            if item:
                await item[0].close()
        elif action == "config":
            public = {}
            for key in ("relay_url", "viewer_url"):
                value = str(data.get(key, "")).strip().rstrip("/")
                parsed = urlsplit(value)
                if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                        or parsed.query or parsed.fragment):
                    raise web.HTTPBadRequest(text="中継URL・視聴ページURLはhttps://で入力してください。")
                public[key] = value + ("/" if key == "viewer_url" and not parsed.path.endswith(".html") else "")
            secret = str(data.get("publisher_key", "")).strip() or s.public.get("publisher_key", "")
            if len(secret) < 32:
                raise web.HTTPBadRequest(text="配信キーはサーバー導入時に発行された32文字以上の値を入力してください。")
            public["publisher_key"] = secret
            s.public = public
            tmp = s.runtime / "config.tmp"
            tmp.write_text(json.dumps({"public": public}), encoding="utf-8")
            os.replace(tmp, s.runtime / "config.json")
        else:
            raise web.HTTPBadRequest(text="不明な操作です。")
        return web.json_response(s.status())

    async def viewer_status(request):
        authenticated(request, s.invite)
        state = s.status()
        return web.json_response({k: state[k] for k in ("sharing", "live", "error")})

    async def offer(request):
        authenticated(request, s.invite)
        if not s.sharing or not s.tracks or not s.status()["live"]:
            raise web.HTTPConflict(text="共有映像の到着を待っています。")
        if len(s.peers) >= 6:
            raise web.HTTPTooManyRequests(text="接続上限は6人です。")
        data = await request.json()
        if data.get("type") != "offer" or not isinstance(data.get("sdp"), str):
            raise web.HTTPBadRequest(text="接続情報が不正です。")
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(**x) for x in s.ice_servers]))
        key = secrets.token_urlsafe(12)
        s.peers[key] = (pc, str(data.get("name", "実況解説"))[:40])

        @pc.on("connectionstatechange")
        async def changed():
            if pc.connectionState in ("failed", "closed"):
                s.peers.pop(key, None)
                if pc.connectionState != "closed":
                    await pc.close()

        async def expire():
            await asyncio.sleep(25)
            if pc.connectionState != "connected":
                s.peers.pop(key, None)
                await pc.close()
        task = asyncio.create_task(expire())
        app[EXPIRY].add(task)
        task.add_done_callback(app[EXPIRY].discard)
        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type="offer"))
            for track in s.tracks:
                sender = pc.addTrack(s.relay.subscribe(track, buffered=False))
                if track.kind == "video":
                    codecs = [c for c in RTCRtpSender.getCapabilities("video").codecs if c.mimeType == "video/H264"]
                    for trans in pc.getTransceivers():
                        if trans.sender == sender:
                            trans.setCodecPreferences(codecs)
            await pc.setLocalDescription(await pc.createAnswer())
            # Stop/rotate during ICE negotiation must not create an authorized zombie.
            if key not in s.peers or not s.sharing:
                raise web.HTTPConflict(text="共有が停止されました。")
            return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type, "id": key})
        except Exception:
            s.peers.pop(key, None)
            await pc.close()
            raise

    async def leave(request):
        authenticated(request, s.invite)
        data = await request.json()
        item = s.peers.pop(str(data.get("id")), None)
        if item:
            await item[0].close()
        return web.json_response({"ok": True})

    async def watchdog():
        while True:
            await asyncio.sleep(2)
            if s.sharing and s.player and time.monotonic() - s.last_frame.get("video", s.started_at) > 6:
                s.error = "OBSの映像が途切れました。停止してから再度開始してください。"
                await s.stop()

    async def startup(app):
        app[WATCHDOG] = asyncio.create_task(watchdog())
        if bridge_enabled:
            from publisher import run_bridge
            app[BRIDGE] = asyncio.create_task(run_bridge(s, local_port))

    async def cleanup(app):
        if BRIDGE in app:
            app[BRIDGE].cancel()
            await asyncio.gather(app[BRIDGE], return_exceptions=True)
        app[WATCHDOG].cancel()
        await asyncio.gather(app[WATCHDOG], return_exceptions=True)
        for task in list(app[EXPIRY]):
            task.cancel()
        await asyncio.gather(*list(app[EXPIRY]), return_exceptions=True)
        await s.stop()

    app[EXPIRY] = set()
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    app.router.add_get("/", page)
    app.router.add_get("/view", page)
    app.router.add_get("/dock", page)
    app.router.add_get("/assets/{name}", asset)
    app.router.add_get("/api/admin", admin)
    app.router.add_post("/api/admin", admin)
    app.router.add_get("/api/status", viewer_status)
    app.router.add_post("/api/offer", offer)
    app.router.add_post("/api/leave", leave)
    return app


def prepare_server(root=ROOT, port=8790):
    """Reserve the port before touching commands, tokens, or public connections."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(128)
        listener.setblocking(False)
        studio = Studio(root)
        actual_port = listener.getsockname()[1]
        dock_url = f"http://127.0.0.1:{actual_port}/dock#" + studio.admin
        (studio.runtime / "dock-url.txt").write_text(dock_url, encoding="utf-8")
        return listener, studio, dock_url
    except BaseException:
        listener.close()
        raise


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Access logs are disabled: invitation tokens never appear in URLs sent to the server.
    try:
        listener, studio, dock_url = prepare_server()
    except OSError:
        raise SystemExit("8790番ポートは使用中です。起動済みのOCSを終了してから実行してください。管理URLは変更していません。")
    print("OBS Caster Share\n管理URLは runtime/dock-url.txt に保存しました。\n終了は Ctrl+C")
    if not args.no_browser:
        import threading
        threading.Timer(1.5, lambda: webbrowser.open(dock_url)).start()
    web.run_app(create_app(studio), sock=listener, access_log=None)
