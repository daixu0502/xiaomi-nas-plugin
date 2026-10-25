#!/usr/bin/env python3
"""User-side CGI: fixed static assets and the existing narrow sudo broker."""
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
from urllib.parse import parse_qs,urlsplit
sys.dont_write_bytecode=True
DIRECTORY=Path(__file__).resolve().parent
ACTIONS={'status','list','details','search','preview','apply','history','preview_undo'}


def reply(value):
    print('Content-Type: application/json; charset=utf-8\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n\r\n',end='')
    print(json.dumps(value,ensure_ascii=False))


def main():
    action=parse_qs(os.environ.get('QUERY_STRING',''),keep_blank_values=True).get('action',[None])[0]
    if action is None:
        name=Path(urlsplit(os.environ.get('REQUEST_URI','/index.html')).path).name
        types={'index.html':'text/html; charset=utf-8','app.js':'application/javascript; charset=utf-8','client-bridge.js':'application/javascript; charset=utf-8','palette.css':'text/css; charset=utf-8','style.css':'text/css; charset=utf-8','icon.png':'image/png'}
        if name in ('','mediaeditor.cgi'): name='index.html'
        if name not in types:
            print('Status: 404 Not Found\r\nContent-Type: text/plain\r\n\r\nNot found');return
        print('Content-Type: '+types[name]+'\r\nCache-Control: no-cache\r\nX-Content-Type-Options: nosniff\r\n\r\n',end='',flush=True)
        sys.stdout.buffer.write((DIRECTORY/name).read_bytes());return
    if action not in ACTIONS: raise ValueError('未知操作')
    if os.environ.get('REQUEST_METHOD')!='POST': raise ValueError('接口仅接受 POST 请求')
    if os.environ.get('CONTENT_TYPE','').split(';',1)[0].strip().lower()!='application/json': raise ValueError('接口需要 JSON 请求')
    length=os.environ.get('CONTENT_LENGTH','0')
    if not length.isdigit() or int(length)>131072: raise ValueError('请求长度无效或超过限制')
    user=pwd.getpwuid(os.getuid()).pw_name
    if not re.fullmatch(r'u[0-9]+',user): raise ValueError('请以 NAS 当前账号打开插件')
    body=sys.stdin.buffer.read(int(length))
    if len(body)!=int(length): raise ValueError('请求内容不完整')
    request=json.loads(body or b'{}')
    if not isinstance(request,dict): raise ValueError('请求必须为 JSON 对象')
    request.update(action=action,pluginUser=user)
    result=subprocess.run(['/usr/bin/sudo','-n','/data/minas/privileged/minas-helper','mediaeditor'],input=json.dumps(request).encode(),capture_output=True,timeout=100,env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LC_ALL':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
    if result.returncode: raise ValueError('无法调用公共权限组件，请检查插件授权')
    value=json.loads(result.stdout)
    if not isinstance(value,dict): raise ValueError('权限组件返回格式无效')
    info=json.loads((Path('/home')/user/'plugin/mediaeditor/INFO').read_text())
    value['pluginVersion']=info['version']
    reply(value)


try: main()
except subprocess.TimeoutExpired: reply({'ok':False,'error':'请求超时，请先查看修改记录再重试保存'})
except Exception as error: reply({'ok':False,'error':str(error)})
