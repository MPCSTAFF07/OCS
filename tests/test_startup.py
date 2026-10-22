import sys
from pathlib import Path
import pytest
from aiohttp import ClientSession, web

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import prepare_server,create_app


def test_duplicate_start_does_not_change_live_token_or_command(tmp_path):
    listener,studio,url=prepare_server(tmp_path,port=0)
    port=listener.getsockname()[1]
    command=studio.runtime/'command.txt'
    command.write_text('keep-current-output\nstart\n')
    try:
        with pytest.raises(OSError):prepare_server(tmp_path,port=port)
        assert (studio.runtime/'dock-url.txt').read_text()==url
        assert command.read_text()=='keep-current-output\nstart\n'
    finally:listener.close()
    restarted,new_studio,new_url=prepare_server(tmp_path,port=port)
    try:assert new_url!=url
    finally:restarted.close()


@pytest.mark.asyncio
async def test_reserved_socket_serves_authenticated_admin(tmp_path):
    listener,studio,url=prepare_server(tmp_path,port=0)
    port=listener.getsockname()[1]
    runner=web.AppRunner(create_app(studio,bridge_enabled=False))
    await runner.setup()
    try:
        await web.SockSite(runner,listener).start()
        async with ClientSession() as client:
            async with client.get(f'http://127.0.0.1:{port}/api/admin',headers={'Authorization':'Bearer '+studio.admin}) as response:
                assert response.status==200
                assert (await response.json())['public_state']=='未設定'
    finally:await runner.cleanup()
