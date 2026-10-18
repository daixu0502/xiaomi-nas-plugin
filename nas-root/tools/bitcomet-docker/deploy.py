"""One-time deployment on the NAS; refuses to overwrite an existing install."""
import json
import os
from pathlib import Path
import pwd
import secrets
import subprocess
import sys


def main():
    user, address = sys.argv[1:3]
    account = pwd.getpwnam(user)
    docker = '/data/docker/docker'
    def command(*args):
        return subprocess.check_output([docker, *args], text=True).strip()
    if os.geteuid() != 0 or not os.path.ismount('/nas/pool0'):
        raise RuntimeError('Requires administrator and a mounted storage pool')
    parent = Path('/nas/pool0') / user / 'data/Download'
    for p in (parent, *parent.parents):
        if p.is_symlink(): raise RuntimeError('Download directory must not contain symlinks')
    if not parent.is_dir(): raise RuntimeError('Download directory is missing')
    destination = parent / 'BitComet'
    credential_dir = Path('/data/bitcomet')
    if destination.exists() or credential_dir.exists():
        raise RuntimeError('Deployment paths already exist; inspect before retrying')
    if 'bitcomet-webui' in command('ps','-a','--format','{{.Names}}').splitlines():
        raise RuntimeError('Container already exists')
    if 'bitcomet_config' in command('volume','ls','--format','{{.Name}}').splitlines():
        raise RuntimeError('Configuration volume already exists')
    # Provision the dedicated network and its scoped NAT unit before deployment.
    command('network','inspect','bridge-net')
    os.umask(0o077)
    destination.mkdir(mode=0o750); os.chown(destination,account.pw_uid,account.pw_gid)
    credential_dir.mkdir(mode=0o700)
    credentials = credential_dir / 'credentials.env'
    password = secrets.token_urlsafe(18)
    credentials.write_text('WEBUI_USERNAME=admin\nWEBUI_PASSWORD='+password+'\nBITCOMET_WEBUI_PORT=8080\nBITCOMET_BT_PORT=6082\nTZ=Asia/Shanghai\n')
    credentials.chmod(0o600)
    image = 'minas-bitcomet-webui:2.22.0-u%d' % account.pw_uid
    command('volume','create','bitcomet_config')
    command('run','--rm','--network','none','--user','0:0',
            '--mount','type=volume,src=bitcomet_config,dst=/root/.config/BitComet,volume-nocopy',
            '--entrypoint','/bin/sh',image,'-c',
            'chown %d:%d /root/.config/BitComet && chmod 0700 /root/.config/BitComet' % (account.pw_uid,account.pw_gid))
    ident = command('run','-d','--name','bitcomet-webui','--network','bridge-net','--restart','unless-stopped',
            '--stop-timeout','120','--ulimit','nofile=65536:65536',
            '--security-opt','no-new-privileges:true','--cap-drop','ALL',
            '--log-opt','max-size=10m','--log-opt','max-file=2',
            '--env-file',str(credentials),
            '-p',address+':6080:8080/tcp','-p','6082:6082/tcp','-p','6082:6082/udp',
            '--mount','type=volume,src=bitcomet_config,dst=/root/.config/BitComet,volume-nocopy',
            '--mount','type=bind,src='+str(destination)+',dst=/root/Downloads',image)
    print(json.dumps({'container':ident,'username':'admin','credentialsFile':str(credentials),'downloads':str(destination),'web':'http://'+address+':6080/'}))


if __name__ == '__main__': main()
