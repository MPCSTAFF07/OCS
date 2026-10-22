import asyncio
import sys
from pathlib import Path
import pytest
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from publisher import run_bridge,connection_error
from relay.app import Broker,create_app
from server import Studio


@pytest.mark.parametrize('status,expected',[(401,'配信キー'),(403,'拒否'),(404,'/caster'),(409,'別の配信'),(502,'応答')])
def test_status_messages_do_not_include_credentials(status,expected):
    error=WSServerHandshakeError(None,(),status=status,message='SECRET-MUST-NOT-APPEAR')
    text=connection_error(error)
    assert str(status) in text and expected in text
    assert 'SECRET' not in text


@pytest.mark.asyncio
@pytest.mark.parametrize('occupied',[False,True])
async def test_real_rejected_websocket_reports_key_or_duplicate(tmp_path,occupied):
    b=Broker('p'*40,'s'*48,'turn.example',[])
    async with TestClient(TestServer(create_app(b))) as c:
        existing=None
        if occupied:
            existing=await c.ws_connect('/ws/publisher',headers={'Authorization':'Bearer '+'p'*40})
            await existing.receive_json()
        s=Studio(tmp_path)
        s.public={'relay_url':str(c.make_url('')).rstrip('/'),'viewer_url':'https://pages.example/','publisher_key':('p' if occupied else 'x')*40}
        task=asyncio.create_task(run_bridge(s))
        try:
            for _ in range(100):
                if 'HTTP' in s.public_state:break
                await asyncio.sleep(.01)
            assert ('409' if occupied else '401') in s.public_state
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
            if existing:await existing.close()
