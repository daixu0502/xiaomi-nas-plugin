#!/usr/bin/env python3
"""Per-user, verified static-core updates. No arbitrary URLs or root helper."""
import contextlib
from runtime_lock import core_update_lock
from plugin_paths import plugin_activity
from memory_paths import update_staging
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import stat
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from github_release import fetch, latest_tag, asset as release_asset

sys.dont_write_bytecode = True
from download_lib import DownloadError, atomic_json, core_path
from fetch_core import ASSETS

REPOSITORY = 'abcfy2/aria2-static-build'
BUSY = ('pending', 'checking', 'downloading', 'installing')


def state(m):
    try:
        result = json.loads((m.runtime / 'core-update.json').read_text())
        if result.get('state') in BUSY and time.time() - result.get('time', 0) > 30:
            try:
                cmd = Path('/proc/%d/cmdline' % int(result.get('workerPid', 0))).read_bytes()
                active = b'core_update.py' in cmd and str(m.home).encode() in cmd
            except (OSError, ValueError):
                active = False
            if not active:
                result.update(state='error', message='上次核心更新已中断，请重新检查版本；任务文件未被删除')
        return result
    except (OSError, ValueError):
        return {}


def write_state(m, **values):
    values['time'] = time.time()
    values['workerPid'] = os.getpid()
    atomic_json(m.runtime / 'core-update.json', values)


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?\d+\.\d+\.\d+', value):
        raise DownloadError('发布版本格式不支持')
    return tuple(map(int, value.lstrip('v').split('.')))


def binary_version(path):
    try:
        result = subprocess.run([str(path), '--version'], capture_output=True, text=True, timeout=10, check=True)
        match = re.search(r'aria2 version (\d+\.\d+\.\d+)', result.stdout)
        if not match: raise ValueError()
        return match.group(1)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise DownloadError('核心自检失败，无法获取 aria2 版本') from exc


def mihomo_proxy(home):
    """Read only this account's canonical Mihomo configuration."""
    from plugin_paths import open_state_directory
    try:
        directory = open_state_directory(Path(home).parent / 'mihomo', 'etc')
        try:
            fd = os.open('ports.env', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                    raise ValueError('端口配置权限异常')
                data = stream.read(4097)
        finally:
            os.close(directory)
        if len(data) > 4096: raise ValueError('端口配置过大')
        ports = re.findall(r'^MIXED_PORT=([0-9]+)$', data.decode('ascii'), re.M)
        if len(ports) != 1 or not 1024 <= int(ports[0]) <= 65535:
            raise ValueError('代理端口无效')
        return 'http://127.0.0.1:' + str(int(ports[0]))
    except (OSError, ValueError) as exc:
        raise DownloadError('无法读取当前账号的 Mihomo 代理端口，请先启动 Mihomo 或取消代理选项') from exc


def release(proxy, arch=None):
    arch = arch or platform.machine()
    if arch not in ASSETS: raise DownloadError('此设备架构暂不支持自动更新')
    tag = latest_tag(REPOSITORY, proxy); version_tuple(tag)
    filename, pinned = ASSETS[arch]
    asset = release_asset(REPOSITORY, tag, filename, proxy, 40 * 1024 * 1024)
    digest = asset.get('digest') or ('sha256:' + pinned if tag.lstrip('v') == '1.37.0' else '')
    if not re.fullmatch(r'sha256:[a-fA-F0-9]{64}', digest):
        raise DownloadError('发布方未提供有效 SHA-256，已拒绝更新')
    if not 0 < asset.get('size', 0) <= 40 * 1024 * 1024:
        raise DownloadError('核心压缩包大小异常')
    return {'latest': tag.lstrip('v'), 'digest': digest[7:].lower(), 'size': asset['size'], 'arch': arch,
            'url': 'https://github.com/' + REPOSITORY + '/releases/download/' + tag + '/' + filename}


def unpack(data, info, target):
    if len(data) != info['size'] or hashlib.sha256(data).hexdigest() != info['digest']:
        raise DownloadError('核心压缩包 SHA-256 或大小不匹配，拒绝安装')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = [f for f in archive.infolist() if Path(f.filename).name == 'aria2c' and not f.is_dir()]
        if len(files) != 1 or not 0 < files[0].file_size <= 80 * 1024 * 1024:
            raise DownloadError('核心压缩包内容异常')
        binary = archive.read(files[0])
    machine = 183 if info['arch'] == 'aarch64' else 62
    if len(binary) < 64 or binary[:6] != b'\x7fELF\x02\x01' or int.from_bytes(binary[18:20], 'little') != machine:
        raise DownloadError('核心不是匹配设备架构的 64 位 Linux ELF')
    with target.open('xb') as stream: stream.write(binary)
    target.chmod(0o755)
    if binary_version(target) != info['latest']: raise DownloadError('核心版本与发布信息不一致')


def wait_running(m, expected):
    for _ in range(40):
        try:
            if m.rpc('getVersion')['version'] == expected: return
        except DownloadError:
            pass
        time.sleep(.25)
    raise DownloadError('新核心未能正常启动')


def install_candidate(m, candidate, version):
    from service import live
    from service_client import control as managed_control, update_transaction
    target = m.var / 'core/aria2c'
    if target.parent.is_symlink() or target.is_symlink():
        raise DownloadError('核心目录异常，拒绝更新')
    target.parent.mkdir(mode=0o700, exist_ok=True)
    # Never modify src: firmware verifies it at boot and uninstalls on mismatch.
    # Staging and rollback stay on the runtime filesystem for atomic replacement.
    with tempfile.TemporaryDirectory(prefix='.core-update-', dir=target.parent) as folder:
        pending, previous = Path(folder)/'aria2c.new', Path(folder)/'aria2c.previous'
        shutil.copy2(candidate, pending)
        shutil.copy2(core_path(m), previous)
        old_version = binary_version(previous)
        with update_transaction(m):
            enabled = (m.var/'enabled').exists()
            running = bool(live(m))
            stopped = False
            try:
                if running:
                    managed_control(m, 'stop', temporary=True); stopped = True
                os.replace(pending, target)
                if running:
                    managed_control(m, 'start', temporary=True); wait_running(m, version)
            except Exception as exc:
                if running and not stopped: raise
                if running:
                    try:
                        managed_control(m, 'stop', temporary=True)
                    except Exception as stop_error:
                        shutil.copy2(previous, m.var/'aria2c.recovery')
                        raise DownloadError('新核心尚未停止，未强制替换；旧核心已保留在 var/aria2c.recovery，请检查日志') from stop_error
                os.replace(previous, target)
                if running:
                    managed_control(m, 'start', temporary=True)
                    try: wait_running(m, old_version)
                    except DownloadError as rollback_error:
                        raise DownloadError('新版本启动失败，旧核心已恢复但服务未启动，请查看服务日志') from rollback_error
                raise DownloadError('核心切换失败，已恢复旧核心：' + str(exc)) from exc
            finally:
                managed_control(m, 'finish', temporary=True)
                if enabled: (m.var/'enabled').touch(mode=0o600)
                else: (m.var/'enabled').unlink(missing_ok=True)


def queue(m, command, proxy=False):
    with plugin_activity(m.var) as activity_fd:
        try:
            with core_update_lock(m.var) as lock_fd:
                return queue_worker(m, command, proxy, activity_fd, lock_fd)
        except BlockingIOError:
            raise DownloadError('已有核心操作正在执行，请稍后再试') from None


def queue_worker(m, command, proxy, activity_fd, lock_fd):
    from qb_update import state as qb_state
    if qb_state(m).get('state') in BUSY:
        raise DownloadError('qBittorrent 正在检查或升级，请稍后再操作 aria2')
    previous = state(m)
    if previous.get('state') in BUSY and time.time() - previous.get('time', 0) < 900:
        raise DownloadError('已有核心检查或更新正在执行，请稍后查看结果')
    write_state(m, state='pending', command=command, message='正在准备' + ('检查更新…' if command == 'check' else '下载更新…'))
    try:
        with (m.runtime/'core-update.log').open('a') as log:
            subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), command, str(m.home),
                              'proxy' if proxy else 'direct', str(lock_fd)], pass_fds=(activity_fd, lock_fd),
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    except OSError as exc:
        write_state(m, state='error', message='无法启动更新进程')
        raise DownloadError('无法启动更新进程') from exc
    return {'pending': True}


def run(m, command, proxy, inherited=None):
    from service import tracker_error
    if command not in ('check', 'apply'): raise DownloadError('无效核心更新操作')
    if os.geteuid() == 0: raise DownloadError('核心更新禁止以 root 运行')
    m.ready()
    with contextlib.ExitStack() as held:
        try:
            held.enter_context(core_update_lock(m.var, inherited))
        except BlockingIOError:
            return
        common = {'command': command, 'proxy': proxy}
        try:
            write_state(m, **common, state='checking', message='正在检查当前渠道的最新版本…')
            proxy_url = mihomo_proxy(m.home) if proxy else False
            current = binary_version(core_path(m))
            info = release(proxy_url)
            available = version_tuple(info['latest']) > version_tuple(current)
            common.update(current=current, latest=info['latest'], updateAvailable=available)
            if command == 'check' or not available:
                write_state(m, **common, state='done', message='发现新版本，可下载并更新。' if available else '当前已是此渠道的最新版本。')
                return
            with tempfile.TemporaryDirectory(prefix='core-download-', dir=update_staging(m.home)) as folder:
                write_state(m, **common, state='downloading', message='正在下载并校验 SHA-256…')
                candidate = Path(folder)/'aria2c'
                unpack(fetch(info['url'], proxy_url, 40 * 1024 * 1024), info, candidate)
                write_state(m, **common, state='installing', message='正在保存任务并切换内核，下载将短暂中断…')
                install_candidate(m, candidate, info['latest'])
            common.update(current=info['latest'], updateAvailable=False)
            write_state(m, **common, state='done', message='更新完成，任务、设置和 Openlist 连接已保留。')
        except Exception as exc:
            # Shared network formatter mentions subscriptions; use core terminology.
            message = str(exc) if isinstance(exc, ValueError) else tracker_error(exc).replace('订阅', '核心发布源').replace('8 秒', '25 秒')
            write_state(m, **common, state='error', message=message)


if __name__ == '__main__':
    from service import manager
    os.umask(0o077)
    run(manager(sys.argv[2]), sys.argv[1], sys.argv[3] == 'proxy', int(sys.argv[4]) if len(sys.argv) > 4 else None)
