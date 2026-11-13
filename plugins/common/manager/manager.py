#!/usr/bin/env python3
"""One root transaction coordinator for install, update, removal and recovery."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import types
import uuid

import adapters
import layout as L
import operations
import restoration
from packages import Bundle, COMPONENT_FILES, code_version

VERSION = L.VERSION
MAINTENANCE = L.BASE / 'maintenance.json'
LEGACY_GUARD = Path('/run/minas-plugin-maintenance')
UNITS = Path('/etc/systemd/system')
SUDOERS = Path('/etc/sudoers.d')
# /etc is overlayfs. Mutating its mounted upperdir separately invalidates the
# live dentry (ESTALE) and can make both installation and rollback impossible.
PORT_CONFIGS = (Path('/etc/config/plugin'),)
WATCH = 'minas-plugin-recovery-v2-watch.service'
PROGRESS = None


def failure_summary(error):
    if isinstance(error, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
        command = error.cmd
        program = Path(str(command[0])).name if isinstance(command, (list, tuple)) else '子进程'
        if program == 'systemctl':
            program += ' ' + ' '.join(str(x) for x in command[1:])
        return program + (' 执行超时' if isinstance(error, subprocess.TimeoutExpired)
                          else ' 退出码 ' + str(error.returncode))
    return str(error)


def run(args, check=True, **kwargs):
    return subprocess.run([str(x) for x in args], env=L.ENV, check=check,
                          capture_output=True, timeout=kwargs.pop('timeout', 120), **kwargs)


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def recovery():
    return module(L.RECOVERY / 'recovery.py', 'minas_manager_recovery')


def running(user, plugin, r=None):
    if plugin in L.SERVERS:
        return run(['systemctl', 'is-active', '--quiet', 'minas-plugin-' + plugin + '-' + user + '.service'], False).returncode == 0
    if plugin == 'taskcenter':
        return L.read(L.COMPONENT / 'users' / user / 'state.json', {}).get('scheduler', False)
    return False


def inventory():
    """Strictly read-only, including on an uninitialized NAS."""
    rows = []
    for path in sorted(L.REGISTRY.glob('u*.list')):
        user = path.stem
        if not re.fullmatch(r'u[0-9]+', user):
            continue
        data = L.read(path)
        L.require(isinstance(data, dict), '原厂注册表损坏：' + str(path))
        pwd.getpwnam(user)
        for plugin in L.PLUGINS:
            home = Path('/home') / user / 'plugin' / plugin
            try:
                info = L.read(home / 'INFO', {})
                if not isinstance(info, dict):
                    info = {}
            except (ValueError, OSError):
                info = {}  # Damaged metadata must not hide an uninstall target.
            entry = data.get(plugin, {})
            record = L.RECOVERY / 'records' / (user + '-' + plugin)
            managed = (record / 'current').is_file() and not (record / 'removed').exists()
            owned_source = any((pool / user / 'plugin/pluginsrc' / plugin).exists()
                               for pool in Path('/nas').glob('pool[0-9]*') if os.path.ismount(pool))
            installed = bool(entry.get('install') or os.path.lexists(home) or managed
                             or os.path.lexists(L.DATA / user / plugin) or owned_source)
            rows.append(dict(user=user, plugin=plugin, installed=installed,
                             version=info.get('version'), enabled=entry.get('enable'),
                             running=running(user, plugin) if installed else False))
    return rows


def uninstall_target(user, plugin, pool=None):
    """Identify owned names, never follow a damaged plugin's links to data."""
    L.identity(user, plugin)
    account = pwd.getpwnam(user)
    L.require(account.pw_uid != 0, '插件必须属于普通 NAS 用户')
    home = Path('/home') / user / 'plugin' / plugin
    L.plain(home.parent)
    entries = L.read(L.REGISTRY / (user + '.list'))
    L.require(isinstance(entries, dict), '账号原厂注册表损坏，不能覆盖其他插件登记')
    entry = entries.get(plugin, {})
    notes, pools = [], set()
    if pool:
        L.require(re.fullmatch(r'pool[0-9]+', pool), '存储池名称无效')
        pools.add(pool)
    for candidate in Path('/nas').glob('pool[0-9]*'):
        if re.fullmatch(r'pool[0-9]+', candidate.name) and os.path.ismount(candidate) and (candidate / user).is_dir():
            pools.add(candidate.name)
    if not home.is_symlink():
        for name in ('src', 'tmp', 'etc', 'var'):
            path = home / name
            if path.is_symlink():
                destination = os.readlink(path)
                match = re.fullmatch(r'/nas/(pool[0-9]+)/' + user + r'/plugin/(?:pluginsrc|plugintmp)/' + plugin, destination)
                if match:
                    pools.add(match[1])
                elif name in ('src', 'tmp') or destination != str(L.DATA / user / plugin / name):
                    notes.append('仅移除异常链接，不删除外部目标：' + str(path) + ' -> ' + destination)
    else:
        notes.append('仅移除异常入口链接，不删除外部目标：' + str(home) + ' -> ' + os.readlink(home))
    directory = L.RECOVERY / 'records' / (user + '-' + plugin)
    try:
        generation = (directory / 'current').read_text().strip()
        if re.fullmatch(r'generation-[0-9]+', generation):
            saved = L.read(directory / generation / 'record.json', {})
            match = re.fullmatch(r'/nas/(pool[0-9]+)/' + user + '/plugin/pluginsrc/' + plugin, saved.get('src', ''))
            if match:
                pools.add(match[1])
    except (OSError, ValueError, AttributeError):
        pass
    locations = []
    for name in sorted(pools):
        parent = Path('/nas') / name / user / 'plugin'
        source, cache = parent / 'pluginsrc' / plugin, parent / 'plugintmp' / plugin
        for path in (source.parent, cache.parent):
            try:
                L.plain(path)
            except ValueError:
                notes.append('保留非规范父路径中的外部文件：' + str(path))
                break
        else:
            locations.append(dict(pool=name, src=str(source), tmp=str(cache)))
    first = locations[0] if locations else dict(src=str(home / 'src'), tmp=str(home / 'tmp'))
    config = L.read(Path('/etc/config/plugin'))
    webroot = L.plain(Path(config.get('settings', {}).get('nginx_plugin', '/data/plugin/www')))
    required = [home / 'INFO', home / 'scripts/control', home / 'src/ui' / (plugin + '.cgi'),
                L.DATA / user / plugin / 'etc', L.DATA / user / plugin / 'var']
    required += [L.DATA / user / plugin / name for name in adapters.INITIAL_FILES.get(plugin, ())]
    incomplete = bool(notes) or home.is_symlink() or any(not p.exists() for p in required)
    if not incomplete:
        try:
            incomplete = not isinstance(L.read(home / 'INFO'), dict)
        except (OSError, ValueError):
            incomplete = True
    if not incomplete and 'generation' in locals() and re.fullmatch(r'generation-[0-9]+', generation):
        saved_src = directory / generation / 'src'
        incomplete = any(not (Path(first['src']) / path.relative_to(saved_src)).is_file()
                         for path in saved_src.rglob('*') if path.is_file() and not path.is_symlink())
    return dict(user=user, plugin=plugin, home=str(home), src=first['src'], tmp=first['tmp'],
                web=str(webroot / user / plugin), uid=account.pw_uid, gid=account.pw_gid,
                enabled=entry.get('enable', False), entry=entry or None, desired=running(user, plugin),
                wasRunning=running(user, plugin), fresh=False, pool=None, locations=locations,
                uninstall=True, incomplete=incomplete, notes=notes)


def target(user, plugin, pool=None):
    L.identity(user, plugin)
    account = pwd.getpwnam(user)
    L.require(account.pw_uid != 0, '插件必须属于普通 NAS 用户')
    home = L.plain(Path('/home') / user / 'plugin' / plugin)
    L.require(home.parent.parent.is_dir(), '账号主目录不存在')
    entries = L.read(L.REGISTRY / (user + '.list'))
    L.require(isinstance(entries, dict), '账号注册表不存在或损坏')
    src_link = home / 'src'
    record_dir = L.RECOVERY / 'records' / (user + '-' + plugin)
    saved = None
    if (record_dir / 'current').is_file() and not (record_dir / 'removed').exists():
        generation = (record_dir / 'current').read_text().strip()
        L.require(re.fullmatch(r'generation-[0-9]+', generation), '恢复代次无效')
        saved = L.read(record_dir / generation / 'record.json')
        if not src_link.is_symlink():
            match = re.fullmatch(r'/nas/(pool[0-9]+)/' + user + '/plugin/pluginsrc/' + plugin, saved['src'])
            L.require(match, '恢复记录源码路径异常')
            L.require(pool is None or pool == match[1], '恢复记录存储池与指定值不一致')
            pool = match[1]
    if src_link.is_symlink():
        src = Path(os.readlink(src_link))
        match = re.fullmatch(r'/nas/(pool[0-9]+)/' + user + '/plugin/pluginsrc/' + plugin, str(src))
        L.require(match, '源码链接位置不符合约定')
        L.require(pool is None or pool == match[1], '指定存储池与已有源码位置不一致')
        pool = match[1]
    else:
        L.require(not src_link.exists(), '插件 src 必须是规范链接')
        choices = [p.name for p in Path('/nas').glob('pool[0-9]*')
                   if re.fullmatch(r'pool[0-9]+', p.name) and os.path.ismount(p) and (p / user).is_dir()]
        if pool is None:
            L.require(len(choices) == 1, '无法唯一确定账号存储池，请使用 --pool 明确指定')
            pool = choices[0]
        L.require(pool in choices, '指定存储池未挂载或不属于该账号')
        src = Path('/nas') / pool / user / 'plugin/pluginsrc' / plugin
    L.require(os.path.ismount('/nas/' + pool), '存储池尚未挂载：' + pool)
    L.plain(src)
    tmp = src.parent.parent / 'plugintmp' / plugin
    for name, expected in (('tmp', tmp), ('etc', L.DATA / user / plugin / 'etc'), ('var', L.DATA / user / plugin / 'var')):
        path = home / name
        if path.is_symlink():
            L.require(os.readlink(path) == str(expected), '非规范持久或缓存链接：' + str(path))
        else:
            L.require(not path.exists(), '已有本地目录不自动迁移：' + str(path))
        L.plain(expected)
    config = L.read(Path('/etc/config/plugin'))
    webroot = Path(config.get('settings', {}).get('nginx_plugin', '/data/plugin/www'))
    L.plain(webroot)
    L.require(webroot.is_absolute(), '网页根目录无效')
    home_present = home.exists()
    entry = entries.get(plugin)
    intent = None
    if saved:
        record = saved
        intent = record.get('running')
        explicit = L.read(record_dir / 'service-intent.json', {})
        if explicit.get('time', 0) >= record.get('runtime_intent_time', 0):
            intent = explicit.get('running', intent)
    if plugin == 'taskcenter' and (L.COMPONENT / 'users' / user / 'state.json').is_file():
        intent = running(user, plugin)
    fresh = not home_present and not entry and not saved
    if not fresh:
        for name in ('etc', 'var'):
            path = L.DATA / user / plugin / name
            L.require(path.is_dir(), '持久数据缺失，不创建空状态替代：' + str(path))
            L.require(path.stat().st_uid == account.pw_uid and stat.S_IMODE(path.stat().st_mode) == 0o700,
                      '用户持久数据权限异常：' + str(path))
    if entry is None and saved:
        entry = saved['entry']
    return dict(user=user, plugin=plugin, home=str(home), src=str(src), tmp=str(tmp),
                web=str(webroot / user / plugin), uid=account.pw_uid, gid=account.pw_gid,
                enabled=entry.get('enable', True) if entry else True, entry=entry,
                desired=bool(intent if intent is not None else True if fresh else running(user, plugin)),
                wasRunning=running(user, plugin) if not fresh else False, fresh=fresh, pool=pool)


def preflight(action, pairs, bundle=None, pool=None):
    L.require(os.geteuid() == 0, 'NAS 操作需要 root')
    L.require(platform.machine() in ('aarch64', 'arm64'), '仅支持 ARM64 小米 NAS')
    L.require(sys.version_info >= (3, 9), '需要 Python 3.9 或更新版本')
    for name in ('systemctl', 'plugincenter', 'runuser', 'openssl', 'sudo', 'visudo', 'jq', 'ss'):
        L.require(shutil.which(name, path=L.ENV['PATH']), 'NAS 缺少命令：' + name)
    L.require(Path('/usr/bin/plugin.sh').is_file(), '缺少原厂源码校验器')
    L.require(Path('/etc/config/plugin').stat().st_nlink > 0,
              '原厂配置 /etc/config/plugin 的挂载句柄已失效；请重启 NAS 后重试，不要直接修改 /data/etc/upper')
    for path, mode in ((Path('/data/minas'), 0o755), (L.BASE, 0o755), (L.RECOVERY, 0o700),
                       (L.UPDATER, 0o755), (L.COMPONENT, 0o755)):
        L.plain(path)
        if path.exists():
            info = path.stat()
            L.require(path.is_dir() and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == mode,
                      '当前布局权限不符合约定：' + str(path))
    rows = [(uninstall_target if action == 'uninstall' else target)(user, plugin, pool) for user, plugin in pairs]
    L.require(rows, '没有选择插件或账号')
    all_old = {(r['user'], r['plugin']) for r in inventory() if r['installed']}
    native = L.read(L.BASE / 'installed.json', {}).get('format') == 2
    if all_old and not native:
        L.require(action in ('reinstall', 'uninstall', 'status'),
                  '检测到旧安装，请先使用 reinstall 完成整机切换')
        if action == 'reinstall':
            L.require(set(pairs) == all_old, '首次切换必须选择所有账号已有插件，不增加原本没有的插件')
    for row in rows:
        plugin, home = row['plugin'], Path(row['home'])
        if not row['fresh'] and action != 'uninstall':
            installed = L.read(home / 'INFO', {})
            L.require(installed.get('abstract') and Path(row['src']).is_dir(), '已有源码缺失，请先执行 repair：' + row['src'])
            L.require(adapters.source_digest(row['user'], row['src']) == installed['abstract'],
                      '已有源码与 INFO 冲突，保留现场：' + row['src'])
            row['sourceVerified'] = True
        if action in ('update', 'uninstall'):
            L.require(not row['fresh'], '目标尚未安装：' + plugin)
        if bundle:
            adapters.preflight_dependencies(plugin)
            preserve_cores = bundle.manifest.get('corePolicy') == 'preserve-installed'
            L.require(not preserve_cores or action in ('update', 'reinstall') and not row['fresh'],
                      '在线包仅用于保留内核升级；全新安装请使用完整离线套件')
            incoming = bundle.manifest['plugins'][plugin]['version']
            old = L.read(home / 'INFO', {}).get('version')
            L.require(not old or L.version(incoming) >= L.version(old), '不允许降级插件：' + plugin)
            for name in adapters.DEPENDENCIES.get(plugin, ()):
                L.require(shutil.which(name, path=L.ENV['PATH']), '缺少插件依赖：' + name)
            for binary in {'mihomo': ('mihomo',), 'downloadcenter': ('aria2c', 'qbittorrent-nox')}.get(plugin, ()):
                path = Path(row['src']) / 'files' / binary if preserve_cores else bundle.payload(plugin) / 'src/files' / binary
                L.plain(path)
                L.require(path.is_file(), '现有内核缺失，请使用完整离线套件修复：' + binary)
                with path.open('rb') as stream:
                    header = stream.read(20)
                L.require(header[:6] == b'\x7fELF\x02\x01' and int.from_bytes(header[18:20], 'little') == 183,
                          '缺少有效 ARM64 离线内核：' + binary)
            row['preservedCoreBytes'] = sum((Path(row['src']) / 'files' / name).stat().st_size for name in
                {'mihomo': ('mihomo',), 'downloadcenter': ('aria2c', 'qbittorrent-nox')}.get(plugin, ())) if preserve_cores else 0
        for path in ((home, L.DATA / row['user'] / plugin) if action == 'uninstall'
                     else (home, Path(row['src']), L.DATA / row['user'] / plugin)):
            L.plain(path.parent if action == 'uninstall' else path)
        if bundle:
            size = sum(v['size'] for k, v in bundle.manifest['files'].items()
                       if k.startswith('plugins/' + plugin + '/')) + row.get('preservedCoreBytes', 0)
            for path in (Path('/data'), Path('/nas') / row['pool']):
                L.require(shutil.disk_usage(path).free >= size * 3 + 64 * 1024 * 1024,
                          '安装及回退空间不足：' + str(path))
    if bundle:
        required_data = 64 * 1024 * 1024
        pool_space = {}
        for row in rows:
            size = sum(v['size'] for key, v in bundle.manifest['files'].items()
                       if key.startswith('plugins/' + row['plugin'] + '/')) + row.get('preservedCoreBytes', 0)
            required_data += size * 6
            pool_space[row['pool']] = pool_space.get(row['pool'], 64 * 1024 * 1024) + size * 2
        L.require(shutil.disk_usage('/data').free >= required_data, '整批事务的 /data 暂存及回退空间不足')
        for name, size in pool_space.items():
            L.require(shutil.disk_usage('/nas/' + name).free >= size, '整批事务的存储池空间不足：' + name)
        registry = L.read(L.COMPONENT / 'registry.json', {})
        L.require(not registry or registry.get('apiVersion') == 1, '公共权限 API 不兼容')
        for row in rows:
            old = registry.get('modules', {}).get(row['plugin'])
            L.require(not old or L.version(old) <= L.version(bundle.manifest['plugins'][row['plugin']]['version']),
                      '其他账号引用更新的权限模块，拒绝降级')
        for name, destination, source in (
            ('privileged', L.COMPONENT / 'common/privileged_runtime.py', 'common/privileged_runtime.py'),
            ('recovery', L.RECOVERY / 'recovery.py', 'recovery.py'),
            ('updater', L.UPDATER / 'updater.py', 'updater.py'),
            ('manager', L.BASE / 'layout.py', 'layout.py')):
            if name == 'privileged' and not any(r['plugin'] in L.PRIVILEGED for r in rows):
                continue
            incoming = bundle.manifest['components'][name]['version']
            if destination.exists():
                old = code_version(destination)
                if L.version(old) > L.version(incoming):
                    L.require(name == 'privileged' or L.version(old)[0] == L.version(incoming)[0],
                              '已安装组件 API 主版本不兼容：' + name)
                if incoming == old:
                    root = destination.parent.parent if name == 'privileged' else destination.parent
                    for relative in COMPONENT_FILES[name]:
                        path = root / relative
                        L.require(path.is_file() and L.checksum(path) == L.checksum(bundle.component(name) / relative),
                                  '同版本组件内容不一致：' + name + '/' + relative)
    return rows, bool(all_old and not native and action == 'reinstall')


def copy_tree(source, destination):
    """Copy link objects without asking the NAS filesystem for link xattrs.

    Its xattr implementation may dereference even with follow_symlinks=False,
    making shutil.copytree fail on the factory's dangling INFO link.
    """
    source, destination = Path(source), Path(destination)
    destination.mkdir(mode=0o700)
    for child in source.iterdir():
        out = destination / child.name
        mode = child.lstat().st_mode
        if stat.S_ISLNK(mode):
            out.symlink_to(os.readlink(child))
        elif stat.S_ISDIR(mode):
            copy_tree(child, out)
        else:
            L.require(stat.S_ISREG(mode), '目录含特殊文件：' + str(child))
            shutil.copy2(child, out, follow_symlinks=False)
    shutil.copystat(source, destination, follow_symlinks=False)


class Journal:
    """Write-ahead metadata; no caches or mutable task/database snapshots."""
    def __init__(self, directory, state=None, phase='prepared'):
        self.directory = directory
        self.state = state if state is not None else dict(format=2, phase=phase, entries=[], registry=[], targets=[])
        if state is None:
            L.protected(directory)
            L.protected(directory / 'backup')
            self.save()

    def save(self):
        L.atomic(self.directory / 'journal.json', self.state)

    def checkpoint(self, step, row=None):
        self.state['step'] = step
        if row:
            self.state['currentTarget'] = [row['user'], row['plugin']]
        self.save()
        if PROGRESS:
            PROGRESS(step, row)

    def capture(self, path):
        path = Path(path)
        if any(e['path'] == str(path) for e in self.state['entries']):
            return
        L.plain(path.parent)
        exists = os.path.lexists(path)
        item = dict(path=str(path), exists=exists)
        if exists:
            L.require_unmounted(path)
            metadata = path.lstat()
            item.update(uid=metadata.st_uid, gid=metadata.st_gid, mode=stat.S_IMODE(metadata.st_mode))
            if path.is_symlink():
                item.update(kind='link', link=os.readlink(path))
            elif path.is_dir():
                item.update(kind='tree', backup=str(len(self.state['entries'])), owners={})
                for child in [path, *path.rglob('*')]:
                    meta = child.lstat()
                    L.require(stat.S_ISDIR(meta.st_mode) or stat.S_ISREG(meta.st_mode) or stat.S_ISLNK(meta.st_mode),
                              '目录含特殊文件：' + str(child))
                    item['owners'][str(child.relative_to(path))] = [meta.st_uid, meta.st_gid, stat.S_IMODE(meta.st_mode)]
                copy_tree(path, self.directory / 'backup' / item['backup'])
                saved = self.directory / 'backup' / item['backup']
                for copied in saved.rglob('*'):
                    if copied.is_file() and not copied.is_symlink():
                        with copied.open('rb') as stream:
                            os.fsync(stream.fileno())
                for directory in [*sorted((p for p in saved.rglob('*') if p.is_dir() and not p.is_symlink()),
                                          key=lambda p: len(p.parts), reverse=True), saved]:
                    L.fsync_dir(directory)
            else:
                L.require(stat.S_ISREG(metadata.st_mode), '事务文件类型异常')
                item.update(kind='file', backup=str(len(self.state['entries'])))
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, 'rb') as stream:
                    L.atomic(self.directory / 'backup' / item['backup'], stream.read())
        self.state['entries'].append(item)
        self.save()

    def registration(self, user, plugin):
        if any(e['user'] == user and e['plugin'] == plugin for e in self.state['registry']):
            return
        with L.lock(L.REGISTRY / ('.' + user + '.plugins.lock')):
            entry = L.read(L.REGISTRY / (user + '.list')).get(plugin)
            self.state['registry'].append(dict(user=user, plugin=plugin, value=entry))
            self.save()

    def metadata(self, path):
        path = L.plain(path)
        if not path.exists() or any(e['path'] == str(path) for e in self.state['entries']):
            return
        info = path.stat()
        L.require(path.is_dir(), '预期为目录：' + str(path))
        self.state['entries'].append(dict(path=str(path), exists=True, kind='metadata',
                                         uid=info.st_uid, gid=info.st_gid, mode=stat.S_IMODE(info.st_mode)))
        self.save()

    def ports(self, user, plugin):
        for path in PORT_CONFIGS:
            if not path.is_file():
                continue
            values = L.read(path).get('ports', {}).get(plugin, {})
            self.state.setdefault('ports', []).append(dict(path=str(path), user=user, plugin=plugin,
                                                           value=values.get(user)))
        self.save()

    def restore(self):
        for item in reversed(self.state['entries']):
            path = Path(item['path'])
            L.plain(path.parent)
            if not item['exists']:
                if root_state(path) and path.is_dir() and not path.is_symlink() and any(p.name == 'lock' or p.name.endswith('.lock') for p in path.rglob('*')):
                    purge_root_state(path)
                else:
                    remove_path(path)
            elif item['kind'] == 'metadata':
                L.plain(path)
                os.chown(path, item['uid'], item['gid'])
                path.chmod(item['mode'])
            elif item['kind'] == 'link':
                remove_path(path)
                path.symlink_to(item['link'])
                os.lchown(path, item['uid'], item['gid'])
            elif item['kind'] == 'file':
                if path.is_symlink():
                    path.unlink()
                L.atomic(path, (self.directory / 'backup' / item['backup']).read_bytes(),
                         item['mode'], (item['uid'], item['gid']))
            else:
                temporary = path.parent / ('.' + path.name + '.restore-' + self.directory.name)
                remove_path(temporary)
                copy_tree(self.directory / 'backup' / item['backup'], temporary)
                for relative, (uid, gid, mode) in item['owners'].items():
                    out = temporary / relative
                    os.chown(out, uid, gid, follow_symlinks=False)
                    if not out.is_symlink():
                        out.chmod(mode)
                if root_state(path) and path.is_dir() and not path.is_symlink() and any(p.name == 'lock' or p.name.endswith('.lock') for p in path.rglob('*')):
                    restore_preserving_locks(temporary, path)
                    remove_path(temporary)
                else:
                    remove_path(path)
                    os.replace(temporary, path)
                L.fsync_dir(path.parent)
        for item in self.state['registry']:
            merge_registration(item['user'], item['plugin'], item['value'])
        restored_ports = set()
        for item in self.state.get('ports', []):
            # Accept journals from <= 2.0.5, but never replay their direct
            # upperdir writes. The mounted path is the single source of truth.
            path = Path('/etc/config/plugin') if item['path'] == '/data/etc/upper/config/plugin' else Path(item['path'])
            key = (str(path), item['user'], item['plugin'])
            if key in restored_ports:
                continue
            restored_ports.add(key)
            data = L.read(path)
            previous = json.dumps(data, sort_keys=True)
            values = data.setdefault('ports', {}).setdefault(item['plugin'], {})
            if item['value'] is None:
                values.pop(item['user'], None)
                if not values:
                    data['ports'].pop(item['plugin'], None)
            else:
                values[item['user']] = item['value']
            if json.dumps(data, sort_keys=True) != previous:
                metadata = path.stat()
                L.atomic(path, data, stat.S_IMODE(metadata.st_mode), (metadata.st_uid, metadata.st_gid))


def root_state(path):
    return any(path == root or root in path.parents for root in (L.COMPONENT, L.RECOVERY, L.UPDATER))


def remove_path(path):
    L.plain(path.parent)
    L.require_unmounted(path)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        L.require(path.is_dir(), '拒绝删除特殊路径：' + str(path))
        shutil.rmtree(path)


def purge_root_state(path, keep=()):
    """Delete owned state/code, retaining only fixed lock inodes and ancestors."""
    L.plain(path)
    if not path.exists():
        return
    for child in path.iterdir():
        if child in keep or child.name == 'lock' or child.name.endswith('.lock'):
            L.require(child.is_file() and not child.is_symlink(), '锁文件类型异常：' + str(child))
        elif child.is_dir() and not child.is_symlink():
            purge_root_state(child, keep)
        else:
            remove_path(child)
    if not any(path.iterdir()):
        path.rmdir()


def restore_preserving_locks(saved, target):
    """Restore root records in place; waiting service callers keep their inode."""
    meta = saved.stat()
    os.chown(target, meta.st_uid, meta.st_gid)
    target.chmod(stat.S_IMODE(meta.st_mode))
    for child in list(target.iterdir()):
        if child.name == 'lock' or child.name.endswith('.lock'):
            continue
        previous = saved / child.name
        if child.is_dir() and not child.is_symlink():
            if previous.is_dir():
                restore_preserving_locks(previous, child)
            else:
                purge_root_state(child)
        elif not previous.exists() and not previous.is_symlink():
            remove_path(child)
    for source in saved.iterdir():
        if source.name == 'lock' or source.name.endswith('.lock'):
            continue
        out = target / source.name
        if source.is_symlink():
            remove_path(out);out.symlink_to(os.readlink(source))
        elif source.is_dir():
            if not out.exists():
                copy_tree(source, out)
            else:
                restore_preserving_locks(source, out)
        else:
            metadata = source.stat()
            L.atomic(out, source.read_bytes(), stat.S_IMODE(metadata.st_mode), (metadata.st_uid, metadata.st_gid))


def merge_registration(user, plugin, entry):
    path = L.REGISTRY / (user + '.list')
    with L.lock(L.REGISTRY / ('.' + user + '.plugins.lock')):
        data = L.read(path)
        L.require(isinstance(data, dict), '拒绝覆盖损坏注册表')
        if entry is None:
            data.pop(plugin, None)
        else:
            data[plugin] = entry
        info = path.stat()
        L.atomic(path, data, stat.S_IMODE(info.st_mode), (info.st_uid, info.st_gid))


def ensure_user_dir(path, owner):
    L.plain(path)
    if not path.exists():
        path.mkdir(mode=0o700)
        os.chown(path, *owner)
    L.require(path.is_dir() and path.stat().st_uid == owner[0], '插件目录属主异常：' + str(path))
    path.chmod(0o700)


def prepare_layout(row, r):
    user, plugin = row['user'], row['plugin']
    home, src, tmp = (Path(row[k]) for k in ('home', 'src', 'tmp'))
    owner = row['uid'], row['gid']
    r.ensure_data_layout()
    for path in (home.parent, src.parent.parent, src.parent, tmp.parent, Path(row['web']).parent):
        L.plain(path)
        if not path.exists():
            path.mkdir(mode=0o755)
            path.chmod(0o755)  # CLI runs with umask 077; shared parents must remain traversable.
        L.require(path.is_dir(), '插件父路径不是目录')
        accessible = run(['runuser', '-u', user, '--', '/usr/bin/test', '-x', path], False).returncode == 0
        L.require(accessible,
                  '账号无法访问插件父目录：' + str(path))
    ensure_user_dir(home, owner)
    ensure_user_dir(home / 'scripts', owner)
    r.persist_state(user, plugin, home, None, owner, row['enabled'])
    r.prepare_tmp(user, src, tmp, owner)
    for path, destination in ((home / 'src', src), (home / 'tmp', tmp), (Path(row['web']), home / 'src/ui')):
        r.linked(path, destination, owner)


def watched_files(row, payload=None):
    user, plugin = row['user'], row['plugin']
    home = Path(row['home'])
    local = [home] if home.is_symlink() else [home / 'INFO', home / 'scripts', home / 'src', home / 'tmp', home / 'etc', home / 'var']
    sources = ([Path(loc['src']) for loc in row['locations'] if os.path.ismount('/nas/' + loc['pool'])]
               if row.get('uninstall') else [Path(row['src'])])
    paths = [*local, *sources, Path(row['web']), L.REGISTRY / 'www/icon' / (plugin + '.icon'),
             L.RECOVERY / 'records' / (user + '-' + plugin),
             SUDOERS / ('minas-update-' + user + '-' + plugin),
             SUDOERS / ('minas-service-' + user + '-' + plugin),
             SUDOERS / (plugin + '-' + user), Path('/etc/cron.d') / (plugin + '-' + user),
             Path('/etc/minas-plugin-services') / (user + '-' + plugin)]
    if plugin in L.SERVERS:
        paths.append(UNITS / ('minas-plugin-' + plugin + '-' + user + '.service'))
    if plugin == 'mihomo':
        paths.append(Path('/etc/cron.d') / ('mihomo-plugin-' + user))
    if plugin in L.PRIVILEGED:
        paths.append(L.COMPONENT / 'registry.json')
        paths.extend(L.COMPONENT / name for name in ('privileged_runtime.py', 'user_state_io.py'))
        if payload:
            manifest = L.read(payload / 'installer/common/manifest.json')
            paths.extend(L.COMPONENT / name for name in manifest['files'])
    for name in (() if row.get('uninstall') else adapters.INITIAL_FILES.get(plugin, ())):
        paths.append(L.DATA / user / plugin / name)
    return paths


def watched_platform():
    for name in (WATCH, 'minas-plugin-recovery-v2.service', 'minas-plugin-recovery-v2.timer',
                 'minas-plugin-update-recover.service', 'minas-maintenance-events.service'):
        yield UNITS / name
        for parent in UNITS.glob('*.wants'):
            yield parent / name
        yield UNITS / 'multi-user.target.wants' / name
    for name in ('minas-plugin-recovery-v2', 'minas-privileged-v2'):
        yield Path('/etc/cron.d') / name


def capture_missing_parents(journal, path):
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for parent in reversed(missing):
        journal.capture(parent)


def set_scheduler(user, enabled, component_locked=False):
    path = L.COMPONENT / 'users' / user / 'state.json'
    if not path.exists():
        return
    # Use the component's API for normal operations. During manager maintenance
    # it is blocked to outsiders, so mutate only the scheduler flag under its lock.
    with contextlib.nullcontext() if component_locked else L.lock(L.COMPONENT / 'component.lock'):
        with L.lock(path.parent / 'lock'):
            state = L.read(path)
            state['scheduler'] = enabled
            L.atomic(path, state)


def install_boot_recovery(journal):
    """Publish a recoverable worker before changing any plugin or service."""
    frozen = L.protected(journal.directory / 'engine')
    for name in COMPONENT_FILES['manager']:
        L.atomic(frozen / name, (Path(__file__).parent / name).read_bytes(),
                 0o755 if name.endswith('.py') else 0o644)
    bootstrap = L.BASE / 'bootstrap.py'
    if not bootstrap.is_file() or bootstrap.read_bytes() != (Path(__file__).parent / 'bootstrap.py').read_bytes():
        L.atomic(bootstrap, (Path(__file__).parent / 'bootstrap.py').read_bytes(), 0o755)
    unit = ('[Unit]\nDescription=Recover interrupted NAS plugin transactions\nAfter=local-fs.target\n'
            'Before=' + WATCH + ' minas-maintenance-events.service\n[Service]\nType=oneshot\n'
            'ExecStart=/usr/bin/python3 -B ' + str(L.BASE / 'bootstrap.py') + '\n'
            'TimeoutStartSec=infinity\nUMask=0077\n[Install]\nWantedBy=multi-user.target\n')
    unit_path = UNITS / 'minas-plugin-manager-recover.service'
    if not unit_path.is_file() or unit_path.read_text() != unit:
        L.atomic(unit_path, unit, 0o644)
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'enable', unit_path.name])
    journal.state['phase'] = 'prepared'
    journal.save()


def install_code(bundle, journal, r):
    for name, destination in (('manager', L.BASE), ('recovery', L.RECOVERY), ('updater', L.UPDATER)):
        L.protected(destination, 0o700 if name == 'recovery' else 0o755)
        version_file = destination / ('layout.py' if name == 'manager' else name + '.py')
        if version_file.is_file() and L.version(code_version(version_file)) > L.version(bundle.manifest['components'][name]['version']):
            continue
        for relative in COMPONENT_FILES[name]:
            path = destination / relative
            journal.capture(path)
            L.atomic(path, (bundle.component(name) / relative).read_bytes(), 0o755 if relative.endswith('.py') else 0o644)
    for unit in (WATCH, 'minas-plugin-recovery-v2.service', 'minas-plugin-update-recover.service',
                 'minas-maintenance-events.service', 'minas-plugin-manager-recover.service'):
        journal.capture(UNITS / unit)
    for path in (Path('/etc/cron.d/minas-plugin-recovery-v2'), Path('/etc/cron.d/minas-privileged-v2')):
        journal.capture(path)


def install_privileged_gate(bundle):
    if 'privileged' not in bundle.manifest['components']:
        return
    destination = L.COMPONENT / 'common/privileged_runtime.py'
    incoming = bundle.manifest['components']['privileged']['version']
    if destination.exists() and L.version(code_version(destination)) >= L.version(incoming):
        return
    for relative in COMPONENT_FILES['privileged']:
        path = L.COMPONENT / relative
        L.protected(path.parent, 0o755)
        L.atomic(path, (bundle.component('privileged') / relative).read_bytes(), 0o755)


def archive_payload(payload, output, previous=None):
    cores = {'mihomo': ('mihomo',), 'downloadcenter': ('aria2c', 'qbittorrent-nox')}.get(payload.name, ())
    written = set()
    with tarfile.open(output, 'w:gz') as stream:
        for directory in ('src', 'scripts'):
            for path in sorted((payload / directory).rglob('*')):
                if path.is_file():
                    relative = path.relative_to(payload)
                    prior = previous / relative.relative_to('src') if previous and relative.parts[0] == 'src' else None
                    preserve = relative.as_posix() in {'src/files/' + name for name in cores} and prior and prior.is_file() and not prior.is_symlink()
                    stream.add(prior if preserve else path, arcname=payload.name + '/' + relative.as_posix(), recursive=False)
                    written.add(relative.as_posix())
        for binary in cores:
            relative = 'src/files/' + binary
            if relative not in written:
                prior = previous / 'files' / binary if previous else None
                L.require(prior is not None and prior.is_file() and not prior.is_symlink(),
                          '在线升级必须保留已校验内核：' + binary)
                stream.add(prior, arcname=payload.name + '/' + relative, recursive=False)


def verify_layout(row):
    user, plugin = row['user'], row['plugin']
    home = Path(row['home'])
    state = L.DATA / user / plugin
    directories = [(Path('/data/minas'), 0, 0o755), (L.BASE, 0, 0o755), (L.RECOVERY, 0, 0o700),
                   (L.UPDATER, 0, 0o755), (L.DATA, 0, 0o711), (state.parent, 0, 0o711), (state, 0, 0o711),
                   (home, row['uid'], 0o700), (home / 'scripts', row['uid'], 0o700),
                   (Path(row['src']), row['uid'], 0o700), (state / 'etc', row['uid'], 0o700),
                   (state / 'var', row['uid'], 0o700)]
    if plugin in L.PRIVILEGED:
        directories += [(L.COMPONENT, 0, 0o755), (L.COMPONENT / 'modules' / plugin, 0, 0o755)]
    if plugin == 'sshrecovery':
        directories.append((L.COMPONENT / 'modules/sshrecovery/var', 0, 0o700))
    if plugin == 'mediaeditor' and (state / 'protected').exists():
        directories.append((state / 'protected', 0, 0o700))
    for path, uid, mode in directories:
        L.plain(path)
        info = path.stat()
        L.require(path.is_dir() and info.st_uid == uid and stat.S_IMODE(info.st_mode) == mode,
                  '安装后目录权限不符合归属清单：' + str(path))
    for path, target_path in ((home / 'src', row['src']), (home / 'tmp', row['tmp']),
                              (home / 'etc', state / 'etc'), (home / 'var', state / 'var'),
                              (Path(row['web']), home / 'src/ui'), (Path(row['src']) / 'INFO', home / 'INFO')):
        L.require(path.is_symlink() and os.readlink(path) == str(target_path), '安装后链接目标不符合约定：' + str(path))
    registry = L.read(L.COMPONENT / 'registry.json', {})
    granted = plugin in registry.get('users', {}).get(user, [])
    L.require(granted == (plugin in L.PRIVILEGED), '安装后账号授权不符合组件边界')


def install_one(row, payload, journal, r):
    user, plugin = row['user'], row['plugin']
    home, src = Path(row['home']), Path(row['src'])
    journal.checkpoint('layout', row)
    prepare_layout(row, r)
    # Services have stopped, so listener occupancy now describes other instances.
    used = set()
    for table in (Path('/proc/net/tcp'), Path('/proc/net/tcp6'), Path('/proc/net/udp'), Path('/proc/net/udp6')):
        if table.exists():
            used.update(int(line.split()[1].split(':')[1], 16) for line in table.read_text().splitlines()[1:]
                        if table.name.startswith('udp') or line.split()[3] == '0A')
    port = adapters.initialize(user, plugin, payload, used)
    archive = journal.directory / (user + '-' + plugin + '.tar.gz')
    source_backup = next((e for e in journal.state['entries'] if e['path'] == row['src'] and e.get('kind') == 'tree'), None)
    previous = journal.directory / 'backup' / source_backup['backup'] if source_backup else None
    archive_payload(payload, archive, previous)
    token = uuid.uuid4().hex
    journal.state.setdefault('stages', []).append(dict(user=user, plugin=plugin, src=str(src), token=token))
    journal.save()
    journal.checkpoint('source', row)
    operations.stage_as_user(user, plugin, src, archive, token)
    info = L.read(payload / 'INFO')
    info.update(port=port, abstract=r.digest(src), timestamp=int(time.time()))
    L.atomic(home / 'INFO', info, 0o644, (row['uid'], row['gid']))
    # Deploy other lifecycle files as data, never execute them as root.
    for path in (payload / 'scripts').iterdir():
        L.atomic(home / 'scripts' / path.name, path.read_bytes(), 0o755, (row['uid'], row['gid']))
    if plugin in L.PRIVILEGED:
        journal.checkpoint('grant', row)
        run(['/usr/bin/python3', '-B', payload / 'installer/common/component_admin.py',
             'install', '--plugin', plugin, '--user', user, '--payload', payload,
             '--plugin-version', info['version'], '--manager-transaction', journal.directory.name])
    journal.checkpoint('registration', row)
    initial = dict(row['entry'] or {}, enable=row['enabled'], status='running' if row['enabled'] else 'disable')
    merge_registration(user, plugin, initial)
    r.normalize(user, plugin, payload)
    r.port_registry(user, plugin, port)
    if plugin in L.SERVERS:
        r.install_daemon(user, plugin)
    updater = module(L.UPDATER / 'updater.py', 'minas_manager_updater')
    updater.bootstrap(user, plugin, payload)
    journal.checkpoint('verification', row)
    r.verify(user, plugin, home, src)
    journal.checkpoint('snapshot', row)
    r.snapshot(user, plugin, payload, retain_previous=True, running_intent=row['desired'])
    verify_layout(row)
    if plugin in L.SERVERS and row['desired']:
        journal.checkpoint('start', row)
        run(['systemctl', 'start', r.daemon_unit(user, plugin)])
        run(['systemctl', 'is-active', '--quiet', r.daemon_unit(user, plugin)])
    if plugin == 'taskcenter':
        set_scheduler(user, False)  # restore its intent only after the batch commits
    journal.checkpoint('health', row)
    adapters.health(user, plugin, home)
    r.verify(user, plugin, home, src)
    current = L.read(L.REGISTRY / (user + '.list'))[plugin]
    current.update(enable=row['enabled'], status='running' if row['enabled'] else 'disable')
    merge_registration(user, plugin, current)


def remove_one(row, journal, r, preserve=False):
    user, plugin = row['user'], row['plugin']
    if plugin == 'mihomo' and not preserve:
        remove_mihomo_proxy(row, journal)
    if plugin in L.PRIVILEGED and (L.COMPONENT / 'registry.json').exists():
        # Legacy remove deletes persistent state. Revoke atomically here and
        # defer all owned-data removal until commit, for both old and new layouts.
        with L.lock(L.COMPONENT / 'component.lock'):
            data = L.read(L.COMPONENT / 'registry.json')
            grants = data.get('users', {}).get(user, [])
            if plugin in grants:
                grants.remove(plugin)
            if not grants:
                data.get('users', {}).pop(user, None)
            if not any(plugin in values for values in data.get('users', {}).values()):
                data.get('modules', {}).pop(plugin, None)
                data.get('packages', {}).pop(plugin, None)
            if plugin == 'mihomo' and not preserve:
                data.get('settings', {}).get(user, {}).pop('mihomo', None)
            L.atomic(L.COMPONENT / 'registry.json', data)
            (SUDOERS / (plugin + '-' + user)).unlink(missing_ok=True)
            grants = {p for values in data.get('users', {}).values() for p in values}
            cron = Path('/etc/cron.d/minas-privileged-v2')
            if grants:
                text = 'SHELL=/bin/sh\nPATH=/usr/sbin:/usr/bin:/sbin:/bin:/data/docker\nMAILTO=""\n'
                if 'dockermanager' in grants:
                    text += '@reboot root ' + str(L.COMPONENT / 'minas-helper') + ' boot >/dev/null 2>&1\n'
                L.atomic(cron, text, 0o644)
            else:
                cron.unlink(missing_ok=True)
            if not grants & {'taskcenter', 'sshrecovery'}:
                unit = 'minas-maintenance-events.service'
                run(['systemctl', 'disable', '--now', unit], False)
                (UNITS / unit).unlink(missing_ok=True)
                run(['systemctl', 'daemon-reload'])
    forget_plugin(row, r)
    merge_registration(user, plugin, None)
    remove_path(Path(row['web']))
    home = Path(row['home'])
    if home.is_symlink():
        remove_path(home)
    else:
        for name in ('src', 'tmp', 'etc', 'var', 'scripts', 'INFO'):
            remove_path(home / name)
    if row.get('uninstall'):
        for location in row['locations']:
            if os.path.ismount('/nas/' + location['pool']):
                remove_path(Path(location['src']))
    else:
        remove_path(Path(row['src']))
    if not preserve:
        # Permanent deletion is deliberately after durable commit. Until then
        # the original state and rollback materials remain available.
        journal.state.setdefault('purge', []).append(row)
        journal.save()


def docker_container_intents():
    output = run(['/data/docker/docker', 'ps', '-a', '--no-trunc', '--format', '{{.ID}} {{.State}}']).stdout.decode()
    result = {}
    for line in output.splitlines():
        fields = line.split()
        L.require(len(fields) == 2 and re.fullmatch('[0-9a-f]{64}', fields[0]), 'Docker 容器状态无效')
        ident, state = fields
        L.require(state in ('running', 'exited', 'created'), '容器处于特殊状态，暂不能重启 Docker：' + ident)
        result[ident] = state == 'running'
    return result


def restore_docker_containers(intents):
    for ident, desired in intents.items():
        L.require(re.fullmatch('[0-9a-f]{64}', ident), 'Docker 回退容器 ID 无效')
        actual = run(['/data/docker/docker', 'inspect', '--format', '{{.State.Running}}', ident]).stdout.decode().strip()
        L.require(actual in ('true', 'false'), '无法确认原有容器状态：' + ident)
        if (actual == 'true') != desired:
            run(['/data/docker/docker', 'start' if desired else 'stop', ident])


def remove_mihomo_proxy(row, journal):
    """Remove only this account's managed drop-in, preserving independent Docker."""
    dropin = UNITS / 'docker.service.d/90-mihomo-proxy.conf'
    if not dropin.exists():
        return
    L.plain(dropin)
    with L.lock(L.COMPONENT / 'component.lock'), L.lock(L.COMPONENT / 'docker.lock'):
        lock = L.COMPONENT / 'modules/mihomo/proxy.lock'
        with L.lock(lock) if lock.parent.is_dir() else contextlib.nullcontext():
            data = L.read(L.COMPONENT / 'registry.json', {})
            port = data.get('settings', {}).get(row['user'], {}).get('mihomo', {}).get('proxyPort')
            L.require(type(port) is int and 1024 <= port <= 65535, '无法确认 Mihomo Docker 代理归属')
            lines = dropin.read_text().splitlines()
            url = 'http://127.0.0.1:' + str(port)
            if not all('Environment="' + key + '=' + url + '"' in lines for key in ('HTTP_PROXY','HTTPS_PROXY')):
                return  # Another account or independently managed configuration.
            journal.capture(dropin)
            journal.state['dockerWasRunning'] = run(['systemctl','is-active','--quiet','docker.service'], False).returncode == 0
            if journal.state['dockerWasRunning']:
                journal.state['dockerContainerIntents'] = docker_container_intents()
            journal.checkpoint('docker-proxy', row)
            dropin.unlink()
            L.fsync_dir(dropin.parent)
            run(['systemctl','daemon-reload'])
            if journal.state['dockerWasRunning']:
                run(['systemctl','restart','docker.service'])
                run(['systemctl','is-active','--quiet','docker.service'])
                restore_docker_containers(journal.state['dockerContainerIntents'])


def forget_plugin(row, r):
    """Remove only target metadata; never invoke an old installer/removal hook."""
    user, plugin = row['user'], row['plugin']
    if row.get('uninstall'):
        if plugin in L.SERVERS:
            stop_for_uninstall(user, plugin)
        for location in row['locations']:
            if plugin in L.SERVERS | {'taskcenter', 'nascenter'} and os.path.ismount('/nas/' + location['pool']):
                cache = Path(location['tmp'])
                if not cache.is_symlink():
                    operations.release_cache(user, plugin, cache)
    elif plugin in ('taskcenter', 'nascenter'):
        cache_io = module(L.COMPONENT / 'common/user_state_io.py', 'minas_manager_cache_io')
        cache_io.release_memory_cache(user, plugin)
    if plugin == 'mihomo':
        # The old fallback enables the daemon regardless of its saved intent.
        (Path('/etc/cron.d') / ('mihomo-plugin-' + user)).unlink(missing_ok=True)
    for path in (SUDOERS / ('minas-update-' + user + '-' + plugin),
                 SUDOERS / ('minas-service-' + user + '-' + plugin),
                 Path('/etc/minas-plugin-services') / (user + '-' + plugin),
                 Path('/etc/cron.d') / (plugin + '-' + user)):
        path.unlink(missing_ok=True)
    record = L.RECOVERY / 'records' / (user + '-' + plugin)
    if record.is_dir():
        L.atomic(record / 'removed', 'explicit unified uninstall\n')
    if plugin in L.SERVERS:
        unit = UNITS / ('minas-plugin-' + plugin + '-' + user + '.service')
        if not row.get('uninstall') and (unit.is_file() or run(['systemctl', 'is-active', '--quiet', unit.name], False).returncode == 0):
            run(['systemctl', 'stop', unit.name])
        # Firmware can remove the service file while a RAM cache mount remains.
        # Release through the UID-validated descriptor before unlinking tmp.
        if not row.get('uninstall') and r.memory_download(Path(row['home'])):
            r.download_runtime(user, 'release', plugin)
        if unit.is_file():
            unit.unlink()
            run(['systemctl', 'daemon-reload'])
    for path in PORT_CONFIGS:
        if not path.is_file():
            continue
        data = L.read(path)
        ports = data.get('ports', {})
        values = ports.get(plugin, {})
        if user in values:
            values.pop(user)
            if not values:
                ports.pop(plugin, None)
            info = path.stat()
            L.atomic(path, data, stat.S_IMODE(info.st_mode), (info.st_uid, info.st_gid))


def stop_for_uninstall(user, plugin):
    unit = 'minas-plugin-' + plugin + '-' + user + '.service'
    # ExecStop may be missing/broken. systemd still stops its cgroup; judge the
    # actual state after stop, not the removed control script's exit status.
    run(['systemctl', 'stop', unit], False)
    L.require(run(['systemctl', 'is-active', '--quiet', unit], False).returncode != 0,
              '插件服务尚未停止：' + unit)


def purge(row, r):
    user, plugin = row['user'], row['plugin']
    if plugin in L.SERVERS:
        remove_path(UNITS / ('minas-plugin-' + plugin + '-' + user + '.service.d'))
        run(['systemctl', 'daemon-reload'])
    locations = row.get('locations', [dict(tmp=row['tmp'], src=row['src'], pool=row.get('pool'))])
    for location in locations:
        if location.get('pool') and not os.path.ismount('/nas/' + location['pool']):
            continue
        tmp = Path(location['tmp'])
        if tmp.is_symlink():
            remove_path(tmp)
        elif tmp.exists():
            if row.get('uninstall') and plugin in L.SERVERS | {'taskcenter','nascenter'}:
                operations.release_cache(user, plugin, tmp)
            L.require_unmounted(tmp)
            code = ('import os,shutil;from pathlib import Path;p=Path(' + repr(str(tmp)) + ');'
                    'assert not any(n.is_symlink() for n in (p,*p.parents));'
                    'shutil.rmtree(p)')
            adapters.run_user(user, code)
        if row.get('uninstall'):
            remove_path(Path(location['src']))
    for path in (Path(row['home']), L.DATA / user / plugin,
                 Path('/home') / user / 'plugin/.reserve' / plugin,
                 Path('/data/minas/backups/plugins') / plugin / user):
        remove_path(path)
    record = L.RECOVERY / 'records' / (user + '-' + plugin)
    for path in (record, L.UPDATER / 'users' / user / plugin):
        purge_root_state(path)
    if record.exists():
        L.atomic(record / 'removed', 'explicit uninstall\n')
    if plugin == 'taskcenter':
        purge_root_state(L.COMPONENT / 'users' / user)
    if plugin == 'sshrecovery':
        state = L.COMPONENT / 'modules/sshrecovery/var'
        for suffix in ('.pub', '.settings.json', '.emergency.json'):
            remove_path(state / 'keys' / (user + suffix))
        remove_path(state / 'tmp' / ('generated-' + user))
    data = L.read(L.COMPONENT / 'registry.json', {})
    if plugin in L.PRIVILEGED and not any(plugin in grants for grants in data.get('users', {}).values()):
        # Shared resource locks retain their inode. Delete plugin-owned data,
        # but not independent /data/minas/ssh or the component's common locks.
        directory = L.COMPONENT / 'modules' / plugin
        purge_root_state(directory)
    if not any(x['installed'] and x['plugin'] == plugin for x in inventory()):
        remove_path(L.REGISTRY / 'www/icon' / (plugin + '.icon'))
    if not any(x['installed'] and x['user'] == user for x in inventory()):
        purge_root_state(L.UPDATER / 'users' / user)


def rollback(journal, r):
    journal.checkpoint('rollback')
    for row in journal.state['targets']:
        if row['plugin'] in L.SERVERS:
            run(['systemctl', 'stop', r.daemon_unit(row['user'], row['plugin'])], False)
    with L.lock(L.COMPONENT / 'component.lock') if L.COMPONENT.exists() else contextlib.nullcontext():
        journal.restore()
    run(['systemctl', 'daemon-reload'])
    for row in journal.state['targets']:
        if row.get('sourceVerified'):
            verifier = r if hasattr(r, 'verify') else recovery()
            verifier.verify(row['user'], row['plugin'], Path(row['home']), Path(row['src']))
    for row in journal.state['targets']:
        if row['plugin'] == 'taskcenter':
            set_scheduler(row['user'], row['desired'])
        elif row['plugin'] in L.SERVERS and row['wasRunning'] and not (row.get('uninstall') and row.get('incomplete')):
            run(['systemctl', 'start', r.daemon_unit(row['user'], row['plugin'])])
    if journal.state.get('dockerWasRunning'):
        with L.lock(L.COMPONENT / 'docker.lock'):
            run(['systemctl', 'restart', 'docker.service'])
            run(['systemctl', 'is-active', '--quiet', 'docker.service'])
            restore_docker_containers(journal.state.get('dockerContainerIntents', {}))
    journal.state['phase'] = 'rolled-back'
    journal.save()


def finish(journal, r):
    journal.checkpoint('cleanup')
    if journal.state['phase'] == 'committed':
        for row in journal.state.get('purge', []):
            purge(row, r)
        pending = sorted({loc['pool'] for row in journal.state.get('purge', [])
                          for loc in row.get('locations', []) if not os.path.ismount('/nas/' + loc['pool'])})
        journal.state['pendingPools'] = pending
        journal.save()
        for row in journal.state['targets']:
            if row['plugin'] == 'taskcenter' and row not in journal.state.get('purge', []):
                set_scheduler(row['user'], row['desired'])
            record = L.RECOVERY / 'records' / (row['user'] + '-' + row['plugin'])
            if (record / 'current').exists():
                current = (record / 'current').read_text().strip()
                for old in record.glob('generation-*'):
                    if old.name != current:
                        remove_path(old)
                for name in ('registry-before-repair.json',):
                    remove_path(record / name)
    for stage in journal.state.get('stages', []):
        operations.clean_source_stage(stage['user'], stage['plugin'], Path(stage['src']), (stage['token'],))
    if journal.state['phase'] == 'committed' and journal.state.get('action') == 'uninstall':
        cleanup_unused_components()


def remove_shared_units(names):
    for name in names:
        if (UNITS / name).exists():
            run(['systemctl', 'stop', name])
            run(['systemctl', 'disable', name], False)
        for parent in UNITS.glob('*.wants'):
            remove_path(parent / name)
        remove_path(UNITS / name)
    run(['systemctl', 'daemon-reload'])


def cleanup_unused_components():
    """Post-commit teardown; retryable while the frozen journal engine remains."""
    installed = [row for row in inventory() if row['installed']]
    registry = L.read(L.COMPONENT / 'registry.json', {})
    grants = any(registry.get('users', {}).values())
    if not grants and not any(row['plugin'] in L.PRIVILEGED for row in installed):
        remove_shared_units(('minas-maintenance-events.service',))
        remove_path(Path('/etc/cron.d/minas-privileged-v2'))
        purge_root_state(L.COMPONENT)
    if installed:
        return
    remove_shared_units((WATCH, 'minas-plugin-recovery-v2.timer', 'minas-plugin-recovery-v2.service',
                         'minas-plugin-update-recover.service'))
    remove_path(Path('/etc/cron.d/minas-plugin-recovery-v2'))
    tombstones = set((L.RECOVERY / 'records').glob('u*-*/removed'))
    purge_root_state(L.RECOVERY, tombstones)
    purge_root_state(L.UPDATER)
    for name in COMPONENT_FILES['manager']:
        if name != 'bootstrap.py':
            remove_path(L.BASE / name)
    remove_path(L.BASE / 'installed.json')


def retire_unused_manager():
    # Keep bootstrap until the last durable transaction is gone. A power loss
    # during post-commit purge must still resume cleanup, never restore data.
    if list((L.BASE / 'transactions').glob('*/journal.json')) or any(r['installed'] for r in inventory()):
        return
    unit = 'minas-plugin-manager-recover.service'
    # This may be running from that oneshot; do not stop our own parent service.
    run(['systemctl', 'disable', unit], False)
    for parent in UNITS.glob('*.wants'):
        remove_path(parent / unit)
    remove_path(UNITS / unit)
    remove_path(L.BASE / 'bootstrap.py')
    run(['systemctl', 'daemon-reload'])


def recover_pending(already_locked=False):
    with contextlib.nullcontext() if already_locked else L.lock(L.INSTALL_LOCK):
        directory = L.BASE / 'transactions'
        recovered = []
        for path in sorted(directory.iterdir()) if directory.exists() else []:
            if not (path / 'journal.json').exists():
                continue
            journal = Journal(path, L.read(path / 'journal.json'))
            if journal.state['phase'] == 'initializing':
                remove_path(path)
                continue
            for row in journal.state['targets'] if journal.state['phase'] not in ('committed', 'rolled-back') else []:
                if row.get('pool'):
                    L.require(os.path.ismount('/nas/' + row['pool']), '等待事务存储池挂载：' + row['pool'])
            r = types.SimpleNamespace(daemon_unit=lambda user, plugin: 'minas-plugin-' + plugin + '-' + user + '.service')
            if journal.state['phase'] not in ('committed', 'rolled-back'):
                rollback(journal, r)
            finish(journal, r)
            recovered.append(journal.state)
        MAINTENANCE.unlink(missing_ok=True)
        if LEGACY_GUARD.is_file() and LEGACY_GUARD.read_text().startswith('manager:'):
            LEGACY_GUARD.unlink()
        for state in recovered:
            resume_observers(state)
        for path in sorted(directory.iterdir()) if directory.exists() else []:
            state = L.read(path / 'journal.json', {})
            if state.get('phase') in ('committed', 'rolled-back') and not state.get('pendingPools'):
                remove_path(path)
        if recovered and all(s.get('action') == 'uninstall' and s['phase'] == 'committed' for s in recovered):
            retire_unused_manager()


def resume_observers(state):
    committed = state['phase'] == 'committed'
    if (UNITS / 'minas-maintenance-events.service').exists() and (committed or state.get('eventsRunning')):
        run(['systemctl', 'start', 'minas-maintenance-events.service'])
    if (UNITS / WATCH).is_file() and (state.get('watchRunning') or committed and state.get('action') != 'uninstall'):
        run(['systemctl', 'start', WATCH])


def begin_maintenance(journal):
    # Scheduling reserves a worker under the shared component lock. Check those
    # reservations and publish the marker under its exclusive counterpart so a
    # queued worker cannot be rejected after its run has already been recorded.
    with L.lock(L.COMPONENT / 'component.lock') if L.COMPONENT.exists() else contextlib.nullcontext():
        admin = L.COMPONENT / 'common/component_admin.py'
        if admin.is_file():
            module(admin, 'minas_manager_idle').check_idle()
        L.atomic(MAINTENANCE, dict(transaction=journal.directory.name, pid=os.getpid(),
                                 targets=[[row['user'], row['plugin']] for row in journal.state['targets']]))


def apply(action, pairs, bundle=None, dry_run=False, pool=None, owned_update=None):
    pending = list((L.BASE / 'transactions').glob('*/journal.json'))
    if pending:
        if dry_run:
            return dict(action=action, requiresRecovery=True, transactions=[p.parent.name for p in pending],
                        targets=[dict(user=u, plugin=p) for u, p in pairs],
                        dataPolicy='delete' if action == 'uninstall' else 'preserve')
        recover_pending()
        L.require(action == 'uninstall' or not list((L.BASE / 'transactions').glob('*/journal.json')),
                  '卸载已完成可访问部分，尚有未挂载存储池待清理；挂载后运行 manage.sh recover')
    rows, cutover = preflight(action, pairs, bundle, pool)
    plan = dict(action=action, cutover=cutover, targets=rows,
                dataPolicy='delete' if action == 'uninstall' else 'preserve')
    if dry_run:
        return plan
    with L.lock(L.INSTALL_LOCK), contextlib.ExitStack() as held:
        recover_pending(already_locked=True)
        rows, cutover = preflight(action, pairs, bundle, pool)
        for row in rows:
            if (row['user'], row['plugin']) != owned_update:
                jobs = L.UPDATER / 'users' / row['user'] / row['plugin'] / 'jobs'
                for file in jobs.glob('*/status.json'):
                    status = L.read(file)
                    if status.get('running'):
                        unit = 'minas-update-' + file.parent.name + '.service'
                        active = run(['systemctl', 'is-active', '--quiet', unit], False).returncode == 0
                        L.require(not active and time.time() - status.get('updatedAt', 0) >= 60,
                                  '在线更新任务仍在运行：' + row['user'] + '/' + row['plugin'])
            if not row['fresh']:
                operations.idle(row['user'], row['plugin'], Path(row['home']), action == 'uninstall', row.get('incomplete', False))
        L.protected(L.BASE, 0o755)
        L.protected(L.BASE / 'transactions')
        L.require(not LEGACY_GUARD.exists(), '另一个维护操作正在使用原厂恢复暂停标记')
        journal = Journal(L.BASE / 'transactions' / uuid.uuid4().hex, phase='initializing')
        journal.state.update(action=action, targets=rows, cutover=cutover)
        journal.save()
        install_boot_recovery(journal)
        use_installed = (L.RECOVERY / 'recovery.py').is_file() and bundle and L.version(code_version(L.RECOVERY / 'recovery.py')) > L.version(bundle.manifest['components']['recovery']['version'])
        r = (types.SimpleNamespace(daemon_unit=lambda u,p:'minas-plugin-'+p+'-'+u+'.service') if action == 'uninstall'
             else module(bundle.component('recovery') / 'recovery.py', 'minas_incoming_recovery') if bundle and not use_installed else recovery())
        was_watch = run(['systemctl', 'is-active', '--quiet', WATCH], False).returncode == 0
        journal.state['watchRunning'] = was_watch
        journal.state['eventsRunning'] = run(['systemctl', 'is-active', '--quiet', 'minas-maintenance-events.service'], False).returncode == 0
        journal.save()
        try:
            begin_maintenance(journal)
        except BaseException:
            remove_path(journal.directory)
            raise
        L.atomic(LEGACY_GUARD, 'manager:' + journal.directory.name)
        run(['systemctl', 'stop', WATCH], False)
        run(['systemctl', 'stop', 'minas-maintenance-events.service'], False)
        try:
            journal.checkpoint('backup')
            for row in rows:
                directory = L.RECOVERY / 'records' / (row['user'] + '-' + row['plugin'])
                if directory.is_dir():
                    held.enter_context(L.lock(directory / '.service.lock'))
            # Close the gap between the read-only preflight and pausing event
            # dispatchers. A task reserved in that interval must block maintenance.
            for row in rows:
                if not row['fresh']:
                    operations.idle(row['user'], row['plugin'], Path(row['home']), action == 'uninstall', row.get('incomplete', False))
            component_guard = contextlib.ExitStack()
            if L.COMPONENT.exists():
                component_guard.enter_context(L.lock(L.COMPONENT / 'component.lock'))
            payloads = {}
            for path in watched_platform():
                capture_missing_parents(journal, path.parent)
                journal.capture(path)
            for row in rows:
                user, plugin = row['user'], row['plugin']
                payload = bundle.materialize(plugin, journal.directory / user) if bundle else None
                payloads[user, plugin] = payload
                journal.registration(user, plugin)
                journal.ports(user, plugin)
                for path in (Path(row['home']), Path(row['tmp']).parent, L.DATA, L.DATA / user, L.DATA / user / plugin):
                    if action == 'uninstall' and path.is_symlink():
                        continue
                    journal.metadata(path)
                for path in watched_files(row, payload):
                    capture_missing_parents(journal, path.parent)
                    journal.capture(path)
                if row['fresh']:
                    for path in (L.DATA / user / plugin, L.COMPONENT / 'users' / user if plugin == 'taskcenter' else L.DATA / user / plugin):
                        journal.capture(path)
                if plugin in L.SERVERS and row['wasRunning']:
                    journal.checkpoint('stop', row)
                    if action == 'uninstall':
                        stop_for_uninstall(user, plugin)
                    else:
                        run(['systemctl', 'stop', r.daemon_unit(user, plugin)])
                if plugin == 'taskcenter':
                    set_scheduler(user, False, component_locked=L.COMPONENT.exists())
            if bundle:
                journal.checkpoint('component')
                install_code(bundle, journal, r)
                if any(row['plugin'] in L.PRIVILEGED for row in rows):
                    install_privileged_gate(bundle)
            component_guard.close()
            for row in rows:
                if action in ('uninstall', 'reinstall'):
                    journal.checkpoint('remove', row)
                    remove_one(row, journal, r, preserve=action == 'reinstall')
                if action != 'uninstall':
                    install_one(row, payloads[row['user'], row['plugin']], journal, r)
            if bundle:
                r.install_units(start=False)
                journal.capture(L.BASE / 'installed.json')
                L.atomic(L.BASE / 'installed.json', dict(format=2, version=VERSION))
            journal.state['phase'] = 'committed'
            journal.save()
        except BaseException as original:
            if 'component_guard' in locals():
                component_guard.close()
            failure = dict(step=journal.state.get('step'), target=journal.state.get('currentTarget'),
                           message=failure_summary(original))
            journal.state['failure'] = failure
            journal.save()
            try:
                rollback(journal, r)
            except BaseException as rollback_error:
                journal.state['rollbackFailure'] = failure_summary(rollback_error)
                journal.save()
                raise RuntimeError('阶段 ' + str(failure['step']) + ' 失败：' + failure['message'] +
                    '；回退失败：' + failure_summary(rollback_error) + '。材料保留于 ' + str(journal.directory) +
                    '，请运行 manage.sh recover') from rollback_error
            raise RuntimeError('阶段 ' + str(failure['step']) + ' 失败，已恢复原安装：' + failure['message']) from original
        finally:
            if journal.state['phase'] in ('committed', 'rolled-back'):
                try:
                    finish(journal, r)
                    held.close()
                    MAINTENANCE.unlink(missing_ok=True)
                    LEGACY_GUARD.unlink(missing_ok=True)
                    resume_observers(journal.state)
                    if not journal.state.get('pendingPools'):
                        remove_path(journal.directory)
                    else:
                        run(['systemctl', 'start', '--no-block', 'minas-plugin-manager-recover.service'])
                    if action == 'uninstall':
                        retire_unused_manager()
                except Exception as error:
                    # A committed operation must never roll back for cleanup.
                    raise RuntimeError('事务已提交或回退，但清理/恢复监听未完成，请运行 recover：' + str(error)) from error
        return dict(ok=True, action=action, targets=[dict(user=r['user'], plugin=r['plugin'], notes=r.get('notes', [])) for r in rows],
                    pendingPools=journal.state.get('pendingPools', []))


def repair_record(directory, intents=None, check_source=True):
    """The watcher holds the installer lock; recovery uses the same journal."""
    r = recovery()
    if MAINTENANCE.exists():
        return
    if restoration.record_healthy(r, directory, intents, check_source):
        return
    saved = r.load_generation(directory)
    record = r.read(saved / 'record.json')
    row = target(record['user'], record['plugin'])
    L.protected(L.BASE / 'transactions')
    journal = Journal(L.BASE / 'transactions' / uuid.uuid4().hex, phase='initializing')
    journal.state.update(targets=[row], action='repair')
    journal.save()
    install_boot_recovery(journal)
    try:
        begin_maintenance(journal)
    except BaseException:
        remove_path(journal.directory)
        raise
    try:
        journal.registration(row['user'], row['plugin'])
        journal.ports(row['user'], row['plugin'])
        for path in (Path(row['home']), Path(row['tmp']).parent, L.DATA, L.DATA / row['user'], L.DATA / row['user'] / row['plugin']):
            journal.metadata(path)
        paths = [p for p in watched_files(row, saved) if p != directory and (check_source or p != Path(row['src']))]
        paths += [saved / 'record.json', directory / 'service-intent.json', *watched_platform()]
        for path in paths:
            capture_missing_parents(journal, path.parent)
            journal.capture(path)
        restoration.restore_locked_record(r, directory, intents, check_source, transaction=journal.directory.name)
        journal.state['phase'] = 'committed'
        journal.save()
    except BaseException:
        rollback(journal, r)
        raise
    finally:
        if journal.state['phase'] in ('committed', 'rolled-back'):
            finish(journal, r)
            MAINTENANCE.unlink(missing_ok=True)
            resume_observers(journal.state)
            remove_path(journal.directory)
