#!/usr/bin/env python3
"""Ubuntu installer. Keeps existing nginx sites and coturn configuration intact."""
import argparse
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


def run(*args):
    subprocess.run(args,check=True)


def nginx_include(text,domain):
    """Insert into exactly one matching TLS server block; fail if ambiguous."""
    blocks=[];stack=[];quote=None;comment=False;escaped=False
    for i,char in enumerate(text):
        if comment:
            if char=='\n':comment=False
            continue
        if escaped:escaped=False;continue
        if char=='\\':escaped=True;continue
        if quote:
            if char==quote:quote=None
            continue
        if char in ('"',"'"):quote=char;continue
        if char=='#':comment=True;continue
        if char=='{':stack.append(i)
        if char=='}':
            if not stack:raise ValueError('Unbalanced nginx config')
            start=stack.pop()
            prefix=text[max(0,start-50):start]
            body=text[start+1:i]
            names=re.findall(r'(?m)^\s*server_name\s+([^;]+);',body)
            if re.search(r'\bserver\s*$',prefix) and any(domain in n.split() for n in names) and re.search(r'\blisten\s+[^;]*\b443\b',body):
                blocks.append((start,i))
    if len(blocks)!=1:raise ValueError('Need exactly one HTTPS server block matching the domain')
    start,end=blocks[0]
    include='include /etc/obs-caster-share/nginx-location.conf;'
    if include in text[start:end]:return text
    return text[:end]+'\n    '+include+'\n'+text[end:]


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--domain',required=True,help='Existing HTTPS domain on this EC2')
    p.add_argument('--public-ip',required=True)
    p.add_argument('--private-ip',help='EC2 private IPv4; auto-detected if omitted')
    p.add_argument('--pages-url',required=True)
    p.add_argument('--nginx-site',help='Optional existing HTTPS vhost path; auto-detected if omitted')
    args=p.parse_args()
    if os.geteuid()!=0:raise SystemExit('Run with sudo python3 deploy/install.py ...')
    domain=args.domain.lower()
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',domain) or '..' in domain:
        raise SystemExit('Invalid domain')
    public=str(ipaddress.IPv4Address(args.public_ip))
    pages=urlsplit(args.pages_url)
    if pages.scheme!='https' or not pages.hostname or pages.username or pages.query or pages.fragment:
        raise SystemExit('pages-url must be an HTTPS GitHub Pages URL')
    origin=f'{pages.scheme}://{pages.netloc}'
    if args.private_ip:private=str(ipaddress.IPv4Address(args.private_ip))
    else:
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
            s.connect(('1.1.1.1',53));private=s.getsockname()[0]

    sites=[Path(args.nginx_site)] if args.nginx_site else list(Path('/etc/nginx/sites-enabled').glob('*'))
    candidates=[]
    for site in sites:
        if not site.is_file():continue
        try:
            original=site.read_text()
            modified=nginx_include(original,domain)
            candidates.append((site.resolve(),original,modified))
        except ValueError:continue
    if len(candidates)!=1:raise SystemExit('Cannot find one existing HTTPS nginx site. Specify --nginx-site /etc/nginx/sites-available/YOUR-SITE')
    site,original,modified=candidates[0]
    source=Path(__file__).resolve().parents[1]
    if not (source/'relay/app.py').is_file():raise SystemExit('Extract the whole ZIP first')
    # Refuse to overwrite a previous install from an incompatible service.
    confdir=Path('/etc/obs-caster-share');confdir.mkdir(mode=0o700,exist_ok=True)
    cfg_path=confdir/'config.json'
    cfg=json.loads(cfg_path.read_text()) if cfg_path.exists() else {'publisher_key':secrets.token_urlsafe(32),'turn_secret':secrets.token_urlsafe(48)}
    cfg.setdefault('admin_key',secrets.token_urlsafe(32))
    cfg.update(turn_host=domain,turn_port=3479,origins=[origin])
    run('apt-get','update')
    run('apt-get','install','-y','python3-venv','coturn')
    target=Path('/opt/obs-caster-share');target.mkdir(exist_ok=True)
    shutil.copy2(source/'relay/app.py',target/'app.py')
    run('python3','-m','venv',str(target/'.venv'))
    run(str(target/'.venv/bin/python'),'-m','pip','install','aiohttp>=3.12,<4')
    cfg_path.write_text(json.dumps(cfg));cfg_path.chmod(0o600)
    turn=f'''listening-port=3479
listening-ip={private}
relay-ip={private}
external-ip={public}/{private}
realm={domain}
fingerprint
pidfile=/tmp/obs-caster-turn.pid
userdb=/tmp/obs-caster-turn.sqlite
use-auth-secret
static-auth-secret={cfg['turn_secret']}
min-port=48900
max-port=49000
total-quota=40
user-quota=4
max-bps=2000000
bps-capacity=20000000
stale-nonce=600
no-cli
no-tls
no-dtls
no-multicast-peers
denied-peer-ip=0.0.0.0-0.255.255.255
denied-peer-ip=10.0.0.0-10.255.255.255
denied-peer-ip=127.0.0.0-127.255.255.255
denied-peer-ip=169.254.0.0-169.254.255.255
denied-peer-ip=172.16.0.0-172.31.255.255
denied-peer-ip=192.168.0.0-192.168.255.255
denied-peer-ip=224.0.0.0-255.255.255.255
denied-peer-ip=::1
denied-peer-ip=fc00::-fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff
denied-peer-ip=fe80::-febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff
'''
    (confdir/'turn.conf').write_text(turn);(confdir/'turn.conf').chmod(0o600)
    (confdir/'nginx-location.conf').write_text('''# Included within the existing HTTPS server block.
location ^~ /caster/ {
    proxy_pass http://127.0.0.1:8792/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 90s;
    proxy_send_timeout 90s;
    client_max_body_size 128k;
    access_log off;
}
''')
    for name,command in [('obs-caster-relay',f'{target}/.venv/bin/python {target}/app.py'),
                         ('obs-caster-turn',f'{shutil.which("turnserver")} -c {confdir}/turn.conf --log-file=stdout --simple-log')]:
        unit=f'''[Unit]
Description={name}
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
ExecStart={command}
Restart=on-failure
RestartSec=3
DynamicUser=yes
LoadCredential=config:{confdir / ('config.json' if name.endswith('relay') else 'turn.conf')}
Environment=CASTER_CONFIG=%d/config
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
[Install]
WantedBy=multi-user.target
'''
        # TURN and Python read their isolated systemd credential copies.
        if name.endswith('turn'):unit=unit.replace(f'-c {confdir}/turn.conf','-c %d/config')
        Path(f'/etc/systemd/system/{name}.service').write_text(unit)
    if modified!=original:
        backup=confdir/'nginx-site-before-caster.conf'
        if not backup.exists():backup.write_text(original)
        site.write_text(modified)
        try:run('nginx','-t')
        except Exception:
            site.write_text(original);raise
    else:run('nginx','-t')
    run('systemctl','daemon-reload')
    run('systemctl','enable','--now','obs-caster-relay','obs-caster-turn')
    run('systemctl','restart','obs-caster-relay','obs-caster-turn')
    run('systemctl','reload','nginx')
    setup={'relay_url':f'https://{domain}/caster','viewer_url':args.pages_url.rstrip('/')+'/','publisher_key':cfg['publisher_key'],'admin_key':cfg['admin_key'],'admin_url':args.pages_url.rstrip('/')+'/manage.html'}
    out=Path('/root/obs-caster-connection.json');out.write_text(json.dumps(setup,indent=2));out.chmod(0o600)
    print('Installed. Connection settings: sudo cat /root/obs-caster-connection.json')
    print('AWS Security Group / host firewall: allow TCP 443, UDP+TCP 3479, UDP 48900-49000.')
    print('Health check: '+setup['relay_url']+'/health')
    print('OBS dock: enter relay_url, viewer_url and publisher_key. Never put the key in GitHub.')
    print('GitHub Pages management: '+setup['admin_url']+' (use admin_key to sign in)')


if __name__=='__main__':main()
