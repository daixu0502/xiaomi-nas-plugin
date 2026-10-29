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
VERSION = '1.0.3'
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
    for item in manifest['plugins'].values():
        require(item['asset'] in assets and assets[item['asset']].get('browser_download_url') == tag_url(tag, item['asset']) and assets[item['asset']]['size'] == item['size'], '发布安装包不完整或大小不匹配')
    value = dict(manifest=base64.b64encode(raw).decode(), signature=base64.b64encode(signature).decode(), checkedAt=time.time())
    save(user_dir(user) / 'release-cache.json', value)
    save(plugin_dir(user, plugin) / 'candidate.json', value)
    return manifest


def unpack(archive, destination, plugin, expected_version):
    seen, total = set(), 0
    protected(destination)
    with tarfile.open(archive, mode='r:gz') as package:
        for item in package:
            path = PurePosixPath(item.name)
            require(not path.is_absolute() and '..' not in path.parts and path.parts and path.parts[0] == plugin and str(path) == item.name and item.name not in seen, '安装包包含非法或重复路径')
            seen.add(item.name)
            require(len(seen) <= 4096 and (item.isfile() or item.isdir()) and not item.mode & 0o7000, '安装包含链接、设备或特殊权限')
            total += item.size
            require(0 <= item.size <= MAX_TREE and total <= MAX_TREE, '安装包解压大小超限')
            target = destination.joinpath(*path.parts)
            protected(target.parent)
            if item.isdir():
                protected(target)
            else:
                with package.extractfile(item) as stream:
                    atomic(target, stream.read(), 0o755 if item.mode & 0o111 else 0o644)
    payload = destination / plugin
    info = read(payload / 'INFO')
    require(info.get('plugin') == plugin and info.get('version') == expected_version and info.get('type') == 'standard' and info.get('system') is False, '安装包 INFO 不匹配')
    for name in ('src/ui/config', 'src/ui/' + plugin + '.cgi', 'scripts/control', 'installer/recovery/recovery.py', 'installer/updater/updater.py'):
        require((payload / name).is_file(), '安装包缺少标准文件')
    require(not any(part in ('etc', 'var', 'tmp', 'local', 'tests', '__tests__') for path in (payload / 'src').rglob('*') for part in path.relative_to(payload / 'src').parts), '源码包混入运行数据或测试')
    return payload


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
USER_STAGE = r'''
import os,sys,tarfile,tempfile,shutil
from pathlib import Path,PurePosixPath
home,src,plugin,token=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3],sys.argv[4]
def plain(p):
 for n in (p,*p.parents):
  if n.is_symlink():raise ValueError('redirected path')
plain(home);plain(src);plain(src.parent)
stage=src.parent/('.'+plugin+'.update-'+token)
plain(stage)
if not stage.is_dir() or stage.stat().st_uid!=os.getuid():raise ValueError('staging owner mismatch')
control=None
try:
 with tarfile.open(fileobj=sys.stdin.buffer,mode='r|gz') as tf:
  for item in tf:
   path=PurePosixPath(item.name)
   if item.name==plugin+'/scripts/control':control=tf.extractfile(item).read();continue
   if len(path.parts)<3 or path.parts[:2]!=(plugin,'src'):continue
   if not item.isfile() or '..' in path.parts:raise ValueError('invalid source entry')
   out=stage.joinpath(*path.parts[2:]);out.parent.mkdir(parents=True,exist_ok=True)
   with out.open('xb') as f:f.write(tf.extractfile(item).read())
   out.chmod(0o755 if item.mode&0o111 else 0o644)
 for name in ('mihomo','aria2c','qbittorrent-nox'):
  old=src/'files'/name;out=stage/'files'/name
  if old.is_file() and not out.exists():
   plain(old);shutil.copy2(old,out)
 if control is None:raise ValueError('control missing')
 plain(home/'scripts');(home/'scripts').mkdir(mode=0o700,exist_ok=True);target=home/'scripts/control'
 fd,temp=tempfile.mkstemp(prefix='.control-',dir=target.parent)
 with os.fdopen(fd,'wb') as f:f.write(control);f.flush();os.fsync(f.fileno());os.fchmod(f.fileno(),0o755)
 os.replace(temp,target)
 (stage/'INFO').symlink_to(home/'INFO')
except BaseException:
 raise
'''


def source_parent(src, user):
    account = pwd.getpwnam(user)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(src.anchor, flags)
    try:
        for part in src.parent.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        metadata = os.fstat(fd)
        require(metadata.st_uid in (0, account.pw_uid) and not metadata.st_mode & 0o002, '源码父目录权限异常')
        return fd
    except BaseException:
        os.close(fd)
        raise


def stage_as_user(user, plugin, src, archive, token):
    require(re.fullmatch('[0-9a-f]{32}', token), '暂存标识无效')
    account = pwd.getpwnam(user)
    parent = source_parent(src, user)
    stage = '.' + plugin + '.update-' + token
    previous = '.' + plugin + '.previous-' + token
    try:
        # Firmware deliberately keeps pluginsrc root-owned and non-writable by
        # users. Prepare metadata via a pinned directory FD, then drop UID for
        # every source-content write. Never loosen the shared parent's mode.
        os.mkdir(stage, 0o700, dir_fd=parent)
        fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            os.fchown(fd, account.pw_uid, account.pw_gid)
            original = os.fstat(fd)
            with open(archive, 'rb') as stream:
                run(['runuser', '-u', user, '--', '/usr/bin/python3', '-I', '-B', '-c', USER_STAGE, HOMES / user / 'plugin' / plugin, src, plugin, token], stdin=stream, timeout=180)
            current = os.stat(stage, dir_fd=parent, follow_symlinks=False)
            require((original.st_dev, original.st_ino) == (current.st_dev, current.st_ino), '暂存目录已变化')
            try:
                os.stat(previous, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError('回退目录已被占用')
            try:
                current_source = os.stat(src.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                current_source = None
            if current_source is not None:
                require(stat.S_ISDIR(current_source.st_mode), '源码目录类型异常')
                os.rename(src.name, previous, src_dir_fd=parent, dst_dir_fd=parent)
            os.rename(stage, src.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def clean_source_stage(user, plugin, src, tokens):
    code = '''import re,shutil,sys\nfrom pathlib import Path\nparent=Path(sys.argv[1]);plugin=sys.argv[2]\nfor token in sys.argv[3:]:\n if not re.fullmatch('[0-9a-f]{32}',token):raise ValueError('invalid token')\n for kind in ('previous','update'):\n  p=parent/('.'+plugin+'.'+kind+'-'+token)\n  if p.is_symlink():raise ValueError('redirected cleanup path')\n  if p.is_dir():\n   for child in p.iterdir():\n    if child.is_symlink() or not child.is_dir():child.unlink()\n    else:shutil.rmtree(child)\n'''
    run(['runuser', '-u', user, '--', '/usr/bin/python3', '-I', '-B', '-c', code, src.parent, plugin, *tokens], timeout=120)
    parent = source_parent(src, user)
    try:
        for token in tokens:
            require(re.fullmatch('[0-9a-f]{32}', token), '清理标识无效')
            for kind in ('previous', 'update'):
                try:
                    os.rmdir('.' + plugin + '.' + kind + '-' + token, dir_fd=parent)
                except FileNotFoundError:
                    pass
        os.fsync(parent)
    finally:
        os.close(parent)


def idle(user, plugin, home):
    # Read-only probes run with the plugin UID, including access to its caches.
    if plugin == 'downloadcenter':
        environment = dict(ENV, REQUEST_METHOD='POST', CONTENT_TYPE='application/json', CONTENT_LENGTH='2', QUERY_STRING='action=status')
        result = subprocess.run(['runuser', '-u', user, '--', str(home / 'src/ui/downloadcenter.cgi')], input=b'{}', env=environment, capture_output=True, timeout=45)
        try:
            data = json.loads(re.split(br'\r?\n\r?\n', result.stdout, maxsplit=1)[1])
            require(data.get('ok') and not data.get('error'), '无法确认下载服务空闲，请稍后重试')
            require(not any(t.get('status') in ('active', 'waiting', 'downloading', 'stalledDL', 'metaDL', 'forcedDL', 'queuedDL', 'checkingDL', 'checkingResumeData', 'allocating') for t in data.get('tasks', [])), '有下载任务正在运行，请暂停任务后再更新')
        except (IndexError, json.JSONDecodeError):
            raise ValueError('无法确认下载服务空闲，请稍后重试') from None
    if plugin == 'quickshare':
        raw = run(['runuser', '-u', user, '--', '/usr/bin/python3', '-I', '-B', '-c', 'from pathlib import Path;print((Path.home()/"plugin/quickshare/var/server.port").read_text().strip())'])
        port = int(raw.strip())
        for table in (Path('/proc/net/tcp'), Path('/proc/net/tcp6')):
            for line in table.read_text().splitlines()[1:] if table.exists() else []:
                fields = line.split()
                require(not (int(fields[1].split(':')[1], 16) == port and fields[3] == '01'), '文件快传有活动连接，请传输完成后再更新')
    admin = PRIVILEGED / 'component_admin.py'
    if plugin not in ('quickshare', 'downloadcenter') and admin.is_file():
        run(['/usr/bin/python3', '-I', '-B', admin, 'check', '--plugin', plugin, '--user', user], timeout=30)


def backup_file(job, path, backups):
    path = plain(path)
    metadata = path.stat() if path.exists() else None
    item = dict(path=str(path), exists=metadata is not None)
    if metadata:
        require(stat.S_ISREG(metadata.st_mode), '升级元数据类型异常')
        name = str(len(backups))
        atomic(job / 'backup' / name, path.read_bytes())
        item.update(file=name, mode=stat.S_IMODE(metadata.st_mode), uid=metadata.st_uid, gid=metadata.st_gid)
    backups.append(item)


def rollback(job, journal):
    r = load_recovery()
    user, plugin = journal['user'], journal['plugin']
    home, owner = r.identity(user, plugin)
    plain(home)
    src = r.source_path(home, user, plugin)
    if journal.get('wasRunning'):
        run(['systemctl', 'stop', journal['unit']], timeout=100)
    archive = job / 'rollback.tar.gz'
    rollback_token = uuid.uuid4().hex
    if journal.get('sourceTouched'):
        stage_as_user(user, plugin, src, archive, rollback_token)
    for item in reversed(journal['files']):
        path = plain(item['path'])
        if item['exists']:
            r.atomic(path, (job / 'backup' / item['file']).read_bytes(), item['mode'], (item['uid'], item['gid']))
        else:
            path.unlink(missing_ok=True)
    with r.user_lock(user):
        registry = REGISTRY_DIR / (user + '.list')
        data = r.read(registry)
        data[plugin] = journal['entry']
        r.save(registry, data)
    r.atomic(RECOVERY / 'records' / (user + '-' + plugin) / 'current', journal['generation'].encode())
    if journal.get('wasRunning'):
        run(['systemctl', 'start', journal['unit']], timeout=100)
    r.verify(user, plugin, home, src)
    journal['state'] = 'rolled_back'
    save(job / 'journal.json', journal)
    clean_source_stage(user, plugin, src, (job.name, rollback_token))


def transaction(user, plugin, archive, payload, job):
    r = load_recovery()
    home, account, before = identity(user, plugin)
    record = RECOVERY / 'records' / (user + '-' + plugin)
    # Lock order matches the installer/recovery service. Downloads happen first.
    with lock(INSTALL_LOCK, 60), r.record_lock(record):
        require(platform.machine() in ('aarch64', 'arm64'), '当前设备不是 ARM64 NAS')
        src = r.source_path(home, user, plugin)
        r.verify(user, plugin, home, src)
        require(before['version'] == read(home / 'INFO')['version'], '插件版本已变化，请重新检查更新')
        idle(user, plugin, home)
        report(job, 'backup', '正在保存源码和注册信息备份', 65)
        generation = (record / 'current').read_text().strip()
        saved = r.load_generation(record)
        require(r.digest(src) == read(saved / 'INFO')['abstract'], '当前源码与恢复快照不一致，请先修复')
        protected(job / 'backup')
        with tarfile.open(job / 'rollback.tar.gz', 'w:gz') as tf:
            for directory in ('src', 'scripts'):
                for path in sorted((saved / directory).rglob('*')):
                    if path.is_file() and not path.is_symlink():
                        tf.add(path, arcname=plugin + '/' + path.relative_to(saved).as_posix(), recursive=False)
        journal = dict(state='prepared', user=user, plugin=plugin, generation=generation, files=[], entry=r.read(REGISTRY_DIR / (user + '.list'))[plugin], sourceTouched=False, wasRunning=False)
        unit = 'minas-plugin-' + plugin + '-' + user + '.service'
        if plugin in r.SERVERS:
            journal['unit'] = unit
            journal['wasRunning'] = subprocess.run(['systemctl', 'is-active', '--quiet', unit], env=ENV).returncode == 0
        paths = {home / 'INFO', REGISTRY_DIR / 'www/icon' / (plugin + '.icon'), RECOVERY / 'recovery.py'}
        paths.add(SYSTEM_UNITS / 'minas-plugin-update-recover.service')
        paths.update(BASE / name for name in CORE_FILES)
        paths.add(SUDOERS / ('minas-update-' + user + '-' + plugin))
        common = payload / 'installer/common'
        if common.is_dir():
            for name in read(common / 'manifest.json')['files']:
                require(not PurePosixPath(name).is_absolute() and '..' not in PurePosixPath(name).parts, '权限组件文件路径无效')
                paths.add(PRIVILEGED / name)
            paths.update((PRIVILEGED / 'registry.json', SUDOERS / (plugin + '-' + user), Path('/etc/cron.d/minas-privileged-v2')))
        for path in sorted(paths):
            backup_file(job, path, journal['files'])
        save(job / 'journal.json', journal)
        try:
            if journal['wasRunning']:
                report(job, 'stopping', '正在暂停插件服务，配置和任务保持保留', 70)
                run(['systemctl', 'stop', unit], timeout=100)
            journal['sourceTouched'] = True
            save(job / 'journal.json', journal)
            report(job, 'installing', '正在安装已验证的插件源码', 75)
            stage_as_user(user, plugin, src, archive, job.name)
            if common.is_dir():
                run(['/usr/bin/python3', '-I', '-B', common / 'component_admin.py', 'install', '--plugin', plugin, '--user', user, '--payload', payload, '--plugin-version', read(payload / 'INFO')['version']], timeout=120)
            report(job, 'verifying', '正在更新注册信息并执行原厂校验', 85)
            spec = importlib.util.spec_from_file_location('minas_new_recovery', payload / 'installer/recovery/recovery.py')
            new_recovery = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(new_recovery)
            require(version(new_recovery.VERSION) >= version(r.VERSION), '拒绝降低恢复组件版本')
            new_recovery.snapshot(user, plugin, payload, retain_previous=True)
            if plugin in r.SERVERS:
                # The service was deliberately stopped for replacement, not by
                # the user. Preserve its pre-update intent in the new snapshot
                # before releasing the record lock to the recovery watcher.
                saved = new_recovery.load_generation(record)
                intent = new_recovery.read(saved / 'record.json')
                intent['running'] = journal['wasRunning']
                new_recovery.save(saved / 'record.json', intent)
            if journal['wasRunning']:
                run(['systemctl', 'start', unit], timeout=100)
                run(['systemctl', 'is-active', '--quiet', unit], timeout=15)
            r.verify(user, plugin, home, src)
            info = read(home / 'INFO')
            require(info['version'] == read(payload / 'INFO')['version'] and r.digest(src) == info['abstract'], '安装后版本或摘要不匹配')
            journal['state'] = 'committed'
            save(job / 'journal.json', journal)
            report(job, 'complete', '更新完成，请关闭插件窗口后重新打开', 100, installedVersion=info['version'], running=False)
        except BaseException:
            report(job, 'rollback', '更新失败，正在恢复上一版本', 90)
            rollback(job, journal)
            raise
        # Cleanup is outside the transaction: a cleanup failure must never undo
        # an already committed update. Keep the protected rollback generation.
        try:
            clean_source_stage(user, plugin, src, (job.name,))
        except Exception:
            pass


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
    job = {}
    if re.fullmatch('[0-9a-f]{32}', current.get('id', '')):
        job = read(directory / 'jobs' / current['id'] / 'status.json', {})
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
    with lock(directory / 'queue.lock', False):
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
            raise
        return status(user, plugin)


def worker(user, plugin, job_id):
    require(re.fullmatch('[0-9a-f]{32}', job_id or ''), '任务编号无效')
    identity(user, plugin)
    directory = plugin_dir(user, plugin)
    job = protected(directory / 'jobs' / job_id)
    with lock(directory / 'worker.lock', False):
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
                payload = unpack(archive, job / 'package', plugin, item['version'])
                transaction(user, plugin, archive, payload, job)
        except Exception as error:
            journal = read(job / 'journal.json', {})
            # Root-only diagnostics contain stack frames, never request bodies,
            # local variable values, proxy settings or authentication headers.
            save(job / 'failure.json', dict(type=type(error).__name__, trace=''.join(traceback.format_exception(type(error), error, error.__traceback__))[-6000:]))
            message = str(error) if isinstance(error, ValueError) else '更新失败，请查看升级记录或通过 SSH 检查'
            if journal.get('state') == 'rolled_back':
                message += '；已恢复上一版本'
            report(job, 'failed', message[:400], 0, running=False)
        finally:
            # Only root-owned updater records are pruned; user caches are never
            # accessed as root. Interrupted transactions remain recoverable.
            try:
                jobs = sorted((directory / 'jobs').iterdir(), key=lambda p:p.stat().st_mtime, reverse=True)
                for old in jobs[4:]:
                    if old.name != job_id and re.fullmatch('[0-9a-f]{32}', old.name) and read(old / 'journal.json', {}).get('state') != 'prepared' and not read(old / 'status.json', {}).get('running'):
                        plain(old)
                        shutil.rmtree(old)
            except OSError:
                pass


def recover_plugin(user, plugin, excluding=None):
    directory = plugin_dir(user, plugin)
    jobs = directory / 'jobs'
    for job in sorted(jobs.iterdir()) if jobs.exists() else []:
        if job.name == excluding:
            continue
        journal = read(job / 'journal.json', {})
        if journal.get('state') == 'prepared':
            unit = 'minas-update-' + job.name + '.service'
            require(subprocess.run(['systemctl', 'is-active', '--quiet', unit], env=ENV).returncode != 0, '旧更新任务仍在执行')
            r = load_recovery()
            with lock(INSTALL_LOCK, 60), r.record_lock(RECOVERY / 'records' / (user + '-' + plugin)):
                rollback(job, journal)
                report(job, 'failed', '中断的更新已恢复至上一版本，请重新检查更新', 0, running=False)


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
    unit = ('[Unit]\nDescription=Recover interrupted Xiaomi NAS plugin updates\nAfter=local-fs.target\nBefore=minas-plugin-recovery-v2-watch.service\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 -I -B ' + str(BASE / 'updater.py') + ' recover\nUMask=0077\n[Install]\nWantedBy=multi-user.target\n').encode()
    target = SYSTEM_UNITS / 'minas-plugin-update-recover.service'
    if not target.exists() or target.read_bytes() != unit:
        atomic(target, unit, 0o644)
        run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', 'minas-plugin-update-recover.service'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('api', 'worker', 'fetch', 'bootstrap', 'recover'))
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
                            recover_plugin(user.name, plugin)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # API errors are intentionally credential-free and have a stable shape.
        print(json.dumps(dict(ok=False, error=str(error) if isinstance(error, ValueError) else '在线更新内部错误，请通过 SSH 检查'), ensure_ascii=False))
        sys.exit(1)
