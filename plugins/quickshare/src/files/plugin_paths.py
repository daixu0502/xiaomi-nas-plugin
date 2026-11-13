"""Disposable user runtime in firmware plugintmp; never credentials or code."""
import os
from pathlib import Path
import re
import stat
import tempfile
import shutil

HOME_ROOT=Path('/home')
PERSISTENT_ROOT=Path('/data/minas/plugins/data')

DISPOSABLE = {
    'mihomo': {'mihomo.pid', 'mihomo.log', 'mihomo.log.1', 'update-tasks',
        'update-operation.lock','geodata-update.lock','core-update.lock','network-access.lock',
        'manual-node.lock','rule-provider.lock'},
    'quickshare': {'server.pid', 'server.log', 'server.log.1'},
    'downloadcenter': {'service.pid', 'service.log', 'service.log.1', 'aria2.log', 'aria2.conf',
        'qbittorrent.log', 'qbittorrent.log.1', 'qb-session.json', 'qb-applied.json',
        'tracker-status.json', 'tracker-apply.json', 'job.json', 'core-update.json',
        'core-update.log', 'qb-update.json', 'qb-update.log','control.log','dht.dat','dht6.dat',
        'api.lock','lifecycle.lock','core-update.lock','tracker-update.lock','supervisor.lock'},
}


# BEGIN SHARED STORAGE PATHS
"""Canonical NAS storage pools; embedded in user and privileged runtimes."""
import os
from pathlib import Path
import re

STORAGE_NAS = Path('/nas')
STORAGE_HOMES = Path('/home')
STORAGE_MOUNTINFO = Path('/proc/self/mountinfo')


def mounted_storage_pools():
    pools = set()
    for line in STORAGE_MOUNTINFO.read_text().splitlines():
        fields = line.split()
        if len(fields) < 10 or ' - ' not in line:
            raise ValueError('无法确认存储池挂载状态')
        mount = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4]))
        if (mount.parent == STORAGE_NAS and re.fullmatch(r'pool[0-9]+', mount.name)
                and not mount.is_symlink() and mount.is_dir()):
            pools.add(mount)
    return sorted(pools, key=lambda p: int(p.name[4:]))


def plugin_storage_root(user, plugin, require_mounted=True):
    if not re.fullmatch(r'u[0-9]+', user or '') or not re.fullmatch(r'[a-z]+', plugin or ''):
        raise ValueError('插件或账号无效')
    link = STORAGE_HOMES / user / 'plugin' / plugin / 'src'
    if not link.is_symlink():
        raise ValueError('插件源码链接不可用，无法确认存储池')
    source = link.resolve(strict=False)
    try:
        parts = source.relative_to(STORAGE_NAS).parts
    except ValueError:
        parts = ()
    if (len(parts) != 5 or not re.fullmatch(r'pool[0-9]+', parts[0])
            or parts[1:] != (user, 'plugin', 'pluginsrc', plugin)):
        raise ValueError('插件源码路径与账号不匹配')
    pool = STORAGE_NAS / parts[0]
    if require_mounted and pool not in mounted_storage_pools():
        raise ValueError('存储池未挂载：' + str(pool))
    return pool / user
# END SHARED STORAGE PATHS


def open_state_directory(home, name='var'):
    """Anchor trusted etc/var links, refusing redirection below their targets."""
    home=Path(home)
    if name not in ('etc','var'):raise ValueError('未知插件持久目录')
    path=home/name
    if not home.is_absolute() or '..' in home.parts:raise PermissionError('插件目录必须为明确的绝对路径')
    if home.is_relative_to(HOME_ROOT):
        match=re.fullmatch(r'(u[0-9]+)/plugin/([a-z]+)',str(home.relative_to(HOME_ROOT)))
        if not match:raise PermissionError('插件目录身份无效')
        expected=PERSISTENT_ROOT/match[1]/match[2]/name
        if path.is_symlink():
            if os.readlink(path)!=str(expected):raise PermissionError('插件持久目录链接异常')
            path=expected
    elif path.is_symlink():
        raise PermissionError('非官方状态目录链接')
    flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
    # Root-owned layout parents are 0711: users may traverse but not list them.
    # O_PATH needs only traversal; the final state directory is opened readable.
    path_flags=os.O_PATH|os.O_DIRECTORY|os.O_NOFOLLOW
    fd=os.open(path.anchor,path_flags)
    try:
        for index,part in enumerate(path.parts[1:],1):
            child=os.open(part,flags if index==len(path.parts)-1 else path_flags,dir_fd=fd)
            os.close(fd);fd=child
        info=os.fstat(fd)
        if info.st_uid!=os.geteuid() or info.st_mode&0o077:raise PermissionError('插件持久目录所属用户或权限异常')
        result=fd;fd=-1;return result
    finally:
        if fd!=-1:os.close(fd)


def runtime_directory(home):
    home = Path(home)
    target = home / 'tmp'
    if str(home).startswith('/home/'):
        match = re.fullmatch(r'/home/(u[0-9]+)/plugin/([a-z]+)', str(home))
        if not match or not target.is_symlink():
            raise RuntimeError('官方插件缓存链接不可用')
        resolved = target.resolve(strict=True)
        expected = re.fullmatch(r'/nas/(pool[^/]+)/' + match[1] + r'/plugin/plugintmp/' + match[2], str(resolved))
        if not expected or not os.path.ismount('/nas/' + expected[1]):
            raise RuntimeError('存储池未挂载，禁止创建插件缓存')
    else:
        # Isolated local tests, never a fallback path on a real NAS.
        target.mkdir(mode=0o700, exist_ok=True)
    info = target.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise PermissionError('插件缓存目录所属用户不正确')
    runtime = target / 'runtime'
    if runtime.is_symlink():
        raise PermissionError('运行目录不能是符号链接')
    runtime.mkdir(mode=0o700, exist_ok=True)
    info = runtime.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PermissionError('运行目录权限不正确')
    return runtime





def update_staging(home):
    """Large disposable downloads use official tmp, outside the bounded RAM mount."""
    runtime_directory(home)
    path = Path(home) / 'tmp/update-staging'
    if path.is_symlink():
        raise PermissionError('更新暂存不能为链接')
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PermissionError('更新暂存权限异常')
    return path


def publish(source, target):
    """Keep final replacement atomic even when staging is on another disk."""
    source, target = Path(source), Path(target)
    if source.is_symlink() or target.is_symlink():
        raise PermissionError('拒绝替换符号链接')
    fd, temporary = tempfile.mkstemp(prefix='.' + target.name + '.', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as output, source.open('rb') as stream:
            shutil.copyfileobj(stream, output)
            os.fchmod(output.fileno(), stat.S_IMODE(source.stat().st_mode))
            output.flush(); os.fsync(output.fileno())
        os.replace(temporary, target)
        source.unlink()
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


if __name__ == '__main__':
    import sys
    if sys.argv[1] == '--publish':publish(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == '--update-staging':print(update_staging(sys.argv[2]))
    else:print(runtime_directory(sys.argv[1]))
