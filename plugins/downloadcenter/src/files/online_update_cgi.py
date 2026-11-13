#!/usr/bin/env python3
"""Shared user-side CGI adapter; the NAS gateway supplies authentication."""
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
from urllib.parse import parse_qs


def intercept(plugin):
    values = parse_qs(os.environ.get('QUERY_STRING', ''), keep_blank_values=True)
    actions = values.get('action', [])
    if not any(value.startswith('plugin_update_') for value in actions):
        return False
    try:
        if len(actions) != 1 or actions[0] not in ('plugin_update_status', 'plugin_update_check', 'plugin_update_install', 'plugin_update_proxy'):
            raise ValueError('更新操作无效')
        if os.environ.get('REQUEST_METHOD') != 'POST' or os.environ.get('CONTENT_TYPE', '').split(';', 1)[0].strip().lower() != 'application/json':
            raise ValueError('在线更新仅接受 JSON POST 请求')
        length = os.environ.get('CONTENT_LENGTH', '0')
        if not length.isdigit() or int(length) > 16384:
            raise ValueError('更新请求过大')
        raw = sys.stdin.buffer.read(int(length))
        if len(raw) != int(length):
            raise ValueError('更新请求不完整')
        request = json.loads(raw or b'{}')
        if not isinstance(request, dict):
            raise ValueError('更新请求必须为 JSON 对象')
        user = pwd.getpwuid(os.getuid()).pw_name
        if not re.fullmatch('u[0-9]+', user):
            raise ValueError('请以 NAS 当前账号打开插件')
        home = Path('/home') / user / 'plugin' / plugin
        if json.loads((home / 'INFO').read_text())['plugin'] != plugin:
            raise ValueError('插件安装信息不匹配')
        broker = Path('/data/minas/plugins/updater/updater.py')
        if not broker.is_file():
            raise ValueError('在线更新组件尚未安装，请先用安装器更新一次插件')
        request['action'] = actions[0].removeprefix('plugin_update_')
        result = subprocess.run(['/usr/bin/sudo', '-n', '/usr/bin/python3', '-I', '-B', str(broker), 'api', '--user', user, '--plugin', plugin], input=json.dumps(request).encode(), capture_output=True, timeout=20, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'})
        try:
            response = json.loads(result.stdout)
        except (ValueError, UnicodeDecodeError):
            raise ValueError('无法调用在线更新组件，请检查安装授权') from None
        if not isinstance(response, dict):
            raise ValueError('更新组件返回格式无效')
    except Exception as error:
        response = {'ok': False, 'error': str(error) if isinstance(error, ValueError) else '在线更新请求失败，请检查安装授权'}
    print('Content-Type: application/json; charset=utf-8\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n\r\n', end='')
    print(json.dumps(response, ensure_ascii=False))
    raise SystemExit


if __name__ == '__main__':
    intercept(sys.argv[1])
