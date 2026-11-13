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
