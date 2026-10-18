#!/usr/bin/env python3
"""Root-installed recovery coordinator. No HTTP, sudo or user command interface."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
BASE = Path('/data/.minas-plugin-recovery')
DATA = Path('/data/.minas-plugin-data')
PLUGINS = {'mihomo', 'dockermanager', 'taskcenter', 'nascenter', 'sshrecovery', 'quickshare', 'downloadcenter'}
SERVERS = {'mihomo', 'quickshare', 'downloadcenter'}
UNIT = 'minas-plugin-recovery'
VERSION = '1.1.0'


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def plain(path):
    """Reject redirected ancestors before any privileged filesystem operation."""
    path = Path(path)
    require(path.is_absolute(), 'path must be absolute')
    for node in (path, *path.parents):
        require(not node.is_symlink(), 'unexpected symlink: ' + str(node))
    return path


def read(path):
    value = json.loads(plain(path).read_text(encoding='utf-8'))
    require(isinstance(value, dict), 'JSON must be an object: ' + str(path))
    return value


def atomic(path, content, mode=0o600, owner=None):
    path = plain(path)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), mode)
            if owner is not None:
                os.fchown(output.fileno(), *owner)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def save(path, value, owner=None):
    previous = path.stat() if path.exists() else None
    atomic(path, encoded(value), stat.S_IMODE(previous.st_mode) if previous else 0o600,
           owner if owner else ((previous.st_uid, previous.st_gid) if previous else None))


def protected(path, mode=0o700):
    plain(path).mkdir(parents=True, exist_ok=True)
    require(path.stat().st_uid == 0, 'root-owned directory required: ' + str(path))
    path.chmod(mode)


def identity(user, plugin):
    require(re.fullmatch(r'u[0-9]+', user) and plugin in PLUGINS, 'invalid user/plugin')
    account = pwd.getpwnam(user)
    return Path('/home') / user / 'plugin' / plugin, (account.pw_uid, account.pw_gid)


def source_path(home, user, plugin):
    require((home / 'src').is_symlink(), 'official src link required')
    src = Path(os.readlink(home / 'src'))
    match = re.fullmatch(r'/nas/(pool[0-9]+)/' + user + r'/plugin/pluginsrc/' + plugin, str(src))
    require(match is not None, 'invalid source pool path')
    require(os.path.ismount('/nas/' + match[1]), 'data pool not mounted')
    plain(src)
    return src


def digest(src):
    result = hashlib.sha256()
    files = []
    for parent, dirs, names in os.walk(src, followlinks=False):
        for name in names:
            path = Path(parent) / name
            if stat.S_ISREG(path.lstat().st_mode):
                files.append(path)
    for path in sorted(files, key=lambda p: os.fsencode(str(p))):
        h = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                h.update(block)
        result.update((h.hexdigest() + '\n').encode())
    return result.hexdigest()


def env(user, plugin, home, src):
    return dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PLUG_USER=user, PLUG_NAME=plugin,
                PLUG_HOME_DIR=str(home), PLUG_SRC_DIR=str(src), PLUG_TMP_DIR=str(home / 'tmp'), PLUG_STATUS='unverified')


def call(command, *, environment=None, check=True, timeout=120):
    return subprocess.run([str(v) for v in command], env=environment, check=check, timeout=timeout,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def control(user, plugin, home, src, action, check=True):
    # No root execution of control scripts from the user's plugin tree.
    return call(['runuser', '-u', user, '--', '/bin/sh', home / 'scripts/control', action],
                environment=env(user, plugin, home, src), check=check)


@contextlib.contextmanager
def user_lock(user):
    with open(plain(Path('/data/plugin/.' + user + '.plugins.lock')), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def linked(path, target, owner):
    plain(path.parent)
    if path.is_symlink():
        require(os.readlink(path) == str(target), 'unexpected link: ' + str(path))
    else:
        require(not path.exists(), 'path occupied: ' + str(path))
        path.symlink_to(target)
    os.lchown(path, *owner)


def normalize(user, plugin, payload):
    home, owner = identity(user, plugin)
    plain(home)
    src = source_path(home, user, plugin)
    installed = read(home / 'INFO')
    template = read(payload / 'INFO')
    require(installed['plugin'] == plugin == template['plugin'], 'INFO plugin mismatch')
    port = str(installed.get('port', ''))
    if plugin == 'downloadcenter':
        port = (home / 'var/rpc.port').read_text().strip()
    info = dict(template, abstract=digest(src), timestamp=int(time.time()),
                size=sum(p.stat().st_size for p in src.rglob('*') if p.is_file() and not p.is_symlink()),
                port=port, hotplug=installed.get('hotplug', []))
    # An INFO symlink is excluded by the firmware's find -type f digest.
    # Two readable INFO paths, without introducing an impossible self-hash.
    atomic(home / 'INFO', encoded(info), 0o644, owner)
    linked(src / 'INFO', home / 'INFO', owner)
    config = read(src / 'ui/config')
    require(all('#/' in item['url'] for item in config['url']), 'desktop hash route missing')
    icon = (payload / 'src/ui/icon.png').read_bytes()
    require(icon[:8] == b'\x89PNG\r\n\x1a\n' and icon[16:24] == (300).to_bytes(4, 'big') * 2,
            '300x300 PNG required')
    icon_path = Path('/data/plugin/www/icon') / (plugin + '.icon')
    atomic(icon_path, icon, 0o644, owner)
    registry = Path('/data/plugin') / (user + '.list')
    with user_lock(user):
        data = read(registry)
        previous = data.get(plugin, {})
        entry = dict(previous, resource=dict(mpk=str(home / 'INFO'), icon='/icon/' + plugin + '.icon', preview=[]),
                     info=info, frontend=config, install=True, upgrade=False, progress='100',
                     icon='/icon/' + plugin + '.icon', changetime=int(time.time()))
        data[plugin] = entry
        save(registry, data)
    return home, src, owner, entry


def persist_state(user, plugin, home, src, owner, enabled):
    """Stop user services, move data once, preserve inode when on one filesystem."""
    protected(DATA, 0o711)
    protected(DATA / user, 0o711)
    destination = DATA / user / plugin
    protected(destination, 0o711)
    running = plugin in SERVERS and control(user, plugin, home, src, 'status', False).returncode == 0
    pending = any(not (home / name).is_symlink() for name in ('etc', 'var'))
    enabled_file = home / 'var/enabled'
    download_enabled = plugin == 'downloadcenter' and enabled_file.is_file()
    if running and pending:
        control(user, plugin, home, src, 'stop')
    try:
        for name in ('etc', 'var'):
            current, target = home / name, destination / name
            if current.is_symlink():
                require(os.readlink(current) == str(target), 'unmanaged state symlink')
                plain(target)
                require(target.is_dir(), 'persistent state missing')
                continue
            plain(current)
            require(not target.exists(), 'persistent state already exists; refuse overwrite: ' + str(target))
            if not current.exists():
                target.mkdir(mode=0o700)
                os.chown(target, *owner)
            elif current.stat().st_dev == destination.stat().st_dev:
                os.replace(current, target)
            else:
                # Copy as the owner; do not follow links to user caches as root.
                target.mkdir(mode=0o700)
                os.chown(target, *owner)
                try:
                    call(['runuser', '-u', user, '--', 'cp', '-a', str(current) + '/.', target])
                except BaseException:
                    shutil.rmtree(target)
                    raise
                # Keep a migration rollback directory until the link is installed.
                old = current.with_name('.' + name + '.pre-persistence')
                require(not old.exists(), 'unfinished data migration')
                os.replace(current, old)
                try:
                    linked(current, target, owner)
                except BaseException:
                    os.replace(old, current)
                    raise
                shutil.rmtree(old)
            target.chmod(0o700)
            linked(current, target, owner)
        if download_enabled:
            # stop removed this flag; persistence must retain its prior meaning.
            atomic(destination / 'var/enabled', b'', 0o600, owner)
    finally:
        if running and pending and enabled:
            control(user, plugin, home, src, 'start')


def port_registry(user, plugin, port, remove=False):
    # CGI-only plugins have no listener. Do not invent/reserve a dummy TCP port.
    if not remove and not port:
        return
    if not remove:
        require(str(port).isdigit() and 1024 <= int(port) <= 65535, 'invalid plugin port')
    for path in (Path('/etc/config/plugin'), Path('/data/etc/upper/config/plugin')):
        if not path.is_file():
            continue
        data = read(path)
        ports = data.setdefault('ports', {})
        require(isinstance(ports, dict), 'invalid ports table')
        previous = encoded(data)
        values = ports.setdefault(plugin, {}) if not remove else ports.get(plugin, {})
        require(isinstance(values, dict), 'invalid per-plugin ports')
        if remove:
            values.pop(user, None)
            if not values:
                ports.pop(plugin, None)
        else:
            for name, users in ports.items():
                if isinstance(users, dict):
                    require(not any(int(v) == int(port) and (name, u) != (plugin, user)
                                    for u, v in users.items() if str(v).isdigit()), 'port reserved by another plugin')
            values[user] = int(port)
        if encoded(data) != previous:
            backup = BASE / 'config-backups'
            protected(backup)
            key = str(path).replace('/', '_')
            if not (backup / key).exists():
                atomic(backup / key, previous)
            save(path, data)


def verify(user, plugin, home, src):
    call(['runuser', '-u', user, '--', '/usr/bin/plugin.sh', 'verify'], environment=env(user, plugin, home, src))


def install_units():
    require((BASE / 'recovery.py').is_file(), 'recovery program not installed')
    service = '''[Unit]
Description=Restore local Xiaomi NAS plugins
After=local-fs.target network.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -B /data/.minas-plugin-recovery/recovery.py reconcile
TimeoutStartSec=600
UMask=0077
'''
    timer = '''[Unit]
Description=Check local Xiaomi NAS plugin registration
[Timer]
OnBootSec=90
OnUnitInactiveSec=30
Persistent=true
Unit=minas-plugin-recovery.service
[Install]
WantedBy=timers.target
'''
    root = Path('/etc/systemd/system')
    for suffix, content in (('service', service), ('timer', timer)):
        path = root / (UNIT + '.' + suffix)
        if not path.exists() or path.read_text() != content:
            atomic(path, content.encode(), 0o644)
    # Cron is independent of the systemd unit; both serialize on the same lock.
    cron = ('SHELL=/bin/sh\nPATH=/usr/sbin:/usr/bin:/sbin:/bin\nMAILTO=""\n'
            '* * * * * root /usr/bin/python3 -B /data/.minas-plugin-recovery/recovery.py bootstrap --quiet\n')
    atomic(Path('/etc/cron.d') / UNIT, cron.encode(), 0o644)
    call(['systemctl', 'daemon-reload'])
    call(['systemctl', 'enable', '--now', UNIT + '.timer'])
    call(['systemctl', 'reload', 'crond.service'], check=False)


def snapshot(user, plugin, payload):
    home, src, owner, entry = normalize(user, plugin, payload)
    verify(user, plugin, home, src)
    report = call(['plugincenter', '-u', user, 'list'])
    try:
        response = json.loads(report.stdout)
    except ValueError:
        response = None
    if isinstance(response, dict) and 'code' in response:
        require(response['code'] == 0, 'plugincenter list failed')
    persist_state(user, plugin, home, src, owner, entry.get('enable', True))
    verify(user, plugin, home, src)
    protected(BASE)
    protected(BASE / 'records')
    directory = BASE / 'records' / (user + '-' + plugin)
    protected(directory)
    stage = Path(tempfile.mkdtemp(prefix='.snapshot-', dir=directory))
    try:
        # Package code only. Runtime user state has a separate persistent home.
        shutil.copytree(payload / 'scripts', stage / 'scripts')
        if (payload / 'installer/common').is_dir():
            (stage / 'installer').mkdir()
            shutil.copytree(payload / 'installer/common', stage / 'installer/common')
        shutil.copytree(src, stage / 'src', symlinks=True)
        require(digest(stage / 'src') == entry['info']['abstract'], 'source changed while taking recovery snapshot')
        for p in (stage / 'src').rglob('*'):
            if p.is_symlink():
                require(p.relative_to(stage / 'src').as_posix() == 'INFO' and os.readlink(p) == str(home / 'INFO'),
                        'unexpected source symlink')
        atomic(stage / 'INFO', (home / 'INFO').read_bytes())
        record = dict(user=user, plugin=plugin, src=str(src), tmp=str(Path(os.readlink(home / 'tmp'))),
                      entry=entry, enabled=bool(entry.get('enable', True)),
                      running=plugin in SERVERS and control(user, plugin, home, src, 'status', False).returncode == 0)
        atomic(stage / 'record.json', encoded(record))
        # A committed generation is selected by a same-directory atomic pointer.
        generation = 'generation-' + str(time.time_ns())
        os.replace(stage, directory / generation)
        atomic(directory / 'current', generation.encode())
        for old in directory.glob('generation-*'):
            if old.name != generation:
                shutil.rmtree(old)
        (directory / 'removed').unlink(missing_ok=True)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    # Installed entry and port registration use the actual runtime port.
    port_registry(user, plugin, entry['info']['port'])
    atomic(BASE / 'recovery.py', Path(__file__).read_bytes(), 0o700)
    install_units()
    print('恢复登记完成：' + user + '/' + plugin)


def load_generation(directory):
    plain(directory)
    generation = (directory / 'current').read_text()
    require(re.fullmatch(r'generation-[0-9]+', generation), 'invalid recovery generation')
    return plain(directory / generation)


def restore_component(saved, user, plugin):
    payload = saved if (saved / 'scripts').is_dir() else saved / 'payload'
    common = payload / 'installer/common'
    if not common.exists(): common = payload / 'common'
    admin = common / 'component_admin.py'
    if not admin.is_file():
        return
    required = (Path('/data/.minas-privileged/minas-helper'),
                Path('/data/.minas-privileged/registry.json'),
                Path('/etc/sudoers.d') / (plugin + '-' + user), Path('/etc/cron.d/minas-privileged'))
    manifest = read(common / 'manifest.json')
    code_present = all(plain(Path('/data/.minas-privileged') / name).is_file() for name in manifest['files'])
    if code_present and all(plain(path).is_file() for path in required):
        grants = read(Path('/data/.minas-privileged/registry.json')).get('users', {}).get(user, {})
        if plugin in grants:
            return
    # Reapply missing sudo/cron entries from a root-owned package. Existing root
    # grants, task state and SSH recovery material are owned by component_admin.
    call([sys.executable, '-B', admin, 'install', '--plugin', plugin, '--user', user,
          '--payload', payload, '--plugin-version', read(saved / 'INFO')['version']])


def restore_missing_source(src, backup, owner):
    """Complete partial cleanup; preserve conflicting local edits for inspection."""
    for path in src.rglob('*'):
        expected = backup / path.relative_to(src)
        require(expected.exists() or expected.is_symlink(), 'extra source file: ' + str(path))
        if path.is_symlink():
            require(expected.is_symlink() and os.readlink(path) == os.readlink(expected), 'changed source link')
        elif path.is_file():
            require(expected.is_file() and not expected.is_symlink() and path.read_bytes() == expected.read_bytes(),
                    'source changed; refusing silent rollback')
        else:
            require(path.is_dir() and expected.is_dir(), 'invalid source node')
    for expected in sorted(backup.rglob('*'), key=lambda p: len(p.parts)):
        path = src / expected.relative_to(backup)
        if path.exists() or path.is_symlink() or expected.is_symlink():
            continue
        if expected.is_dir():
            plain(path).mkdir()
            os.chown(path, *owner)
            path.chmod(stat.S_IMODE(expected.stat().st_mode))
        else:
            atomic(path, expected.read_bytes(), stat.S_IMODE(expected.stat().st_mode), owner)


def restore_record(directory):
    if (directory / 'removed').exists():
        return
    saved = load_generation(directory)
    record = read(saved / 'record.json')
    user, plugin = record['user'], record['plugin']
    home, owner = identity(user, plugin)
    require(directory.name == user + '-' + plugin, 'record identity mismatch')
    src = Path(record['src'])
    match = re.fullmatch(r'/nas/(pool[0-9]+)/' + user + r'/plugin/pluginsrc/' + plugin, str(src))
    require(match and os.path.ismount('/nas/' + match[1]), 'data pool not ready')
    plain(home)
    plain(src)
    registry = Path('/data/plugin') / (user + '.list')
    # Never restore a removed account or overwrite a broken registry with {}.
    require(registry.is_file(), 'user registry not ready')
    with user_lock(user):
        data = read(registry)
        live = data.get(plugin)
        expected_info = read(saved / 'INFO')
        source_ok = src.is_dir() and digest(src) == expected_info['abstract']
        complete = ((src / 'ui/config').is_file() and (home / 'INFO').is_file()
                    and all((home / 'scripts' / p.name).is_file() for p in ((saved / 'scripts') if (saved / 'scripts').is_dir() else (saved / 'payload/scripts')).iterdir())
                    and all((home / name).is_symlink() for name in ('src', 'tmp', 'etc', 'var')) and source_ok)
        if complete and isinstance(live, dict) and live.get('install') is True and live.get('info', {}).get('version') == expected_info['version']:
            # Observe deliberate disable/enable while a complete installation exists.
            previously_enabled = record['enabled']
            record['enabled'] = live.get('enable', record['enabled']) is True
            record['entry']['enable'] = record['enabled']
            record['entry']['status'] = live.get('status', 'stopped')
            record['entry']['changetime'] = live.get('changetime', record['entry']['changetime'])
            if plugin in SERVERS:
                if previously_enabled != record['enabled']:
                    record['running'] = record['enabled']
                elif control(user, plugin, home, src, 'status', False).returncode == 0:
                    record['running'] = True
            if read(saved / 'record.json') != record:
                save(saved / 'record.json', record)
        repair = not complete or not isinstance(live, dict) or live.get('install') is not True
        if (home / 'INFO').is_file():
            require(read(home / 'INFO')['version'] == expected_info['version'], 'different installed version; capture it explicitly')
        if src.is_dir() and not source_ok:
            restore_missing_source(src, saved / 'src', owner)
            require(digest(src) == expected_info['abstract'], 'source changed; refusing silent rollback')
        if complete:
            require(digest(src) == expected_info['abstract'], 'source changed; refusing silent rollback')
        if repair:
            home.mkdir(parents=True, exist_ok=True)
            os.chown(home, *owner)
            home.chmod(0o700)
            if not src.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                # Restore through a same-filesystem staging directory.
                stage = Path(tempfile.mkdtemp(prefix='.' + plugin + '-restore-', dir=src.parent))
                try:
                    shutil.copytree(saved / 'src', stage, dirs_exist_ok=True, symlinks=True)
                    for p in [stage, *stage.rglob('*')]:
                        os.chown(p, *owner, follow_symlinks=False)
                    os.replace(stage, src)
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
            require(digest(src) == expected_info['abstract'], 'incomplete source: retained for manual inspection')
            protected(DATA, 0o711)
            destination = plain(DATA / user / plugin)
            for name in ('etc', 'var'):
                require(plain(destination / name).is_dir(), 'persistent data missing; refusing empty state')
                linked(home / name, destination / name, owner)
            scripts = plain(home / 'scripts')
            scripts.mkdir(exist_ok=True)
            os.chown(scripts, *owner)
            for p in ((saved / 'scripts') if (saved / 'scripts').is_dir() else (saved / 'payload/scripts')).iterdir():
                atomic(scripts / p.name, p.read_bytes(), 0o755, owner)
            atomic(home / 'INFO', encoded(expected_info), 0o644, owner)
            linked(home / 'src', src, owner)
            linked(src / 'INFO', home / 'INFO', owner)
            tmp = Path(record['tmp'])
            require(str(tmp) == str(src).replace('/pluginsrc/', '/plugintmp/'), 'invalid temporary path')
            # Creation and access to the official user cache happen after dropping privileges.
            call(['runuser', '-u', user, '--', 'mkdir', '-p', tmp])
            linked(home / 'tmp', tmp, owner)
        webroot = Path(read(Path('/etc/config/plugin')).get('settings', {}).get('nginx_plugin', '/data/plugin/www'))
        plain(webroot / user).mkdir(parents=True, exist_ok=True)
        web = webroot / user / plugin
        if web.is_symlink() and os.readlink(web) == str(src / 'ui'):
            web.unlink()
        linked(web, home / 'src/ui', owner)
        icon = Path('/data/plugin/www/icon') / (plugin + '.icon')
        plain(icon.parent).mkdir(parents=True, exist_ok=True)
        icon_source = saved / 'src/ui/icon.png'
        if not icon_source.exists(): icon_source = saved / 'payload/ui/icon.png'
        expected_icon = icon_source.read_bytes()
        if not icon.is_file() or icon.read_bytes() != expected_icon:
            atomic(icon, expected_icon, 0o644, owner)
        port_registry(user, plugin, expected_info['port'])
        # Merge exactly one entry; all official/other-user entries remain intact.
        entry = dict(record['entry'], enable=record['enabled'])
        if data.get(plugin) != entry:
            backup = directory / 'registry-before-repair.json'
            atomic(backup, registry.read_bytes())
            data[plugin] = entry
            save(registry, data)
        verify(user, plugin, home, src)
    # Do not hold the list lock while lifecycle hooks update the list themselves.
    restore_component(saved, user, plugin)
    if plugin in SERVERS and record['enabled'] and record['running']:
        if control(user, plugin, home, src, 'status', False).returncode:
            control(user, plugin, home, src, 'start')
    # Task scheduler/SSH actions are never enabled/replayed here.
    if repair:
        print('已恢复：' + user + '/' + plugin, flush=True)


def reconcile():
    failures = 0
    records = BASE / 'records'
    if not records.is_dir():
        return
    for directory in sorted(records.iterdir()):
        if not directory.is_dir():
            continue
        try:
            restore_record(directory)
        except Exception as error:
            failures += 1
            print(directory.name + ': ' + str(error), file=sys.stderr, flush=True)
    require(not failures, str(failures) + ' recovery record(s) need attention')


def forget(user, plugin):
    identity(user, plugin)
    directory = BASE / 'records' / (user + '-' + plugin)
    if directory.is_dir():
        atomic(directory / 'removed', b'explicit manage.sh uninstall\n')
    port_registry(user, plugin, '', remove=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('capture', 'normalize', 'reconcile', 'bootstrap', 'forget', 'status'))
    parser.add_argument('--user')
    parser.add_argument('--plugin', choices=sorted(PLUGINS))
    parser.add_argument('--package', '--payload', dest='payload', type=Path)
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args()
    require(os.geteuid() == 0, 'root required')
    protected(BASE)
    # Install/uninstall already owns this lock; their child coordinator must not
    # reacquire it. Timer/Cron use a nonblocking lock and retry on the next tick.
    with contextlib.ExitStack() as stack:
        if args.action in ('reconcile', 'bootstrap'):
            lock = stack.enter_context(open(plain(Path('/data/plugin/.local-plugin-installer.lock')), 'a'))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
        if args.action in ('capture', 'normalize'):
            require(args.user and args.plugin and args.payload, 'user/plugin/payload required')
            plain(args.payload.resolve())
            if args.action == 'capture':
                snapshot(args.user, args.plugin, args.payload.resolve())
            else:
                normalize(args.user, args.plugin, args.payload.resolve())
        elif args.action == 'forget':
            forget(args.user, args.plugin)
        elif args.action == 'bootstrap':
            install_units()
            reconcile()
        elif args.action == 'reconcile':
            reconcile()
        else:
            records = BASE / 'records'
            for directory in sorted(records.iterdir()) if records.exists() else []:
                print(directory.name + (' removed' if (directory / 'removed').exists() else ' registered'))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('插件恢复失败：' + str(error), file=sys.stderr)
        sys.exit(1)
