#!/usr/bin/env python3
"""Root-installed recovery coordinator. No HTTP, sudo or user command interface."""
import argparse
import contextlib
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import select
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
BASE = Path('/data/.minas-plugin-recovery')
DATA = Path('/data/.minas-plugin-data')
PLUGINS = {'mihomo', 'dockermanager', 'taskcenter', 'nascenter', 'sshrecovery', 'quickshare', 'downloadcenter'}
SERVERS = {'mihomo', 'quickshare', 'downloadcenter'}
MANAGED = SERVERS | {'taskcenter'}
UNIT = 'minas-plugin-recovery'
VERSION = '1.4.0'
WATCH_UNIT = UNIT + '-watch.service'
RUNTIME = Path('/run/minas-plugin-recovery')


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
    if stat.S_IMODE(path.stat().st_mode) != mode:
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
    if plugin in SERVERS and (Path('/etc/systemd/system') / daemon_unit(user, plugin)).is_file():
        operation = 'is-active' if action == 'status' else action
        return call(['systemctl', operation, daemon_unit(user, plugin)], check=check)
    return legacy_control(user, plugin, home, src, action, check)


def legacy_control(user, plugin, home, src, action, check=True):
    return call(['runuser', '-u', user, '--', '/bin/sh', home / 'scripts/control', action],
                environment=dict(env(user, plugin, home, src), MINAS_SERVICE_MANAGED='1'), check=check)


def daemon_unit(user, plugin):
    require(re.fullmatch(r'u[0-9]+', user) and plugin in SERVERS, 'invalid daemon identity')
    return 'minas-plugin-' + plugin + '-' + user + '.service'


@contextlib.contextmanager
def record_lock(directory):
    plain(directory)
    with open(plain(directory / '.service.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def install_daemon(user, plugin):
    home, owner = identity(user, plugin)
    unit = daemon_unit(user, plugin)
    pidfile = {'mihomo': 'mihomo.pid', 'quickshare': 'server.pid', 'downloadcenter': 'service.pid'}[plugin]
    content = ('[Unit]\nDescription=Local NAS plugin ' + plugin + ' (' + user + ')\nAfter=local-fs.target network.target\n'
        '[Service]\nType=forking\nUser=' + user + '\nGroup=' + str(owner[1]) + '\n'
        'Environment=MINAS_SERVICE_MANAGED=1 PYTHONDONTWRITEBYTECODE=1 PLUG_USER=' + user +
        ' PLUG_HOME_DIR=' + str(home) + ' PLUG_SRC_DIR=' + str(home / 'src') + '\n'
        'WorkingDirectory=' + str(home) + '\nPIDFile=' + str(home / 'tmp/runtime' / pidfile) + '\n'
        'ExecStart=/bin/sh ' + str(home / 'scripts/control') + ' start\n'
        'ExecStop=/bin/sh ' + str(home / 'scripts/control') + ' stop\n'
        # Explicit systemctl stop (including user/lifecycle stops) suppresses
        # Restart=. Unexpected failures recover without scanning plugin files.
        'GuessMainPID=no\nRestart=on-failure\nRestartSec=5\nKillMode=control-group\nTimeoutStartSec=90\nTimeoutStopSec=90\nUMask=0077\n')
    target = Path('/etc/systemd/system') / unit
    if not target.exists() or target.read_text() != content:
        atomic(target, content.encode(), 0o644)
        call(['systemctl', 'daemon-reload'])
    # Only fixed actions for this user's registered plugin. No command/path input.
    operations = ('start', 'stop', 'restart', 'status', 'lifecycle-start', 'lifecycle-stop')
    if plugin == 'downloadcenter': operations += ('update-start', 'update-stop', 'update-finish')
    prefix = '/usr/bin/python3 -B ' + str(BASE / 'recovery.py') + ' service --user ' + user + ' --plugin ' + plugin + ' --operation '
    policy = user + ' ALL=(root) NOPASSWD: ' + ', '.join(prefix + action for action in operations) + '\n'
    rules = Path('/etc/sudoers.d') / ('minas-service-' + user + '-' + plugin)
    if not rules.exists() or rules.read_text() != policy:
        fd, name = tempfile.mkstemp(prefix='.minas-service-', dir=rules.parent)
        try:
            with os.fdopen(fd, 'w') as output:
                output.write(policy); output.flush(); os.fsync(output.fileno()); os.fchmod(output.fileno(), 0o440)
            call(['visudo', '-cf', name])
            os.replace(name, rules)
        finally:
            if os.path.exists(name): os.unlink(name)
    marker = Path('/etc/minas-plugin-services')
    protected(marker, 0o755)
    marker_file = marker / (user + '-' + plugin)
    if not marker_file.is_file() or marker_file.read_bytes() != unit.encode():
        atomic(marker_file, unit.encode(), 0o644)


def service_operation(user, plugin, operation):
    identity(user, plugin)
    unit = daemon_unit(user, plugin)
    directory = BASE / 'records' / (user + '-' + plugin)
    require(directory.is_dir() and not (directory / 'removed').exists(), 'plugin is not registered')
    if operation == 'status':
        return call(['systemctl', 'is-active', '--quiet', unit], check=False).returncode
    with record_lock(directory):
        require((Path('/etc/systemd/system') / unit).is_file(), 'daemon unit missing')
        if operation.startswith('update-'):
            require(plugin == 'downloadcenter', 'unsupported update lifecycle')
            if operation == 'update-finish':
                (directory / 'service-update.json').unlink(missing_ok=True)
                return 0
            save(directory / 'service-update.json', dict(until=time.time() + 600))
            result = call(['systemctl', operation.removeprefix('update-'), unit], check=False)
            require(result.returncode == 0, '下载核心切换时服务操作失败')
            return 0
        if operation == 'lifecycle-start':
            record = read(load_generation(directory) / 'record.json')
            apply_service_intent(record, directory)
            if not record['running']:
                return 0
        if not operation.startswith('lifecycle-'):
            # A dedicated intent file cannot be overwritten by an in-flight
            # registry refresh. A stop keeps the UI installed and accessible.
            save(directory / 'service-intent.json', dict(running=operation != 'stop', time=time.time()))
        action = operation.removeprefix('lifecycle-')
        result = call(['systemctl', action, unit], check=False)
        require(result.returncode == 0, '服务操作失败，请查看服务日志：' + unit)
    return 0


def apply_service_intent(record, directory):
    path = directory / 'service-intent.json'
    if not path.is_file(): return
    intent = read(path)
    require(isinstance(intent.get('running'), bool) and isinstance(intent.get('time'), (int, float)), 'invalid service intent')
    if intent['time'] < record.get('runtime_intent_time', record.get('native_intent_time', 0)):
        return
    record['running'] = intent['running']
    record['service_intent_time'] = intent['time']
    record['runtime_intent_time'] = intent['time']


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
    metadata = path.lstat()
    if (metadata.st_uid, metadata.st_gid) != owner:
        os.lchown(path, *owner)


def prepare_tmp(user, src, tmp, owner):
    """Initialize the trusted parent only; access cache entries as their owner."""
    require(str(tmp) == str(src).replace('/pluginsrc/', '/plugintmp/'), 'invalid temporary path')
    parent = plain(tmp.parent)
    # Firmware creates this shared, per-user parent as root:root 0755. Do not
    # inspect/chown existing cache contents or make another user's cache writable.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(parent.anchor, flags)
    try:
        # Hold each ancestor open, rather than reopening an absolute pathname
        # after validation: user-writable ancestors cannot redirect root's
        # metadata operation through a symlink race.
        for index, part in enumerate(parent.parts[1:], 1):
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                require(index == len(parent.parts) - 1, 'temporary parent ancestor missing')
                os.mkdir(part, mode=0o755, dir_fd=fd)
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        metadata = os.fstat(fd)
        require(metadata.st_uid in (0, owner[0]), 'unexpected temporary parent owner')
        os.fchown(fd, metadata.st_uid, owner[1])
        os.fchmod(fd, stat.S_IMODE(metadata.st_mode) | 0o070)
    finally:
        os.close(fd)
    script = '''import os, stat, sys
from pathlib import Path
p = Path(sys.argv[1])
for node in (p, *p.parents):
    if node.is_symlink(): raise RuntimeError('redirected temporary path')
p.mkdir(mode=0o700, exist_ok=True)
s = p.lstat()
if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid():
    raise RuntimeError('unexpected temporary directory owner/type')
p.chmod(0o700)
if not os.access(p, os.W_OK | os.X_OK): raise RuntimeError('temporary directory not writable')
'''
    result = call(['runuser', '-u', user, '--', sys.executable, '-B', '-c', script, tmp], check=False)
    # This fixed script accepts only the validated cache path, no configuration
    # or credentials. Its last error line is safe and useful in the journal.
    error = result.stderr.decode('utf-8', errors='replace').strip().splitlines()
    require(result.returncode == 0, 'temporary directory initialization failed: ' + str(tmp)
            + ('; ' + error[-1][:400] if error else ''))


def native_intents():
    """Read successful explicit toggles; boot/uninstall hook calls are excluded."""
    try:
        result = call(['journalctl', '--boot', 'SYSLOG_IDENTIFIER=plugincenter',
                       '--no-pager', '-o', 'json'], check=False, timeout=20)
        if result.returncode:
            return {}
        return parse_native_intents(result.stdout.decode('utf-8', errors='replace'))
    except (OSError, subprocess.TimeoutExpired):
        return {}


def parse_native_intents(output):
    pending, intents = {}, {}
    for line in output.splitlines():
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            message, pid = row.get('MESSAGE', ''), row.get('SYSLOG_PID', row.get('_PID'))
            started = re.search(r'\[do_(enable|disable):[0-9]+\] (u[0-9]+) (enable|disable) plugin ([a-z0-9]+)$', message)
            if started:
                if started[1] == started[3] and started[4] in PLUGINS:
                    pending[pid] = (started[2], started[4], started[1] == 'enable')
                continue
            done = re.search(r'\[plugin_(enable|disable):[0-9]+\] plugin ([a-z0-9]+) (enable|disable) success$', message)
            if done and pid in pending:
                user, plugin, enabled = pending[pid]
                if done[1] == done[3] and plugin == done[2] and enabled == (done[1] == 'enable'):
                    intents[user, plugin] = dict(enabled=enabled,
                        time=int(row['__REALTIME_TIMESTAMP']) / 1000000, cursor=row.get('__CURSOR', ''))
                pending.pop(pid, None)
        except (ValueError, TypeError, KeyError):
            continue
    return intents


def apply_native_intent(record, event):
    cutoff = record.get('intent_time', record['entry'].get('changetime', 0))
    if not event or event['time'] <= cutoff:
        return
    record['enabled'] = event['enabled']
    record['entry']['enable'] = event['enabled']
    record['entry']['status'] = 'running' if event['enabled'] else 'disable'
    record['intent_time'] = event['time']
    record['intent_event'] = event['cursor']
    # A successful explicit enable resumes managed services. A disable remains
    # disabled even if the registry and source are removed before the next poll.
    if record['plugin'] in MANAGED:
        record['running'] = event['enabled']
        record['runtime_intent_time'] = event['time']
        record['native_intent_time'] = event['time']


def observe_state(record, live, user, plugin, home, src):
    """Only accept a newer, usable registration as a changed user intent.

    Firmware boot rewrites enable without advancing changetime. Market refresh
    marks entries offline before uninstalling them. Neither is a user toggle.
    """
    if live.get('online') is False or live.get('status') in ('uninstalling', 'installing', 'upgrading', 'error'):
        return
    enabled = live.get('enable')
    if not isinstance(enabled, bool):
        return
    current = live.get('changetime', 0)
    previous = record['entry'].get('changetime', 0)
    newer = isinstance(current, (int, float)) and isinstance(previous, (int, float)) and current > previous
    if enabled != record['enabled'] and not newer:
        return
    changed = enabled != record['enabled']
    record['enabled'] = enabled
    record['entry']['enable'] = enabled
    if newer:
        record['entry']['status'] = live.get('status', 'stopped')
        record['entry']['changetime'] = current
        if changed:
            record['intent_time'] = time.time()
    if plugin in MANAGED:
        if changed and newer:
            # A usable registration with an advanced revision is an explicit
            # native toggle. A boot rewrite without a revision is not.
            record['running'] = enabled
            record['runtime_intent_time'] = time.time()
            record['native_intent_time'] = record['runtime_intent_time']
        elif plugin == 'taskcenter':
            # Scheduler enable is a persistent, explicit flag in the protected
            # task state, independent of the plugin UI's enable flag.
            record['running'] = control(user, plugin, home, src, 'status', False).returncode == 0
        elif not record.get('runtime_intent_time') and control(user, plugin, home, src, 'status', False).returncode == 0:
            record['running'] = True


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
        if running and pending:
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
    expected_program = hashlib.sha256((BASE / 'recovery.py').read_bytes()).hexdigest()
    try:
        previous_watch = read(RUNTIME / 'watch-status.json')
    except (OSError, ValueError):
        previous_watch = {}
    restart = (previous_watch.get('programSHA256') != expected_program and
               call(['systemctl', 'is-active', '--quiet', WATCH_UNIT], check=False).returncode == 0)
    service = '''[Unit]
Description=Restore local Xiaomi NAS plugins
After=local-fs.target network.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -B /data/.minas-plugin-recovery/recovery.py reconcile
TimeoutStartSec=600
UMask=0077
'''
    watcher = '''[Unit]
Description=Watch Xiaomi NAS plugin recovery events
After=local-fs.target network.target
[Service]
Type=simple
ExecStart=/usr/bin/python3 -B /data/.minas-plugin-recovery/recovery.py watch
Restart=on-failure
RestartSec=15
RuntimeDirectory=minas-plugin-recovery
RuntimeDirectoryMode=0700
UMask=0077
[Install]
WantedBy=multi-user.target
'''
    root = Path('/etc/systemd/system')
    for name, content in ((UNIT + '.service', service), (WATCH_UNIT, watcher)):
        path = root / name
        if not path.exists() or path.read_text() != content:
            atomic(path, content.encode(), 0o644)
    # Remove both old full-scan sources. Healthy Cron checks only systemd's
    # in-memory state; it never starts Python or reads plugin source/state.
    call(['systemctl', 'disable', '--now', UNIT + '.timer'], check=False)
    plain(root / (UNIT + '.timer')).unlink(missing_ok=True)
    cron = ('SHELL=/bin/sh\nPATH=/usr/sbin:/usr/bin:/sbin:/bin\nMAILTO=""\n'
            '* * * * * root /usr/bin/systemctl is-active --quiet ' + WATCH_UNIT +
            ' || /usr/bin/systemctl start ' + WATCH_UNIT +
            ' || /usr/bin/python3 -B /data/.minas-plugin-recovery/recovery.py bootstrap --quiet\n')
    cron_path = Path('/etc/cron.d') / UNIT
    if not cron_path.is_file() or cron_path.read_bytes() != cron.encode():
        atomic(cron_path, cron.encode(), 0o644)
    call(['systemctl', 'daemon-reload'])
    call(['systemctl', 'enable', '--now', WATCH_UNIT])
    if restart:
        call(['systemctl', 'restart', WATCH_UNIT])
    call(['systemctl', 'reload', 'crond.service'], check=False)


def snapshot(user, plugin, payload):
    home, src, owner, entry = normalize(user, plugin, payload)
    prepare_tmp(user, src, Path(os.readlink(home / 'tmp')), owner)
    if plugin in SERVERS:
        unit = Path('/etc/systemd/system') / daemon_unit(user, plugin)
        migrate_running = not unit.exists() and legacy_control(user, plugin, home, src, 'status', False).returncode == 0
        if migrate_running:
            legacy_control(user, plugin, home, src, 'stop')
        install_daemon(user, plugin)
        if migrate_running:
            control(user, plugin, home, src, 'start')
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
                      intent_time=time.time(),
                      runtime_intent_time=time.time(),
                      running=plugin in MANAGED and control(user, plugin, home, src, 'status', False).returncode == 0)
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


def restore_record(directory, intents=None, check_source=True):
    with record_lock(directory):
        return restore_locked_record(directory, intents, check_source)


def restore_locked_record(directory, intents=None, check_source=True):
    if (directory / 'removed').exists():
        return
    saved = load_generation(directory)
    record = read(saved / 'record.json')
    user, plugin = record['user'], record['plugin']
    home, owner = identity(user, plugin)
    require(directory.name == user + '-' + plugin, 'record identity mismatch')
    apply_native_intent(record, (native_intents() if intents is None else intents).get((user, plugin)))
    apply_service_intent(record, directory)
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
        source_ok = src.is_dir() and (not check_source or digest(src) == expected_info['abstract'])
        complete = ((src / 'ui/config').is_file() and (home / 'INFO').is_file()
                    and all((home / 'scripts' / p.name).is_file() for p in ((saved / 'scripts') if (saved / 'scripts').is_dir() else (saved / 'payload/scripts')).iterdir())
                    and all((home / name).is_symlink() for name in ('src', 'tmp', 'etc', 'var')) and source_ok)
        if complete and isinstance(live, dict) and live.get('install') is True and live.get('info', {}).get('version') == expected_info['version']:
            observe_state(record, live, user, plugin, home, src)
        apply_service_intent(record, directory)
        if read(saved / 'record.json') != record:
            save(saved / 'record.json', record)
        repair = not complete or not isinstance(live, dict) or live.get('install') is not True
        if (home / 'INFO').is_file():
            require(read(home / 'INFO')['version'] == expected_info['version'], 'different installed version; capture it explicitly')
        if src.is_dir() and not source_ok:
            restore_missing_source(src, saved / 'src', owner)
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
        # A tmp symlink can survive while its target is removed. Initialize the
        # parent and check the target even when the rest of the install is intact.
        tmp = Path(record['tmp'])
        if check_source or repair:
            prepare_tmp(user, src, tmp, owner)
        linked(home / 'tmp', tmp, owner)
        if plugin in SERVERS:
            install_daemon(user, plugin)
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
        if check_source or repair:
            verify(user, plugin, home, src)
    # Do not hold the list lock while lifecycle hooks update the list themselves.
    restore_component(saved, user, plugin)
    update = directory / 'service-update.json'
    if update.is_file() and read(update).get('until', 0) > time.time():
        return
    if plugin in SERVERS and not record['running'] and record.get('runtime_intent_time'):
        if control(user, plugin, home, src, 'status', False).returncode == 0:
            control(user, plugin, home, src, 'stop')
    if plugin in MANAGED and record['running']:
        if control(user, plugin, home, src, 'status', False).returncode:
            result = control(user, plugin, home, src, 'start', False)
            # A user-owned control may start successfully but cannot update the
            # firmware's root-owned list. Verify the service independently.
            deadline = time.monotonic() + 20
            while control(user, plugin, home, src, 'status', False).returncode:
                require(time.monotonic() < deadline,
                        'service did not recover: ' + plugin + ' (start exit ' + str(result.returncode) + ')')
                time.sleep(0.25)
    # Only resume the scheduler flag; never execute a task or an SSH recovery.
    if repair:
        print('已恢复：' + user + '/' + plugin, flush=True)


def reconcile(requests=None):
    failures = 0
    records = BASE / 'records'
    if not records.is_dir():
        return
    intents = native_intents()
    for directory in sorted(records.iterdir()):
        if not directory.is_dir():
            continue
        if requests is not None and directory.name not in requests:
            continue
        try:
            restore_record(directory, intents, True if requests is None else requests[directory.name])
        except Exception as error:
            failures += 1
            print(directory.name + ': ' + str(error), file=sys.stderr, flush=True)
    require(not failures, str(failures) + ' recovery record(s) need attention')


class PluginEvents:
    """Kernel notifications only. No periodic stat, scan, or cache access."""
    CHANGE = 0x00000008 | 0x00000080 | 0x00000100 | 0x00000200 | 0x00000040
    SELF = 0x00000400 | 0x00000800 | 0x00002000
    OVERFLOW = 0x00004000
    IGNORED = 0x00008000

    def __init__(self):
        self.lib = ctypes.CDLL(None, use_errno=True)
        self.lib.inotify_init1.argtypes = [ctypes.c_int]
        self.lib.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.fd = self.lib.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), 'inotify_init1')
        self.subscriptions = {}
        self.specs = {}

    def close(self):
        os.close(self.fd)

    def add(self, path, target, kind, names=None):
        # IN_ONLYDIR | IN_DONT_FOLLOW: never follow a user-created symlink.
        mask = self.CHANGE | self.SELF | 0x01000000 | 0x02000000 | 0x20000000
        wd = self.lib.inotify_add_watch(self.fd, os.fsencode(path), mask)
        if wd < 0:
            error = ctypes.get_errno()
            if error in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                return
            raise OSError(error, 'cannot watch ' + str(path))
        item = (target, kind, frozenset(names) if names is not None else None)
        if item not in self.subscriptions.setdefault(wd, []):
            self.subscriptions[wd].append(item)

    def configure(self):
        records = BASE / 'records'
        self.add(BASE, None, 'config', {'records'})
        self.add(records, None, 'config')
        for directory in sorted(records.iterdir()) if records.is_dir() else []:
            if not re.fullmatch(r'u[0-9]+-[a-z0-9]+', directory.name):
                continue
            self.add(directory, None, 'config', {'current', 'removed'})
            if (directory / 'removed').exists():
                continue
            record = read(load_generation(directory) / 'record.json')
            user, plugin = record['user'], record['plugin']
            require(directory.name == user + '-' + plugin, 'invalid watch record identity')
            try:
                home, _ = identity(user, plugin)
            except KeyError:
                continue
            src = Path(record['src'])
            require(re.fullmatch(r'/nas/pool[0-9]+/' + user + '/plugin/pluginsrc/' + plugin, str(src)),
                    'invalid watch source path')
            key = directory.name
            self.specs[key] = record
            self.add(directory, key, 'registry', {'service-intent.json'})
            self.add(Path('/data/plugin'), key, 'registry', {user + '.list'})
            self.add(Path('/home'), key, 'source', {user})
            self.add(home.parent.parent, key, 'source', {'plugin'})
            self.add(home.parent, key, 'source', {plugin})
            self.add(home, key, 'source', {'src', 'tmp', 'etc', 'var', 'INFO', 'scripts'})
            self.add(home / 'scripts', key, 'source')
            self.add(src.parent, key, 'source', {plugin})
            # Only source directories are recursive; never watch etc/var/tmp
            # contents, download progress, logs, PID files or user documents.
            if os.path.ismount(src.parents[3]):
                plain(src)
                for parent, dirs, _ in os.walk(src, followlinks=False):
                    dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink()]
                    self.add(Path(parent), key, 'source')
                self.add(Path(record['tmp']).parent, key, 'source', {plugin})
            self.add(Path('/data/plugin/www'), key, 'registry', {user})
            self.add(Path('/data/plugin/www') / user, key, 'registry', {plugin})
            self.add(Path('/data/plugin/www/icon'), key, 'registry', {plugin + '.icon'})

    def drain(self):
        requests, rebuild = {}, False
        while True:
            try:
                data = os.read(self.fd, 256 * 1024)
            except BlockingIOError:
                break
            if not data:
                break
            offset = 0
            while offset < len(data):
                wd, mask, _, size = struct.unpack_from('iIII', data, offset)
                name = os.fsdecode(data[offset + 16:offset + 16 + size].split(b'\0', 1)[0])
                offset += 16 + size
                if mask & self.OVERFLOW:
                    requests.update({key: True for key in self.specs})
                    rebuild = True
                    continue
                for target, kind, names in self.subscriptions.get(wd, []):
                    if name and names is not None and name not in names:
                        continue
                    if kind == 'config':
                        if names is None and name and not re.fullmatch(r'u[0-9]+-[a-z0-9]+', name):
                            continue
                        rebuild = True
                        requests.update({key: True for key in self.specs})
                    else:
                        requests[target] = requests.get(target, False) or kind == 'source'
                        rebuild = rebuild or kind == 'source'
        return requests, rebuild


def pool_mounts(content):
    """Ignore container/private mount churn; only storage pool mounts matter."""
    return frozenset(tuple(row.split()[:4]) for row in content.splitlines()
                     if len(row.split()) >= 4 and re.fullmatch(r'/nas/pool[0-9]+', row.split()[1]))


def native_operation_busy():
    # Original plugincenter does not hold our installer lock. Wait for its
    # command process to finish as well as for a quiet event interval.
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            if (process / 'comm').read_text().strip() == 'plugincenter':
                return True
        except OSError:
            pass
    return False


def event_reconcile(requests):
    if native_operation_busy():
        return False
    with open(plain(Path('/data/plugin/.local-plugin-installer.lock')), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        reconcile(requests)
    return True


def watch():
    protected(RUNTIME)
    # One watcher per NAS even if somebody invokes the command manually.
    with open(plain(RUNTIME / 'watch.lock'), 'a') as singleton:
        fcntl.flock(singleton, fcntl.LOCK_EX | fcntl.LOCK_NB)
        events = PluginEvents()
        events.configure()
        with open('/proc/self/mounts') as mounts:
            known_mounts = pool_mounts(mounts.read())
            poll = select.poll()
            poll.register(events.fd, select.POLLIN)
            poll.register(mounts.fileno(), select.POLLPRI | select.POLLERR)
            pending = {key: True for key in events.specs}
            uptime = float(Path('/proc/uptime').read_text().split()[0])
            ready_at = time.monotonic() + max(5, 90 - uptime)
            deadline = ready_at if pending else None
            first_event = time.monotonic()
            rebuild, failures = False, 0
            status = dict(version=VERSION, pid=os.getpid(), procPid=int(os.readlink('/proc/self')), checks=0, fullChecks=0,
                          watches=len(events.subscriptions), lastReason='startup', lastError='',
                          programSHA256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
            save(RUNTIME / 'watch-status.json', status)
            try:
                while True:
                    timeout = -1 if deadline is None else max(0, int((deadline - time.monotonic()) * 1000))
                    for fd, _ in poll.poll(timeout):
                        if fd == events.fd:
                            incoming, changed = events.drain()
                            if incoming or changed:
                                if not pending:
                                    first_event = time.monotonic()
                                for key, deep in incoming.items():
                                    pending[key] = pending.get(key, False) or deep
                                rebuild = rebuild or changed
                                deadline = max(ready_at, min(time.monotonic() + 5, first_event + 30))
                                status['lastReason'] = 'filesystem'
                        elif fd == mounts.fileno():
                            mounts.seek(0)
                            current = pool_mounts(mounts.read())
                            if current != known_mounts:
                                known_mounts = current
                                pending.update({key: True for key in events.specs})
                                rebuild = True
                                deadline = max(ready_at, time.monotonic() + 5)
                                status['lastReason'] = 'storage-mount'
                    if deadline is None or time.monotonic() < deadline:
                        continue
                    if rebuild:
                        # Arm new watches before repairing; events generated by
                        # the repair are queued and checked on the next pass.
                        previous_keys = set(events.specs)
                        replacement = PluginEvents()
                        try:
                            replacement.configure()
                        except Exception:
                            replacement.close()
                            raise
                        poll.unregister(events.fd)
                        events.close()
                        events = replacement
                        poll.register(events.fd, select.POLLIN)
                        for key in events.specs:
                            if key not in previous_keys:
                                pending[key] = True
                        rebuild = False
                    # An unmounted pool waits for the next mount notification,
                    # without repeatedly checking its missing files on disk.
                    pending = {key: deep for key, deep in pending.items() if key in events.specs
                               and os.path.ismount(Path(events.specs[key]['src']).parents[3])}
                    try:
                        if pending and not event_reconcile(pending):
                            deadline = time.monotonic() + 5
                            continue
                        if pending:
                            status['checks'] += 1
                            status['fullChecks'] += sum(pending.values())
                        status.update(lastError='', lastCheck=time.time(), watches=len(events.subscriptions))
                        failures = 0
                    except Exception as error:
                        failures += 1
                        status.update(lastError=str(error)[:500], lastCheck=time.time())
                        print('事件恢复失败：' + str(error), file=sys.stderr, flush=True)
                    save(RUNTIME / 'watch-status.json', status)
                    # Bounded retries only after a real failure. Conflicting
                    # source edits need a new event/manual check, not polling.
                    if failures and failures < 3:
                        deadline = time.monotonic() + 60 * failures
                    else:
                        pending = {}
                        deadline = None
            finally:
                events.close()


def forget(user, plugin):
    identity(user, plugin)
    directory = BASE / 'records' / (user + '-' + plugin)
    if directory.is_dir():
        atomic(directory / 'removed', b'explicit manage.sh uninstall\n')
    if plugin in SERVERS:
        unit = Path('/etc/systemd/system') / daemon_unit(user, plugin)
        if unit.is_file():
            call(['systemctl', 'stop', unit.name])
            unit.unlink()
            call(['systemctl', 'daemon-reload'])
        for path in (Path('/etc/sudoers.d') / ('minas-service-' + user + '-' + plugin),
                     Path('/etc/minas-plugin-services') / (user + '-' + plugin)):
            plain(path).unlink(missing_ok=True)
    port_registry(user, plugin, '', remove=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('capture', 'normalize', 'reconcile', 'bootstrap', 'forget', 'status', 'service', 'watch'))
    parser.add_argument('--user')
    parser.add_argument('--plugin', choices=sorted(PLUGINS))
    parser.add_argument('--package', '--payload', dest='payload', type=Path)
    parser.add_argument('--quiet', action='store_true')
    parser.add_argument('--operation', choices=('start', 'stop', 'restart', 'status', 'lifecycle-start', 'lifecycle-stop', 'update-start', 'update-stop', 'update-finish'))
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
        if args.action == 'watch':
            watch()
        elif args.action == 'service':
            require(args.user and args.plugin and args.operation, 'user/plugin/operation required')
            sys.exit(service_operation(args.user, args.plugin, args.operation))
        elif args.action in ('capture', 'normalize'):
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
