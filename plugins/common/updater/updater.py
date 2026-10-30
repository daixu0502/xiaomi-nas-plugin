#!/usr/bin/env python3
"""Signed, account-scoped plugin updates. Installed as a fixed root broker."""
import argparse
import base64
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.dont_write_bytecode = True
VERSION = '2.0.0'
REPOSITORY = 'daixu0502/xiaomi-nas-plugin'
BASE = Path('/data/minas/plugins/updater')
RECOVERY = Path('/data/minas/plugins/recovery')
HOMES = Path('/home')
PLUGINS = ('dockermanager', 'mihomo', 'taskcenter', 'nascenter', 'sshrecovery', 'quickshare', 'downloadcenter', 'mediaeditor')
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}
MAX_PACKAGE = 256 * 1024 * 1024
MAX_TREE = 768 * 1024 * 1024
MANIFEST = 'plugin-updates.json'
SIGNATURE = 'plugin-updates.sig'
CORE_FILES = ('updater.py', 'release-public-key.txt')
REGISTRY_DIR = Path('/data/plugin')
PRIVILEGED = Path('/data/minas/privileged')
SUDOERS = Path('/etc/sudoers.d')
SYSTEM_UNITS = Path('/etc/systemd/system')
INSTALL_LOCK = REGISTRY_DIR / '.local-plugin-installer.lock'


def require(ok, message):
    if not ok:
        raise ValueError(message)


def version(value):
    require(isinstance(value, str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', value), '版本格式无效')
    return tuple(map(int, value.split('.')))


def plain(path):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, '路径无效')
    for parent in (path, *path.parents):
        require(not parent.is_symlink(), '拒绝重定向路径')
    return path


def protected(path, mode=0o700):
    path = plain(path)
    if not path.exists():
        protected(path.parent, 0o755 if path == BASE else 0o700)
        path.mkdir(mode=mode)
    metadata = path.stat()
    require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == 0 and not metadata.st_mode & 0o022, '升级目录权限异常')
    return path


def atomic(path, content, mode=0o600):
    path = plain(path)
    protected(path.parent)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), mode)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode()


def save(path, value):
    atomic(path, encoded(value))


def read(path, default=None):
    if not Path(path).exists() and default is not None:
        return default
    return json.loads(plain(path).read_text(encoding='utf-8'))


@contextlib.contextmanager
def lock(path, wait=True):
    protected(path.parent)
    with open(plain(path), 'a') as handle:
        deadline = time.monotonic() + (wait if type(wait) in (int, float) else 0)
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait is True else fcntl.LOCK_NB))
                break
            except BlockingIOError:
                if wait is False or time.monotonic() >= deadline:
                    raise ValueError('另一个更新或安装任务仍在执行，请稍后重试') from None
                time.sleep(0.2)
        yield


def run(args, **kwargs):
    result = subprocess.run([str(x) for x in args], env=ENV, capture_output=True, timeout=kwargs.pop('timeout', 120), **kwargs)
    require(result.returncode == 0, '更新步骤执行失败，请查看升级记录')
    return result.stdout


def identity(user, plugin, caller=False):
    require(re.fullmatch(r'u[0-9]+', user or '') and plugin in PLUGINS, '插件或账号无效')
    account = pwd.getpwnam(user)
    if caller:
        require(os.geteuid() == 0 and os.environ.get('SUDO_USER') == user and os.environ.get('SUDO_UID') == str(account.pw_uid), '升级请求账号不匹配')
    home = HOMES / user / 'plugin' / plugin
    plain(home)
    info = read(home / 'INFO')
    require(info.get('plugin') == plugin, '插件尚未安装')
    version(info['version'])
    record = RECOVERY / 'records' / (user + '-' + plugin)
    require(record.is_dir() and not (record / 'removed').exists(), '插件尚未登记恢复信息，请重新运行安装器')
    return home, account, info


def user_dir(user):
    require(re.fullmatch(r'u[0-9]+', user), '账号无效')
    return protected(BASE / 'users' / user)


def plugin_dir(user, plugin):
    require(plugin in PLUGINS, '插件无效')
    return protected(user_dir(user) / plugin)


def proxy_settings(value, previous=None):
    previous = previous or {}
    require(isinstance(value, dict), '代理设置无效')
    enabled = value.get('enabled', False)
    require(isinstance(enabled, bool), '代理开关无效')
    url = value.get('url', '')
    require(isinstance(url, str) and len(url) <= 1024, '代理地址无效')
    url = url.strip()
    if url:
        parsed = urllib.parse.urlsplit(url)
        require(parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password and parsed.path in ('', '/') and not parsed.query and not parsed.fragment, '请输入 HTTP/HTTPS 代理地址，认证信息填写在单独字段')
        require(1 <= (parsed.port or (443 if parsed.scheme == 'https' else 80)) <= 65535, '代理端口无效')
        require(not any(ord(c) < 33 for c in url), '代理地址含无效字符')
        url = url.rstrip('/')
    require(not enabled or url, '启用代理时需要填写地址')
    username = value.get('username', '')
    password = value.get('password', '')
    require(all(isinstance(x, str) and len(x) <= 512 and '\n' not in x and '\r' not in x for x in (username, password)), '代理认证信息无效')
    if not password and not value.get('clearPassword'):
        password = previous.get('password', '') if username == previous.get('username', '') and url == previous.get('url', '') else ''
    return dict(enabled=enabled, url=url, username=username, password=password)


def settings(user, public=False):
    value = read(user_dir(user) / 'settings.json', dict(enabled=False, url='', username='', password=''))
    if public:
        return {k: value[k] for k in ('enabled', 'url', 'username')} | {'hasPassword': bool(value.get('password'))}
    return value


def allowed_url(url):
    parsed = urllib.parse.urlsplit(url)
    require(parsed.scheme == 'https' and not parsed.username and not parsed.password and parsed.port in (None, 443), '下载地址无效')
    if parsed.hostname == 'api.github.com':
        require(parsed.path.startswith('/repos/' + REPOSITORY + '/releases/'), '仅允许官方仓库')
    elif parsed.hostname == 'github.com':
        require(parsed.path.startswith('/' + REPOSITORY + '/releases/download/'), '仅允许官方发布文件')
    else:
        require(parsed.hostname in ('release-assets.githubusercontent.com', 'objects.githubusercontent.com'), '下载重定向不受信任')
    return url


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        allowed_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener(proxy):
    mapping = {}
    if proxy.get('enabled'):
        url = urllib.parse.urlsplit(proxy['url'])
        credentials = ''
        if proxy.get('username'):
            credentials = urllib.parse.quote(proxy['username'], safe='') + ':' + urllib.parse.quote(proxy.get('password', ''), safe='') + '@'
        address = urllib.parse.urlunsplit((url.scheme, credentials + url.netloc, '', '', ''))
        mapping = {'http': address, 'https': address}
    return urllib.request.build_opener(urllib.request.ProxyHandler(mapping), Redirect())


def fetch():
    """Unprivileged network reader. Credentials only arrive on stdin."""
    require(os.geteuid() != 0, '下载必须使用插件用户')
    request = json.loads(sys.stdin.buffer.read(16385))
    url, limit = allowed_url(request['url']), request['limit']
    require(isinstance(limit, int) and 0 < limit <= MAX_PACKAGE, '下载大小无效')
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Xiaomi-NAS-Plugin-Updater/' + VERSION, 'Accept': 'application/vnd.github+json' if 'api.github.com' in url else 'application/octet-stream'})
        with opener(request['proxy']).open(req, timeout=25) as response:
            allowed_url(response.url)
            length = response.headers.get('Content-Length')
            require(not length or (length.isdigit() and int(length) <= limit), '下载文件超过大小限制')
            total = 0
            while True:
                block = response.read(65536)
                if not block:
                    break
                total += len(block)
                require(total <= limit, '下载文件超过大小限制')
                sys.stdout.buffer.write(block)
    except Exception:
        # Never echo URLs, proxy credentials or authentication headers.
        sys.exit(1)


def download(user, url, proxy, limit, output=None, progress=None):
    request = encoded(dict(url=allowed_url(url), proxy=proxy, limit=limit))
    cmd = ['runuser', '-u', user, '--', '/usr/bin/python3', '-I', '-B', str(BASE / 'updater.py'), 'fetch']
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=ENV)
    buffer, total, last = io.BytesIO() if output is None else output, 0, 0
    try:
        process.stdin.write(request)
        process.stdin.close()
        while True:
            block = process.stdout.read1(65536)
            if not block:
                break
            total += len(block)
            require(total <= limit, '安装包大小超出签名清单')
            buffer.write(block)
            if progress and time.monotonic() - last > 1:
                progress(total, limit)
                last = time.monotonic()
        require(process.wait(timeout=35) == 0, 'GitHub 下载失败，请检查网络或代理；也可能尚未发布完整安装包')
        return buffer.getvalue() if output is None else total
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()


def tag_url(tag, asset):
    require(isinstance(tag, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,95}', tag), '发布标签无效')
    require(isinstance(asset, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', asset), '发布文件名无效')
    return 'https://github.com/' + REPOSITORY + '/releases/download/' + tag + '/' + asset


def verify_manifest(raw, signature, directory):
    require(len(raw) <= 131072 and len(signature) <= 2048, '更新清单过大')
    atomic(directory / MANIFEST, raw)
    atomic(directory / SIGNATURE, signature)
    try:
        run(['openssl', 'dgst', '-sha256', '-verify', BASE / 'release-public-key.txt', '-signature', directory / SIGNATURE, directory / MANIFEST], timeout=15)
    except ValueError:
        raise ValueError('更新清单签名校验失败，已拒绝更新') from None
    value = json.loads(raw)
    require(value.get('schema') == 1 and value.get('repository') == REPOSITORY, '更新清单格式或仓库不匹配')
    require(version(value['minUpdater']) <= version(VERSION), '需要先通过安装器升级在线更新组件')
    require(isinstance(value.get('plugins'), dict) and set(value['plugins']) == set(PLUGINS), '更新清单缺少插件')
    require(value.get('format') == 2 and isinstance(value.get('components'),dict), '需要格式 2 组件清单')
    require(set(value['components']) == {'manager','updater','recovery','privileged'}, '发布清单缺少共有组件')
    for name,item in value['components'].items():
        require(name in ('manager','updater','recovery','privileged'),'未知公共组件')
        version(item['version'])
        require(item['asset']=='minas-'+name+'-v'+item['version']+'.tar.gz','组件文件名无效')
        require(isinstance(item['size'],int) and 0<item['size']<=MAX_PACKAGE and re.fullmatch('[0-9a-f]{64}',item['sha256']),'组件大小或摘要无效')
    tag_url(value['tag'], MANIFEST)
    for plugin, item in value['plugins'].items():
        version(item['version'])
        require(item.get('asset') == plugin + '-v' + item['version'] + '.tar.gz', '安装包名称与版本不匹配')
        require(isinstance(item.get('size'), int) and 0 < item['size'] <= MAX_PACKAGE and re.fullmatch('[0-9a-f]{64}', item.get('sha256', '')), '安装包摘要或大小无效')
        require(item.get('architecture') == 'aarch64', '安装包架构不受支持')
        require(isinstance(item.get('notes', ''), str) and len(item.get('notes', '')) <= 8000, '更新说明过大')
    return value


def candidate(user, plugin):
    value = read(plugin_dir(user, plugin) / 'candidate.json', {})
    return value


def latest(user, plugin, job, proxy, use_cache=False):
    report(job, 'checking', '正在通过代理检查 GitHub 更新' if proxy['enabled'] else '正在检查 GitHub 更新', 5)
    cache = read(user_dir(user) / 'release-cache.json', {}) if use_cache else {}
    if cache and time.time() - cache.get('checkedAt', 0) < 300:
        manifest = verify_manifest(base64.b64decode(cache['manifest']), base64.b64decode(cache['signature']), job)
        save(plugin_dir(user, plugin) / 'candidate.json', cache)
        return manifest
    release = json.loads(download(user, 'https://api.github.com/repos/' + REPOSITORY + '/releases/latest', proxy, 2 * 1024 * 1024))
    require(not release.get('draft') and not release.get('prerelease'), '当前发布不是正式版本')
    tag = release['tag_name']
    assets = {item['name']: item for item in release.get('assets', [])}
    require(MANIFEST in assets and SIGNATURE in assets, '最新发布尚未提供签名更新清单')
    for asset in (MANIFEST, SIGNATURE):
        require(assets[asset].get('browser_download_url') == tag_url(tag, asset), '更新清单来源不匹配')
    raw = download(user, tag_url(tag, MANIFEST), proxy, 131072)
    signature = download(user, tag_url(tag, SIGNATURE), proxy, 2048)
    manifest = verify_manifest(raw, signature, job)
    require(manifest['tag'] == tag, '发布标签与签名清单不一致')
    for item in [*manifest['plugins'].values(), *manifest['components'].values()]:
        require(item['asset'] in assets and assets[item['asset']].get('browser_download_url') == tag_url(tag, item['asset']) and assets[item['asset']]['size'] == item['size'], '发布安装包不完整或大小不匹配')
    value = dict(manifest=base64.b64encode(raw).decode(), signature=base64.b64encode(signature).decode(), checkedAt=time.time())
    save(user_dir(user) / 'release-cache.json', value)
    save(plugin_dir(user, plugin) / 'candidate.json', value)
    return manifest


def report(job, phase, message, percent, **extra):
    previous = read(job / 'status.json', {})
    logs = previous.get('logs', [])
    if not logs or logs[-1]['message'] != message:
        logs = (logs + [dict(time=time.time(), message=message)])[-40:]
    value = dict(previous, phase=phase, message=message, percent=percent, updatedAt=time.time(), logs=logs, **extra)
    save(job / 'status.json', value)


def load_recovery():
    spec = importlib.util.spec_from_file_location('minas_recovery_update', RECOVERY / 'recovery.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Only user-owned plugin code is written by this unprivileged process. No shell
# or package installer supplied by the browser is ever executed as root.
def cleanup_locked(user, plugin, r):
    """Caller holds installer + record locks. Never remove an uncommitted backup."""
    directory = plugin_dir(user, plugin)
    jobs = plain(directory / 'jobs')
    entries = [plain(p) for p in jobs.iterdir() if re.fullmatch('[0-9a-f]{32}', p.name)] if jobs.exists() else []
    journals = [(p, read(p / 'journal.json', {})) for p in entries]
    if any(j.get('state') == 'prepared' for _, j in journals):
        return False
    home, _, _ = identity(user, plugin)
    src = r.source_path(home, user, plugin)
    r.verify(user, plugin, home, src)
    record = protected(RECOVERY / 'records' / (user + '-' + plugin))
    current = plain(r.load_generation(record))
    require(current.parent == record and re.fullmatch('generation-[0-9]+', current.name), '当前恢复快照路径无效')
    expected = read(current / 'INFO')
    live = read(home / 'INFO')
    require(expected['version'] == live['version'] and expected['abstract'] == live['abstract'] and r.digest(current / 'src') == live['abstract'], '当前恢复快照未通过校验，保留清理材料')
    # All paths are within protected root storage; rmtree does not follow the
    # INFO symlink inside snapshots. No user etc/var/tmp directory is traversed.
    for old in record.iterdir():
        if old != current and re.fullmatch('generation-[0-9]+', old.name):
            protected(old)
            shutil.rmtree(old)
    for job, journal in journals:
        status_value = read(job / 'status.json', {})
        if status_value.get('running') and time.time() - status_value.get('updatedAt', 0) > 45:
            unit = 'minas-update-' + job.name + '.service'
            if subprocess.run(['systemctl', 'is-active', '--quiet', unit], env=ENV).returncode != 0:
                committed = journal.get('state') == 'committed'
                report(job, 'complete' if committed else 'interrupted',
                       '更新已完成，请重新打开插件窗口' if committed else '更新任务已中断，请重新检查更新',
                       100 if committed else 0, running=False)
                status_value = read(job / 'status.json', {})
        settled = journal.get('state') in ('committed', 'rolled_back') or (not journal and status_value.get('phase') in ('complete', 'failed', 'interrupted'))
        if not settled or status_value.get('running'):
            continue
        protected(job)
        for name in ['package.tar.gz', 'package', 'rollback.tar.gz', 'backup'] + [p.name for p in job.glob('component-*')]:
            path = plain(job / name)
            if path.is_dir():
                protected(path)
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        if journal and 'cleanedAt' not in journal:
            journal['cleanedAt'] = time.time()
            save(job / 'journal.json', journal)
    return True


def remove_finished_jobs(directory, queue_locked=False):
    """Drop settled jobs, keeping only the current result needed by pollers."""
    jobs = plain(directory / 'jobs')
    if not jobs.exists():
        return
    with contextlib.nullcontext() if queue_locked else lock(directory / 'queue.lock'):
        current = read(directory / 'current.json', {})
        for job in jobs.iterdir():
            if not re.fullmatch('[0-9a-f]{32}', job.name):
                continue
            protected(job)
            journal = read(job / 'journal.json', {})
            state = read(job / 'status.json', {})
            if journal.get('state') not in (None, 'committed', 'rolled_back') or state.get('running'):
                continue
            if state.get('phase') not in ('complete', 'failed', 'interrupted'):
                continue
            # Material removal must have passed snapshot verification first.
            if any((job / name).exists() for name in ('package.tar.gz', 'package', 'rollback.tar.gz', 'backup')):
                continue
            if current.get('id') == job.name:
                result = {key: state[key] for key in ('id', 'action', 'phase', 'message', 'percent', 'updatedAt', 'installedVersion', 'verification') if key in state}
                result['running'] = False
                current = dict(result=result)
                save(directory / 'current.json', current)
            shutil.rmtree(job)


def cleanup_plugin(user, plugin, worker_locked=False):
    r = load_recovery()
    record = RECOVERY / 'records' / (user + '-' + plugin)
    if not record.is_dir() or (record / 'removed').exists():
        return
    directory = plugin_dir(user, plugin)
    # External maintenance must wait until the worker has finished reporting.
    # A worker already owns this lock; flock is not reentrant across handles.
    with contextlib.nullcontext() if worker_locked else lock(directory / 'worker.lock'):
        with lock(INSTALL_LOCK), r.record_lock(record):
            if cleanup_locked(user, plugin, r):
                remove_finished_jobs(directory)


def load_manager():
    root = Path('/data/minas/plugins/manager')
    require((root / 'manager.py').is_file(), '需要先通过统一安装器切换')
    sys.path.insert(0, str(root))
    import manager
    return manager


def prepare_bundle(user, plugin, archive, job, proxy, release):
    load_manager()
    import packages
    from layout import BASE as manager_root
    root = packages.unpack(archive, job / 'package')
    run(['openssl','dgst','-sha256','-verify',BASE/'release-public-key.txt',
         '-signature',root/'suite.sig',root/'suite.json'],timeout=15)
    manifest = read(root/'suite.json')
    require(manifest.get('format') == 2 and set(manifest['plugins']) == {plugin}, '更新包身份或格式不匹配')
    require(manifest['plugins'][plugin]['version'] == release['plugins'][plugin]['version'], '更新版本不匹配')
    installed = {'manager':manager_root,'recovery':RECOVERY,'updater':BASE,'privileged':PRIVILEGED}
    for name, component in manifest['components'].items():
        require(name in installed and component == release['components'][name], '组件依赖与签名发布清单不匹配')
        prefix = 'components/' + name + '/'
        files = {key[len(prefix):]:value for key,value in manifest['files'].items() if key.startswith(prefix)}
        reusable = all((installed[name]/key).is_file() and not (installed[name]/key).is_symlink()
                       and hashlib.sha256((installed[name]/key).read_bytes()).hexdigest()==value['sha256']
                       for key,value in files.items())
        if reusable:
            for relative in files:
                target=root/'components'/name/relative
                protected(target.parent)
                atomic(target,(installed[name]/relative).read_bytes(),0o755 if relative.endswith('.py') or relative in ('minas-helper','component_admin.py') else 0o644)
        else:
            path=job/('component-'+name+'.tar.gz')
            with path.open('xb') as output:
                count=download(user,tag_url(release['tag'],component['asset']),proxy,component['size'],output)
            require(count==component['size'] and hashlib.sha256(path.read_bytes()).hexdigest()==component['sha256'],'组件下载摘要不匹配')
            expanded=packages.unpack(path,job/('component-'+name))
            actual={p.relative_to(expanded).as_posix() for p in expanded.rglob('*') if p.is_file()}
            require(actual=={prefix+key for key in files},'组件包含未声明文件')
            (root/'components').mkdir(exist_ok=True)
            shutil.move(str(expanded/'components'/name),root/'components'/name)
            shutil.rmtree(expanded);path.unlink()
    return packages.Bundle(root, BASE/'release-public-key.txt')


def transaction(user, plugin, archive, payload, job):
    report(job,'installing','正在通过统一安装引擎安装并验证',65)
    load_manager().apply('update',[(user,plugin)],payload,owned_update=(user,plugin))
    save(job/'journal.json',dict(state='committed'))
    report(job,'complete','更新完成，请关闭插件窗口后重新打开',100,
           installedVersion=payload.manifest['plugins'][plugin]['version'],running=False)


def public_candidate(user, plugin, installed):
    c = candidate(user, plugin)
    if not c:
        return None
    try:
        data = json.loads(base64.b64decode(c['manifest']))
        item = data['plugins'][plugin]
        return dict(version=item['version'], notes=item.get('notes', ''), size=item['size'], available=version(item['version']) > version(installed), checkedAt=c['checkedAt'], releaseUrl='https://github.com/' + REPOSITORY + '/releases/tag/' + data['tag'])
    except (KeyError, ValueError):
        return None


def status(user, plugin):
    _, _, info = identity(user, plugin)
    directory = plugin_dir(user, plugin)
    current = read(directory / 'current.json', {})
    job = current.get('result', {})
    if re.fullmatch('[0-9a-f]{32}', current.get('id', '')):
        job = read(directory / 'jobs' / current['id'] / 'status.json', {})
        if not job:
            # Cleanup atomically publishes the final result before removing the
            # job. A poll may have read the old pointer immediately beforehand.
            job = read(directory / 'current.json', {}).get('result', {})
        if job.get('running') and time.time() - job.get('updatedAt', 0) > 45:
            active = subprocess.run(['systemctl', 'is-active', '--quiet', 'minas-update-' + current['id'] + '.service'], env=ENV).returncode == 0
            if not active:
                journal = read(directory / 'jobs' / current['id'] / 'journal.json', {})
                if journal.get('state') == 'committed':
                    job = dict(job, running=False, phase='complete', message='更新已完成，请重新打开插件窗口', percent=100)
                else:
                    job = dict(job, running=False, phase='interrupted', message='更新任务意外结束；已保留记录，重新检查前会尝试恢复', percent=0)
    last_check = candidate(user, plugin).get('checkedAt', 0)
    if job.get('action') == 'check':
        last_check = max(last_check, job.get('updatedAt', 0))
    return dict(ok=True, updaterVersion=VERSION, installedVersion=info['version'], proxy=settings(user, True), latest=public_candidate(user, plugin, info['version']), job=job, checkDue=time.time() - last_check >= 21600)


def api(user, plugin, request):
    identity(user, plugin, True)
    require(isinstance(request, dict), '请求格式无效')
    action = request.get('action')
    require(action in ('status', 'proxy', 'check', 'install'), '更新操作无效')
    directory = plugin_dir(user, plugin)
    if action == 'status':
        return status(user, plugin)
    with lock(INSTALL_LOCK, False), lock(directory / 'queue.lock', False):
        require(not Path('/data/minas/plugins/manager/maintenance.json').exists(), '插件正在维护，请稍后操作')
        current_status = status(user, plugin)
        if action == 'check' and request.get('background') is True and (current_status['job'].get('running') or not current_status['checkDue']):
            return current_status
        require(not current_status['job'].get('running'), '当前插件的更新任务尚未结束')
        if action == 'proxy':
            with lock(user_dir(user) / 'settings.lock'):
                save(user_dir(user) / 'settings.json', proxy_settings(request.get('proxy'), settings(user)))
                (user_dir(user) / 'release-cache.json').unlink(missing_ok=True)
            return status(user, plugin)
        if action == 'install':
            latest_item = status(user, plugin)['latest']
            require(latest_item and latest_item['available'] and request.get('version') == latest_item['version'], '没有可安装的新版本，请重新检查更新')
        job_id = uuid.uuid4().hex
        job = protected(directory / 'jobs' / job_id)
        save(job / 'request.json', dict(user=user, plugin=plugin, action=action, background=request.get('background') is True))
        report(job, 'queued', '更新任务已排队', 0, running=True, action=action, id=job_id)
        save(directory / 'current.json', dict(id=job_id))
        try:
            run(['systemd-run', '--quiet', '--collect', '--unit=minas-update-' + job_id, '--property=Type=exec', '--property=UMask=0077', '--property=RuntimeMaxSec=30min', '/usr/bin/python3', '-I', '-B', BASE / 'updater.py', 'worker', '--user', user, '--plugin', plugin, '--job', job_id], timeout=15)
        except Exception:
            report(job, 'failed', '无法启动更新后台任务', 0, running=False)
            remove_finished_jobs(directory, queue_locked=True)
            raise
        return status(user, plugin)


def worker(user, plugin, job_id):
    require(re.fullmatch('[0-9a-f]{32}', job_id or ''), '任务编号无效')
    identity(user, plugin)
    directory = plugin_dir(user, plugin)
    job = protected(directory / 'jobs' / job_id)
    # A previous check can already report its result while still finishing
    # cleanup. Serialize the accepted next job instead of failing its queue.
    with lock(directory / 'worker.lock'):
        try:
            recover_plugin(user, plugin, excluding=job_id)
            request = read(job / 'request.json')
            require(request['user'] == user and request['plugin'] == plugin, '更新任务身份不匹配')
            proxy = settings(user)
            if request['action'] == 'check':
                manifest = latest(user, plugin, job, proxy, request.get('background', False))
                report(job, 'complete', '已找到新版本' if version(manifest['plugins'][plugin]['version']) > version(identity(user, plugin)[2]['version']) else '当前已是最新版本', 100, running=False)
            else:
                c = candidate(user, plugin)
                require(c and time.time() - c['checkedAt'] <= 86400, '更新清单已过期，请重新检查')
                manifest = verify_manifest(base64.b64decode(c['manifest'], validate=True), base64.b64decode(c['signature'], validate=True), job)
                item = manifest['plugins'][plugin]
                require(version(item['version']) > version(identity(user, plugin)[2]['version']), '当前已是最新版本，拒绝覆盖或降级')
                report(job, 'downloading', '正在通过代理下载安装包' if proxy['enabled'] else '正在下载安装包', 10)
                archive = job / 'package.tar.gz'
                with archive.open('xb') as output:
                    count = download(user, tag_url(manifest['tag'], item['asset']), proxy, item['size'], output, lambda n, total: report(job, 'downloading', '正在下载安装包', 10 + int(40 * n / total)))
                    output.flush()
                    os.fsync(output.fileno())
                require(count == item['size'] and hashlib.sha256(archive.read_bytes()).hexdigest() == item['sha256'], '安装包 SHA-256 校验失败')
                report(job, 'validating', '正在校验安装包内容', 55)
                payload = prepare_bundle(user, plugin, archive, job, proxy, manifest)
                transaction(user, plugin, archive, payload, job)
        except Exception as error:
            journal = read(job / 'journal.json', {})
            # Root-only diagnostics contain stack frames, never request bodies,
            # local variable values, proxy settings or authentication headers.
            diagnostic = getattr(error, 'diagnostic', None) or read(job / 'verification-failure.json', None)
            save(job / 'failure.json', dict(type=type(error).__name__, trace=''.join(traceback.format_exception(type(error), error, error.__traceback__))[-6000:],
                                           verification=diagnostic if isinstance(diagnostic,dict) else None))
            message = str(error) if isinstance(error, ValueError) else '更新失败，请查看升级记录或通过 SSH 检查'
            if isinstance(diagnostic,dict):
                message = '原厂校验失败：'+diagnostic.get('reason','请查看升级记录')+'（退出码 '+str(diagnostic.get('exitCode'))+'）'
            if journal.get('state') == 'rolled_back':
                message += '；已恢复上一版本'
            report(job, 'failed', message[:400], 0, running=False,verification=diagnostic if isinstance(diagnostic,dict) else None)
        finally:
            # Only root-owned updater records are pruned; user caches are never
            # accessed as root. Interrupted transactions remain recoverable.
            try:
                cleanup_plugin(user, plugin, worker_locked=True)
            except (OSError, ValueError):
                pass


def recover_plugin(user, plugin, excluding=None):
    load_manager().recover_pending()


def bootstrap(user, plugin, payload):
    require(os.geteuid() == 0, '需要安装器权限')
    require(plugin in PLUGINS and re.fullmatch(r'u[0-9]+', user or ''), '账号或插件无效')
    source = plain(payload / 'installer/updater')
    incoming = (source / 'updater.py').read_bytes()
    match = re.search(br"^VERSION = '([0-9.]+)'$", incoming, re.M)
    require(match is not None, '升级组件版本缺失')
    protected(BASE, 0o755)
    existing = BASE / 'updater.py'
    install = True
    if existing.exists():
        installed_match = re.search(br"^VERSION = '([0-9.]+)'$", existing.read_bytes(), re.M)
        require(installed_match is not None, '已安装升级组件版本异常')
        comparison = (version(match[1].decode()), version(installed_match[1].decode()))
        install = comparison[0] > comparison[1]
        if comparison[0] == comparison[1]:
            require(all((source / name).read_bytes() == (BASE / name).read_bytes() for name in CORE_FILES), '同版本更新组件内容不一致，拒绝覆盖')
    if install or not existing.exists():
        for name in CORE_FILES:
            atomic(BASE / name, (source / name).read_bytes(), 0o755 if name.endswith('.py') else 0o644)
    protected(BASE / 'users')
    prefix = '/usr/bin/python3 -I -B ' + str(BASE / 'updater.py') + ' api --user ' + user + ' --plugin ' + plugin
    rule = SUDOERS / ('minas-update-' + user + '-' + plugin)
    policy = (user + ' ALL=(root) NOPASSWD: ' + prefix + '\n').encode()
    staged = BASE / '.sudoers-check'
    atomic(staged, policy, 0o440)
    try:
        run(['visudo', '-cf', staged], timeout=15)
        atomic(rule, policy, 0o440)
    finally:
        staged.unlink(missing_ok=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('api', 'worker', 'fetch', 'bootstrap', 'recover', 'cleanup'))
    parser.add_argument('--user')
    parser.add_argument('--plugin', choices=PLUGINS)
    parser.add_argument('--job')
    parser.add_argument('--payload', type=Path)
    args = parser.parse_args()
    if args.action == 'fetch':
        fetch()
        return
    require(os.geteuid() == 0, '需要固定安装入口权限')
    if args.action == 'api':
        request = json.loads(sys.stdin.buffer.read(16385))
        print(json.dumps(api(args.user, args.plugin, request), ensure_ascii=False))
    else:
        require(os.environ.get('SUDO_USER') in (None, 'root'), '更新管理入口不能由普通用户直接执行')
        if args.action == 'bootstrap':
            bootstrap(args.user, args.plugin, args.payload.resolve())
        elif args.action == 'worker':
            worker(args.user, args.plugin, args.job)
        else:
            for user in (BASE / 'users').iterdir() if (BASE / 'users').exists() else []:
                if re.fullmatch('u[0-9]+', user.name):
                    for plugin in PLUGINS:
                        if (user / plugin).is_dir():
                            if args.action == 'recover':
                                recover_plugin(user.name, plugin)
                            try:
                                cleanup_plugin(user.name, plugin)
                            except (OSError, ValueError):
                                # Other plugins still get maintenance when a
                                # removed account or offline pool is not ready.
                                continue


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # API errors are intentionally credential-free and have a stable shape.
        print(json.dumps(dict(ok=False, error=str(error) if isinstance(error, ValueError) else '在线更新内部错误，请通过 SSH 检查'), ensure_ascii=False))
        sys.exit(1)
