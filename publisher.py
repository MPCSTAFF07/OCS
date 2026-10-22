"""Outbound-only signaling link: the broadcaster needs no VPN or open ports."""
import asyncio
import contextlib
from urllib.parse import urlsplit, urlunsplit

from aiohttp import (ClientSession, ClientTimeout, WSMsgType, WSServerHandshakeError,
                     ClientConnectorCertificateError, ClientConnectorError)


def connection_error(exc):
    """Do not include URLs, request headers, or credentials in user-facing errors."""
    if isinstance(exc, WSServerHandshakeError):
        reasons = {401: '配信キーが一致しません。AWS側のpublisher_keyを入力し直してください',
                   403: '接続が拒否されました。サーバーのアクセス設定を確認してください',
                   404: '接続先が見つかりません。中継URLの末尾が/casterか確認してください',
                   409: '別の配信接続が残っています。古いOCSを終了してください',
                   502: '公開サーバーの中継サービスが応答していません'}
        return f'接続失敗（HTTP {exc.status}）：' + reasons.get(exc.status, 'サーバーの接続経路を確認してください')
    if isinstance(exc, ClientConnectorCertificateError):
        return '接続失敗：HTTPS証明書を確認できません。PCの日時とサーバー証明書を確認してください'
    if isinstance(exc, ClientConnectorError):
        return '接続失敗：サーバーに到達できません（DNS・回線・443番ポートを確認）'
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return '接続失敗：通信がタイムアウトしました'
    return '接続失敗：' + type(exc).__name__


async def run_bridge(studio, local_port=8790):
    while True:
        cfg = dict(studio.public)
        if not all(cfg.get(k) for k in ('relay_url', 'publisher_key', 'viewer_url')):
            studio.public_state = '未設定'
            await asyncio.sleep(1)
            continue
        studio.public_state = '接続中'
        tasks = set()
        try:
            url = urlsplit(cfg['relay_url'])
            ws_url = urlunsplit(('wss' if url.scheme == 'https' else 'ws', url.netloc,
                                url.path.rstrip('/')+'/ws/publisher', '', ''))
            async with ClientSession(timeout=ClientTimeout(total=35)) as session:
                async with session.ws_connect(ws_url, headers={'Authorization':'Bearer '+cfg['publisher_key']},
                                              heartbeat=15, max_msg_size=128*1024) as ws:
                    async def status_loop():
                        while True:
                            if cfg != studio.public:
                                await ws.close()
                                return
                            st = studio.status()
                            await ws.send_json({'type':'status', 'token':studio.invite,
                                                **{k:st[k] for k in ('sharing','live','error')}})
                            await asyncio.sleep(1)

                    async def reply(message):
                        path = 'offer' if message['type']=='offer' else 'leave'
                        try:
                            async with session.post(f'http://127.0.0.1:{local_port}/api/{path}',
                                                    headers={'Authorization':'Bearer '+message.get('token','')},
                                                    json=message['data']) as r:
                                result = await r.json() if r.status==200 else {'error':await r.text()}
                                await ws.send_json({'type':'result','request_id':message['request_id'],
                                                    'status':r.status,'data':result})
                        except Exception:
                            if not ws.closed:
                                await ws.send_json({'type':'result','request_id':message['request_id'],
                                                    'status':502,'data':{'error':'配信PCへの接続に失敗しました。'}})

                    sender = asyncio.create_task(status_loop())
                    tasks.add(sender)
                    async for packet in ws:
                        if packet.type != WSMsgType.TEXT:
                            continue
                        message = packet.json()
                        if message.get('type') == 'hello':
                            studio.ice_servers = message['ice_servers']
                            studio.public_state = '接続済み'
                        elif message.get('type') in ('offer','leave'):
                            if 'ice_servers' in message:
                                studio.ice_servers = message['ice_servers']
                            task=asyncio.create_task(reply(message))
                            tasks.add(task); task.add_done_callback(tasks.discard)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            studio.public_state = connection_error(exc)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # A detached signaling channel must not leave revocation-inaccessible viewers.
            peers, studio.peers = studio.peers, {}
            await asyncio.gather(*(item[0].close() for item in peers.values()))
            if studio.public_state == '接続済み':
                studio.public_state = '再接続中'
        await asyncio.sleep(3)
