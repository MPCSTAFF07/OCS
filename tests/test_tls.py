import datetime
import ipaddress
import ssl
import sys
from pathlib import Path

import pytest
from aiohttp import web,ClientSession,TCPConnector,ClientConnectorCertificateError
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from publisher import create_tls_context


@pytest.mark.asyncio
async def test_additional_ca_support_preserves_certificate_verification(tmp_path):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')])
    now=datetime.datetime.now(datetime.timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
          .not_valid_after(now+datetime.timedelta(days=1))
          .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
          .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256()))
    certfile=tmp_path/'test-cert.pem';keyfile=tmp_path/'test-key.pem'
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    server_ssl=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);server_ssl.load_cert_chain(certfile,keyfile)
    app=web.Application()
    async def health(request):return web.json_response({'ok':True})
    app.router.add_get('/health',health)
    runner=web.AppRunner(app);await runner.setup()
    try:
        site=web.TCPSite(runner,'127.0.0.1',0,ssl_context=server_ssl);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        ctx=create_tls_context()
        assert ctx.check_hostname and ctx.verify_mode==ssl.CERT_REQUIRED
        assert ctx.cert_store_stats()['x509_ca']>0
        async with ClientSession(connector=TCPConnector(ssl=ctx)) as client:
            with pytest.raises(ClientConnectorCertificateError):await client.get(f'https://127.0.0.1:{port}/health')
        ctx.load_verify_locations(cafile=str(certfile))
        async with ClientSession(connector=TCPConnector(ssl=ctx)) as client:
            async with client.get(f'https://127.0.0.1:{port}/health') as response:
                assert (await response.json())=={'ok':True}
    finally:await runner.cleanup()
