"""The installation contract: package ownership is separate from user state."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

VERSION = '2.0.5'
PLUGINS = ('dockermanager', 'mihomo', 'taskcenter', 'nascenter',
           'sshrecovery', 'quickshare', 'downloadcenter', 'mediaeditor')
PRIVILEGED = frozenset(PLUGINS) - {'quickshare', 'downloadcenter'}
SERVERS = frozenset(('mihomo', 'quickshare', 'downloadcenter'))
BASE = Path('/data/minas/plugins/manager')
RECOVERY = Path('/data/minas/plugins/recovery')
UPDATER = Path('/data/minas/plugins/updater')
COMPONENT = Path('/data/minas/privileged')
DATA = Path('/data/minas/plugins/data')
REGISTRY = Path('/data/plugin')
INSTALL_LOCK = REGISTRY / '.local-plugin-installer.lock'
ENV = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/data/docker',
       'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1'}

# Serialized in every signed bundle and checked again by the installer.
OWNERSHIP = {
    'plugin-home': {'path': '/home/{user}/plugin/{plugin}', 'owner': 'user', 'mode': '0700'},
    'plugin-source': {'path': '/nas/{pool}/{user}/plugin/pluginsrc/{plugin}', 'owner': 'user', 'mode': '0700'},
    'plugin-cache': {'path': '/nas/{pool}/{user}/plugin/plugintmp/{plugin}', 'owner': 'user', 'mode': '0700', 'disposable': True},
    'plugin-data': {'path': '/data/minas/plugins/data/{user}/{plugin}', 'owner': 'root', 'mode': '0711'},
    'user-state': {'path': '/data/minas/plugins/data/{user}/{plugin}/{etc,var}', 'owner': 'user', 'mode': '0700'},
    'privileged': {'path': '/data/minas/privileged', 'owner': 'root', 'mode': '0755'},
    'private-module': {'path': '/data/minas/privileged/modules/{plugin}', 'owner': 'root', 'mode': '0755'},
    'ssh-state': {'path': '/data/minas/privileged/modules/sshrecovery/var', 'owner': 'root', 'mode': '0700'},
    'media-state': {'path': '/data/minas/plugins/data/{user}/mediaeditor/protected', 'owner': 'root', 'mode': '0700'},
    'media-lock': {'path': '/data/minas/privileged/modules/mediaeditor/var', 'owner': 'root', 'mode': '0700'},
    'scheduler-state': {'path': '/data/minas/privileged/users/{user}', 'owner': 'root', 'mode': '0700'},
    'manager': {'path': str(BASE), 'owner': 'root', 'mode': '0755'},
    'transactions': {'path': str(BASE / 'transactions'), 'owner': 'root', 'mode': '0700'},
    'recovery': {'path': str(RECOVERY), 'owner': 'root', 'mode': '0700'},
    'updater': {'path': str(UPDATER), 'owner': 'root', 'mode': '0755'},
}


def file_contract(relative, executable):
    """Concrete package member -> owner, mode and installation path template."""
    parts = relative.split('/')
    if parts[0] == 'components':
        name = parts[1]
        roots = {name: OWNERSHIP[name]['path'] for name in ('manager','recovery','updater','privileged')}
        require(name in roots, '未知组件文件归属')
        mode = '0755' if relative.endswith('.py') or name == 'privileged' else '0644'
        return dict(target=str(roots[name]) + '/' + '/'.join(parts[2:]), owner='root', mode=mode)
    if parts[0] == 'plugins':
        plugin = parts[1]
        require(plugin in PLUGINS, '未知插件文件归属')
        child = '/'.join(parts[2:])
        if child == 'INFO' or child.startswith('scripts/'):
            return dict(target='/home/{user}/plugin/' + plugin + '/' + child, owner='user',
                        mode='0755' if child.startswith('scripts/') else '0644')
        if child.startswith('src/'):
            return dict(target='/nas/{pool}/{user}/plugin/pluginsrc/' + plugin + '/' + child[4:],
                        owner='user', mode='0755' if executable else '0644')
        if child.startswith('etc/'):
            return dict(target=str(DATA) + '/{user}/' + plugin + '/' + child, owner='user', mode='0600')
        prefix = 'installer/common/modules/' + plugin + '/'
        if child.startswith(prefix):
            require(plugin in PRIVILEGED, '非提权插件禁止携带私有模块')
            return dict(target=str(COMPONENT) + '/modules/' + plugin + '/' + child[len(prefix):], owner='root', mode='0755')
    return dict(target=None, owner='package', mode='0755' if executable else '0644')


def require(ok, message):
    if not ok:
        raise ValueError(message)


def require_unmounted(path):
    """Reject real mounts before traversing a tree, including file bind mounts.

    Firmware overlay files can have a different st_dev from their parent without
    being mounts. The kernel mount table is authoritative for this namespace.
    """
    path = Path(path)
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
        require(len(fields) >= 10 and ' - ' in line, '内核挂载表格式异常')
        mount = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4]))
        require(mount != path and path not in mount.parents, '目标内有挂载点：' + str(mount))


def version(value):
    require(isinstance(value, str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', value), '无效版本')
    return tuple(map(int, value.split('.')))


def identity(user, plugin):
    require(re.fullmatch(r'u[0-9]+', user or '') and plugin in PLUGINS, '无效账号或插件')


def plain(path):
    """Validate ancestors too; never resolve an unexpected symlink."""
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, '路径必须为绝对路径')
    for part in (path, *path.parents):
        require(not part.is_symlink(), '路径含符号链接：' + str(part))
    return path


def protected(path, mode=0o700):
    path = plain(path)
    if not path.exists():
        if not path.parent.exists():
            protected(path.parent, 0o755)
        path.mkdir(mode=mode)
    for part in (path, *path.parents):
        info = part.stat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                'root 目录权限异常：' + str(part))
    path.chmod(mode)
    return path


def read(path, default=None):
    try:
        with plain(path).open('rb') as stream:
            return json.load(stream)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as error:
        raise ValueError('JSON 文件损坏：' + str(path)) from error


def fsync_dir(path):
    fd = os.open(plain(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path, data, mode=0o600, owner=(0, 0)):
    path = plain(path)
    if isinstance(data, (dict, list)):
        data = (json.dumps(data, ensure_ascii=False, sort_keys=True) + '\n').encode()
    elif isinstance(data, str):
        data = data.encode()
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), *owner)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fsync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def checksum(path):
    digest = hashlib.sha256()
    fd = os.open(plain(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), '文件类型异常')
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


@contextlib.contextmanager
def lock(path, timeout=60):
    """Never truncate, replace or unlink a live lock inode."""
    path = plain(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                '锁文件权限异常')
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                require(time.monotonic() < deadline, '另一个维护操作仍在执行')
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)
