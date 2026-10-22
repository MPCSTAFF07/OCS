#!/usr/bin/env python3
"""Upgrade an installed OCS relay without changing TURN or nginx settings."""
import argparse
import json
import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


def atomic_private_json(path,data):
    descriptor,name=tempfile.mkstemp(dir=path.parent,prefix='.ocs-config-')
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8') as output:
            json.dump(data,output,indent=2)
        os.chmod(name,0o600)
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--pages-url',required=True)
    args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit('Run with sudo python3 deploy/update-management.py --pages-url ...')
    pages=urlsplit(args.pages_url)
    if pages.scheme!='https' or not pages.hostname or pages.username or pages.password or pages.query or pages.fragment:
        raise SystemExit('pages-url must be an HTTPS GitHub Pages project URL')
    conf=Path('/etc/obs-caster-share/config.json')
    target=Path('/opt/obs-caster-share/app.py')
    source=Path(__file__).resolve().parents[1]/'relay/app.py'
    if not conf.is_file() or not target.is_file():raise SystemExit('Install the OCS public relay first')
    original=json.loads(conf.read_text())
    config=dict(original)
    config.setdefault('admin_key',secrets.token_urlsafe(32))
    if len(config['admin_key'])<32 or config['admin_key'] in (config['publisher_key'],config['turn_secret']):
        raise SystemExit('admin_key must be distinct and at least 32 characters')
    origin=pages.scheme+'://'+pages.netloc
    config['origins']=list(dict.fromkeys([*config['origins'],origin]))
    content=source.read_text()
    compile(content,str(source),'exec')
    backup=conf.parent/'app-before-management.py'
    if not backup.exists():shutil.copy2(target,backup)
    previous=target.read_bytes()
    atomic_private_json(conf,config)
    target.write_text(content);target.chmod(0o644)
    try:
        subprocess.run(['systemctl','restart','obs-caster-relay'],check=True)
        subprocess.run(['systemctl','is-active','--quiet','obs-caster-relay'],check=True)
    except Exception:
        atomic_private_json(conf,original);target.write_bytes(previous)
        subprocess.run(['systemctl','restart','obs-caster-relay'],check=False)
        raise
    setup_path=Path('/root/obs-caster-connection.json')
    setup=json.loads(setup_path.read_text()) if setup_path.exists() else {}
    setup.update(admin_key=config['admin_key'],admin_url=args.pages_url.rstrip('/')+'/manage.html')
    atomic_private_json(setup_path,setup)
    print('Management relay updated. TURN and nginx settings were retained.')
    print('Management URL: '+setup['admin_url'])
    print('Show the private management key:')
    print('sudo python3 -c \'import json; print(json.load(open("/etc/obs-caster-share/config.json"))["admin_key"])\'')
    print('Update publisher.py on the broadcasting PC, then restart OCS once.')


if __name__=='__main__':main()
