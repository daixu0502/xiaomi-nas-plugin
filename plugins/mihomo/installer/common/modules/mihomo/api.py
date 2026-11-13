"""Narrow registered-user Docker proxy API; never accepts arbitrary ports/commands."""
import json
from pathlib import Path
import re
import subprocess
import privileged_runtime as runtime

ACTIONS = {'docker_proxy_status':'status', 'docker_proxy_enable':'enable', 'docker_proxy_disable':'disable'}


def container_intents():
    result = runtime.docker_command(['ps', '-a', '--no-trunc', '--format', '{{.ID}} {{.State}}'], timeout=20)
    if result.returncode:
        raise ValueError('无法保存 Docker 容器启停状态，未更改代理')
    intents = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2 or not re.fullmatch('[0-9a-f]{64}', parts[0]) or parts[1] not in ('running', 'exited', 'created'):
            raise ValueError('容器处于特殊状态，暂不能重启 Docker')
        intents[parts[0]] = parts[1] == 'running'
    return intents


def restore_containers(intents):
    for ident, desired in intents.items():
        result = runtime.docker_command(['inspect', '--format', '{{.State.Running}}', ident], timeout=20)
        if result.returncode or result.stdout.strip() not in ('true', 'false'):
            raise ValueError('Docker 代理操作后无法确认容器状态：' + ident)
        if (result.stdout.strip() == 'true') != desired:
            result = runtime.docker_command(['start' if desired else 'stop', ident], timeout=60)
            if result.returncode:
                raise ValueError('Docker 代理操作后容器未恢复：' + ident)


def api(user, request):
    runtime.authorize(user, 'mihomo')
    action = ACTIONS.get(request.get('action'))
    if action is None:
        raise ValueError('未知 Docker 代理操作')
    port = runtime.registry().get('settings', {}).get(user, {}).get('mihomo', {}).get('proxyPort')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Docker 代理端口尚未由安装器登记，请重新安装插件')
    if 'port' in request and (type(request['port']) is not int or request['port'] != port):
        raise ValueError('不能操作其他插件端口')
    force = request.get('force', False)
    if type(force) is not bool:
        raise ValueError('无效检测参数')
    args = ['/bin/sh', str(Path(__file__).with_name('mihomo-docker-proxy')), action, str(port)]
    if action == 'status' and force:
        args.append('fresh')
    def invoke():
        env = {'PATH':'/data/docker:/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL':'C.UTF-8', 'PYTHONDONTWRITEBYTECODE':'1', 'MINAS_CACHE_USER':user}
        result = subprocess.run(args, input='', capture_output=True, text=True, env=env, timeout=110)
        if result.returncode:
            raise ValueError('Docker 代理助手执行失败')
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError('Docker 代理助手返回无效数据')
        return value
    if action == 'status':
        return invoke()
    with runtime.docker_transaction():
        active = subprocess.run(['systemctl', 'is-active', '--quiet', 'docker.service'],
                                capture_output=True, timeout=15).returncode == 0
        intents = container_intents() if active else {}
        try:
            return invoke()
        finally:
            # Changing the daemon proxy restarts Docker, including containers
            # with restart=no. Preserve both previously running and stopped ones.
            restore_containers(intents)
