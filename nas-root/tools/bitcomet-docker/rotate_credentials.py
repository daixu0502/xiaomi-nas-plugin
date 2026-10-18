"""Rotate local WebUI credentials from stdin, retaining container settings/data."""
import base64
import copy
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, ProxyHandler, build_opener


class DockerHTTP(http.client.HTTPConnection):
    def __init__(self): super().__init__('localhost',timeout=140)
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/docker.sock')


def docker(method, path, body=None):
    connection = DockerHTTP()
    try:
        raw = None if body is None else json.dumps(body).encode()
        connection.request(method,'/v1.41'+path,body=raw,headers={'Content-Type':'application/json'})
        response=connection.getresponse(); data=response.read()
        if response.status>=400: raise RuntimeError('Docker operation failed (HTTP %s)' % response.status)
        return json.loads(data) if data else None
    finally: connection.close()


def main():
    if os.geteuid()!=0: raise RuntimeError('Administrator required')
    new=json.load(sys.stdin)
    username,password=new['username'],new['password']
    if not all(isinstance(v,str) and 1<=len(v)<=256 and not any(c in v for c in '\r\n\x00') for v in (username,password)) or ':' in username:
        raise RuntimeError('Invalid credentials')
    name='bitcomet-webui'
    old=docker('GET','/containers/'+name+'/json')
    assert old['State']['Running'] and set(old['NetworkSettings']['Networks'])=={'bridge-net'}
    if not os.path.ismount('/nas/pool0'): raise RuntimeError('Storage pool unavailable')
    config=copy.deepcopy(old['Config']); host=copy.deepcopy(old['HostConfig'])
    config['Image']=old['Image']  # Preserve the exact installed image, not a mutable tag.
    env=dict(v.split('=',1) for v in config['Env'])
    old_user,old_password=env['WEBUI_USERNAME'],env['WEBUI_PASSWORD']
    env.update(WEBUI_USERNAME=username,WEBUI_PASSWORD=password)
    config['Env']=[k+'='+v for k,v in env.items()]
    host['NetworkMode']='bridge-net'
    endpoint=old['NetworkSettings']['Networks']['bridge-net']
    network={'IPAMConfig':{'IPv4Address':endpoint['IPAddress']},'Aliases':endpoint.get('Aliases') or []}
    config.update(HostConfig=host,NetworkingConfig={'EndpointsConfig':{'bridge-net':network}})
    credentials=Path('/data/bitcomet/credentials.env')
    previous=credentials.read_bytes()
    def save(data):
        temp=credentials.with_name('.credentials-'+secrets.token_hex(6))
        with temp.open('xb') as out: out.write(data)
        temp.chmod(0o600); os.replace(temp,credentials)
    stored=dict(v.split('=',1) for v in previous.decode().splitlines())
    stored.update(WEBUI_USERNAME=username,WEBUI_PASSWORD=password)
    payload=('\n'.join(k+'='+v for k,v in stored.items())+'\n').encode()
    binding=host['PortBindings']['8080/tcp'][0]
    url='http://'+binding['HostIp']+':'+binding['HostPort']+'/'
    opener=build_opener(ProxyHandler({}))
    def status(user,secret):
        auth=base64.b64encode((user+':'+secret).encode()).decode()
        try:
            with opener.open(Request(url,headers={'Authorization':'Basic '+auth}),timeout=4) as response:
                return response.status
        except HTTPError as e: return e.code
    backup=name+'-credentials-'+str(int(time.time()))
    stopped,renamed,detached,created=False,False,False,None
    try:
        save(payload)
        docker('POST','/containers/'+old['Id']+'/stop?t=120'); stopped=True
        docker('POST','/containers/'+old['Id']+'/rename?name='+quote(backup)); renamed=True
        docker('POST','/networks/bridge-net/disconnect',{'Container':old['Id'],'Force':False}); detached=True
        created=docker('POST','/containers/create?name='+name,config)['Id']
        docker('POST','/containers/'+created+'/start')
        for _ in range(30):
            try:
                if status(username,password)==200: break
            except (OSError,ValueError): pass
            time.sleep(1)
        else: raise RuntimeError('New credentials failed verification')
        if (old_user,old_password)!=(username,password) and status(old_user,old_password)!=401:
            raise RuntimeError('Old credentials remain accepted')
        current=docker('GET','/containers/'+created+'/json')
        assert current['State']['Running']
        # Remove only the stopped original container; never remove its data volumes.
        docker('DELETE','/containers/'+old['Id'])
        print(json.dumps({'updated':True,'username':username,'newLoginHTTP':200,'oldLoginHTTP':401,
                          'container':created,'network':list(current['NetworkSettings']['Networks'])}))
    except Exception:
        try:
            if created:
                docker('POST','/containers/'+created+'/stop?t=120')
                docker('DELETE','/containers/'+created)
            save(previous)
            if renamed: docker('POST','/containers/'+old['Id']+'/rename?name='+name)
            if detached: docker('POST','/networks/bridge-net/connect',{'Container':old['Id'],'EndpointConfig':network})
            if stopped: docker('POST','/containers/'+old['Id']+'/start')
            print('Credential change failed; previous configuration restored.',file=sys.stderr)
        except Exception:
            print('Rollback requires inspection; existing data volumes retained.',file=sys.stderr)
        raise


if __name__=='__main__':
    os.umask(0o077)
    main()
