"""Public signaling broker. coturn carries media when direct ICE cannot connect."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from aiohttp import web, WSMsgType


class Broker:
    def __init__(self, key, turn_secret, turn_host, origins, turn_port=3479):
        if len(key)<32 or len(turn_secret)<32:
            raise ValueError('Publisher key and TURN secret must be at least 32 characters')
        self.key=key; self.turn_secret=turn_secret; self.turn_host=turn_host; self.turn_port=turn_port
        self.origins=set(origins); self.ws=None; self.token=''; self.updated=0
        self.state={'sharing':False,'live':False,'error':''}
        self.pending={}

    def ice(self, publisher=False):
        username=f'{int(time.time())+43200}:{secrets.token_hex(8)}'
        credential=base64.b64encode(hmac.new(self.turn_secret.encode(),username.encode(),hashlib.sha1).digest()).decode()
        turns=[f'turn:{self.turn_host}:{self.turn_port}?transport=tcp'] if publisher else [f'turn:{self.turn_host}:{self.turn_port}?transport=udp',f'turn:{self.turn_host}:{self.turn_port}?transport=tcp']
        return [{'urls':f'stun:{self.turn_host}:{self.turn_port}'},
                {'urls':turns,
                 'username':username,'credential':credential}]

    def authorize(self,request):
        if not self.token or not secrets.compare_digest(request.headers.get('Authorization',''),'Bearer '+self.token):
            raise web.HTTPUnauthorized(text='招待URLが無効です。新しいURLを受け取ってください。')

    def online(self):
        return self.ws is not None and not self.ws.closed and time.monotonic()-self.updated<6

    async def forward(self,kind,request):
        self.authorize(request)
        if not self.online():
            raise web.HTTPServiceUnavailable(text='配信PCが未接続です。')
        if kind=='offer' and (not self.state['live'] or not self.state['sharing']):
            raise web.HTTPConflict(text='本番映像の共有開始を待っています。')
        if len(self.pending)>=6:
            raise web.HTTPTooManyRequests(text='接続が混み合っています。少し待って再試行してください。')
        try:
            data=await request.json()
        except (ValueError,UnicodeError):
            raise web.HTTPBadRequest(text='接続情報が不正です。')
        if not isinstance(data,dict):
            raise web.HTTPBadRequest()
        rid=secrets.token_urlsafe(16);future=asyncio.get_running_loop().create_future()
        self.pending[rid]=future
        token=self.token
        try:
            await self.ws.send_json({'type':kind,'request_id':rid,'token':token,'data':data,'ice_servers':self.ice(publisher=True)})
            result=await asyncio.wait_for(future,30)
            # A token rotation while answering invalidates this request too.
            self.authorize(request)
            return web.json_response(result['data'],status=result['status'])
        except asyncio.TimeoutError:
            raise web.HTTPGatewayTimeout(text='配信PCへの接続がタイムアウトしました。')
        except ConnectionError:
            raise web.HTTPServiceUnavailable(text='配信PCとの接続が切れました。')
        finally:
            self.pending.pop(rid,None)


def create_app(b):
    @web.middleware
    async def cors(request,handler):
        origin=request.headers.get('Origin','')
        if origin and origin not in b.origins:
            raise web.HTTPForbidden(text='許可されていない視聴ページです。')
        try:
            response=web.Response(status=204) if request.method=='OPTIONS' else await handler(request)
        except web.HTTPException as exc:
            response=exc
        response.headers.update({'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff'})
        if origin:
            response.headers.update({'Access-Control-Allow-Origin':origin,'Vary':'Origin',
                                     'Access-Control-Allow-Headers':'Authorization, Content-Type',
                                     'Access-Control-Allow-Methods':'GET, POST, OPTIONS'})
        if isinstance(response,web.HTTPException):raise response
        return response

    app=web.Application(middlewares=[cors],client_max_size=128*1024)

    async def health(request):return web.json_response({'ok':True,'publisher_connected':b.online()})
    async def status(request):
        b.authorize(request)
        state=dict(b.state)
        if not b.online():state.update(sharing=False,live=False,error='配信PCの接続待ちです。')
        return web.json_response({**state,'ice_servers':b.ice()})
    async def offer(request):return await b.forward('offer',request)
    async def leave(request):return await b.forward('leave',request)

    async def publisher(request):
        if not secrets.compare_digest(request.headers.get('Authorization',''),'Bearer '+b.key):
            raise web.HTTPUnauthorized()
        if b.ws is not None and not b.ws.closed:
            raise web.HTTPConflict(text='別の配信PCが接続中です。')
        ws=web.WebSocketResponse(heartbeat=15,max_msg_size=128*1024)
        # Reserve the slot before awaiting the upgrade.
        b.ws=ws
        try:
            await ws.prepare(request)
            await ws.send_json({'type':'hello','ice_servers':b.ice(publisher=True)})
            async for packet in ws:
                if packet.type!=WSMsgType.TEXT:continue
                try:message=json.loads(packet.data)
                except ValueError:await ws.close(code=1003);break
                if not isinstance(message,dict):await ws.close(code=1003);break
                if message.get('type')=='status':
                    token=message.get('token','')
                    if not isinstance(token,str) or not 32<=len(token)<=128:
                        await ws.close(code=1008);break
                    b.token=token;b.updated=time.monotonic()
                    b.state={k:bool(message.get(k)) for k in ('sharing','live')}
                    b.state['error']=str(message.get('error',''))[:300]
                elif message.get('type')=='result':
                    future=b.pending.get(message.get('request_id'))
                    code=message.get('status',502)
                    if not isinstance(code,int) or code<200 or code>599:code=502
                    if future and not future.done():future.set_result({'status':code,'data':message.get('data',{})})
        finally:
            if b.ws is ws:
                b.ws=None;b.state.update(sharing=False,live=False)
            for f in b.pending.values():
                if not f.done():f.set_exception(ConnectionError())
        return ws

    async def shutdown(app):
        if b.ws is not None:await b.ws.close(code=1001)

    app.on_shutdown.append(shutdown)
    app.router.add_get('/health',health)
    app.router.add_get('/api/status',status)
    app.router.add_post('/api/offer',offer)
    app.router.add_post('/api/leave',leave)
    app.router.add_get('/ws/publisher',publisher)
    app.router.add_route('OPTIONS','/{path:.*}',health)
    return app


if __name__=='__main__':
    config=json.loads(Path(os.environ.get('CASTER_CONFIG','/etc/obs-caster-share/config.json')).read_text())
    broker=Broker(config['publisher_key'],config['turn_secret'],config['turn_host'],config['origins'],config.get('turn_port',3479))
    web.run_app(create_app(broker),host='127.0.0.1',port=8792,access_log=None)
