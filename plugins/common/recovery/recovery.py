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
import socket
import array

sys.dont_write_bytecode = True
BASE = Path('/data/minas/plugins/recovery')
DATA = Path('/data/minas/plugins/data')
PLUGINS = {'mihomo', 'dockermanager', 'taskcenter', 'nascenter', 'sshrecovery', 'quickshare', 'downloadcenter', 'mediaeditor'}
SERVERS = {'mihomo', 'quickshare', 'downloadcenter'}
MANAGED = SERVERS | {'taskcenter'}
UNIT = 'minas-plugin-recovery-v2'
VERSION = '2.0.0'
WATCH_UNIT = UNIT + '-watch.service'
RUNTIME = Path('/run/minas-plugin-recovery-v2')

# BEGIN MINAS DATA LAYOUT (generated)
"""Small standalone layout primitives; generated constants select one owner."""
DATA_LAYOUT_VERSION = 1
DATA_LAYOUT_ROOT = '/data/minas'


def _layout_secure(path):
    import os,stat
    from pathlib import Path
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts:raise ValueError('invalid layout path')
    for node in (path,*path.parents):
        s=node.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:
            raise ValueError('unsafe layout directory: '+str(node))
    return path


def _layout_atomic(path,data,mode=0o600):
    import os,tempfile
    from pathlib import Path
    path=Path(path);_layout_secure(path.parent)
    fd,name=tempfile.mkstemp(prefix='.layout-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data if isinstance(data,bytes) else data.encode())
            stream.flush();os.fchmod(stream.fileno(),mode);os.fsync(stream.fileno())
        os.replace(name,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)


def _layout_initialize():
    import json,os,stat
    from pathlib import Path
    if os.geteuid()!=0:raise ValueError('root required for layout')
    root=Path(DATA_LAYOUT_ROOT);_layout_secure(root.parent)
    if not root.exists():
        root.mkdir(mode=0o755);root.chmod(0o755)
    _layout_secure(root)
    if not root.stat().st_mode&0o001:raise ValueError('layout root must allow traversal')
    marker=root/'layout.json'
    if os.path.lexists(marker):
        s=marker.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('unsafe layout marker')
        value=json.loads(marker.read_text())
        if value.get('version')!=DATA_LAYOUT_VERSION or value.get('root')!=DATA_LAYOUT_ROOT:raise ValueError('unsupported layout')
    else:_layout_atomic(marker,json.dumps(dict(version=DATA_LAYOUT_VERSION,root=DATA_LAYOUT_ROOT))+'\n',0o644)


def ensure_data_layout():
    """Initialize only the current layout; never inspect or move former paths."""
    _layout_initialize()


def remap_data_path(value):
    from pathlib import Path
    path=Path(value)
    if not path.is_absolute() or '..' in path.parts:raise ValueError('invalid state path')
    return path


def rebase_data_text(text):
    return text
# END MINAS DATA LAYOUT

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
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
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
    def failed(error):
        raise error
    for parent, dirs, names in os.walk(src, followlinks=False, onerror=failed):
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


def memory_download(home):
    minimum={'downloadcenter':(1,1,21),'quickshare':(1,1,73),'mihomo':(1,7,74)}
    try:return home.name in minimum and tuple(int(n) for n in json.loads((home/'INFO').read_text())['version'].split('.')) >= minimum[home.name]
    except (OSError,ValueError,KeyError):return False


_MEMORY_RUNTIME = r'''
import os,re,sys,stat,socket,array,json,tempfile,ctypes
from pathlib import Path
home=Path(sys.argv[1]);sock=socket.socket(fileno=int(sys.argv[2]))
if home.is_symlink() or home.parent.is_symlink():raise ValueError('redirected plugin home')
link=home/'tmp'
if not link.is_symlink():raise ValueError('official cache link required')
base=link.resolve(strict=True)
match=re.fullmatch(r'/nas/(pool[0-9]+)/'+home.parts[2]+r'/plugin/plugintmp/'+home.name,str(base))
if not match or not os.path.ismount('/nas/'+match[1]):raise ValueError('pool unavailable')
if base.stat().st_uid!=os.geteuid():raise ValueError('cache owner mismatch')
directory=base/'runtime'
if directory.is_symlink():raise ValueError('runtime symlink')
directory.mkdir(mode=0o700,exist_ok=True)
s=directory.stat()
if not stat.S_ISDIR(s.st_mode) or s.st_uid!=os.geteuid() or s.st_mode&0o077:raise ValueError('unsafe runtime')
fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
buf=ctypes.create_string_buffer(256)
if ctypes.CDLL(None).fstatfs(fd,buf):raise OSError('fstatfs failed')
sock.sendmsg([b'runtime'],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[fd]))])
'''


def download_runtime(user, operation='prepare', plugin='downloadcenter'):
    require(operation in ('prepare','release'), 'invalid runtime operation')
    require(plugin in SERVERS, 'invalid runtime plugin')
    home,owner=identity(user,plugin)
    require(owner[0]!=0, 'ordinary plugin user required')
    if os.readlink('/proc/self/ns/mnt')!=os.readlink('/proc/1/ns/mnt'):
        def host_namespace():
            fd=os.open('/proc/1/ns/mnt',os.O_RDONLY)
            try:require(ctypes.CDLL(None,use_errno=True).setns(fd,0)==0, 'host namespace unavailable')
            finally:os.close(fd)
        subprocess.run([sys.executable,'-B','-','runtime','--user',user,'--plugin',plugin,'--operation',operation],input=Path(__file__).read_bytes(),preexec_fn=host_namespace,check=True,timeout=30)
        return
    protected(RUNTIME)
    if plugin == 'downloadcenter' and operation == 'prepare':
        # qB samples its default destination every 30s even without torrents.
        # All managed jobs supply persistent destinations. This empty, root-owned
        # RAM directory rejects accidental implicit downloads; it stores no data.
        guard=Path('/run/minas-download-default')
        guard.mkdir(mode=0o555,exist_ok=True)
        meta=guard.lstat()
        require(stat.S_ISDIR(meta.st_mode) and meta.st_uid==0 and not meta.st_mode&0o222,'unsafe download default guard')
        guard.chmod(0o555)
        libc=ctypes.CDLL(None,use_errno=True);buf=ctypes.create_string_buffer(256)
        require(libc.statfs(os.fsencode(guard),buf)==0 and ctypes.c_long.from_buffer(buf).value==0x01021994,'download default guard requires RAM filesystem')
    with open(plain(RUNTIME/(plugin+'-runtime.lock')),'a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        parent,child=socket.socketpair();parent.settimeout(10);process=None;fd=None
        try:
            def demote():os.initgroups(user,owner[1]);os.setgid(owner[1]);os.setuid(owner[0])
            process=subprocess.Popen([sys.executable,'-I','-B','-c',_MEMORY_RUNTIME,str(home),str(child.fileno())],pass_fds=(child.fileno(),),preexec_fn=demote,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            child.close()
            _,anc,_,_=parent.recvmsg(32,socket.CMSG_SPACE(array.array('i').itemsize))
            for level,kind,data in anc:
                if level==socket.SOL_SOCKET and kind==socket.SCM_RIGHTS:
                    fds=array.array('i');fds.frombytes(data[:len(data)//fds.itemsize*fds.itemsize]);fd=fds[0]
            _,error=process.communicate(timeout=10)
            require(process.returncode==0 and fd is not None,'runtime validation failed: '+error.decode(errors='replace')[-500:])
            meta=os.fstat(fd);require(stat.S_ISDIR(meta.st_mode) and meta.st_uid==owner[0] and not meta.st_mode&0o077,'unsafe runtime descriptor')
            libc=ctypes.CDLL(None,use_errno=True);buf=ctypes.create_string_buffer(256)
            require(libc.fstatfs(fd,buf)==0,'fstatfs failed');mounted=ctypes.c_long.from_buffer(buf).value==0x01021994
            target=('/proc/self/fd/'+str(fd)).encode()
            if operation=='release':
                require(not mounted or libc.umount2(target,2)==0,'cannot release runtime')
            elif not mounted:
                size=96 if plugin=='downloadcenter' else 24
                options=('size=%dm,mode=0700,uid=%d,gid=%d'%(size,*owner)).encode()
                require(libc.mount(('minas-'+plugin+'-runtime').encode(),target,b'tmpfs',2|4|8,options)==0,'cannot mount memory runtime')
        finally:
            parent.close();child.close()
            if fd is not None:os.close(fd)
            if process is not None and process.poll() is None:process.kill();process.wait()


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
        'WorkingDirectory=' + str(home) + '\nPIDFile=' + str(home / 'tmp/runtime' / pidfile) + '\n' +
        ('ExecStartPre=+/usr/bin/python3 -B '+str(BASE/'recovery.py')+' runtime --user '+user+' --plugin '+plugin+' --operation prepare\n' if memory_download(home) else '') +
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
    require(not Path('/data/minas/plugins/manager/maintenance.json').exists(), '插件正在维护，请稍后操作')
    with record_lock(directory):
        require(not Path('/data/minas/plugins/manager/maintenance.json').exists() and not (directory / 'removed').exists(),
                '插件正在维护或已卸载')
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
        if operation=='lifecycle-stop' and memory_download(identity(user,plugin)[0]):
            download_runtime(user,'release',plugin)
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
    pending, intents, lifecycle = {}, {}, set()
    for line in output.splitlines():
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            message, pid = row.get('MESSAGE', ''), row.get('SYSLOG_PID', row.get('_PID'))
            # Firmware shutdown/unmount runs do_disable for every plugin in a
            # pool, with the same per-plugin success lines as a user toggle.
            # The leading batch marker identifies that process as lifecycle.
            # Keep earlier genuine toggles and ignore every member of the batch.
            if re.search(r'\[do_(?:enable|disable):[0-9]+\] start to (?:disbale|disable|enable) .* plugin in pool ', message):
                lifecycle.add(pid)
                pending.pop(pid, None)
                continue
            if pid is None or pid in lifecycle:
                continue
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
    """Install directly into the current persistent layout; never move old data."""
    protected(DATA,0o711);protected(DATA/user,0o711)
    destination=DATA/user/plugin;protected(destination,0o711)
    for name in ('etc','var'):
        target=destination/name;current=home/name
        if current.is_symlink():
            require(os.readlink(current)==str(target),'unmanaged state symlink')
        elif current.exists():
            plain(current)
            require(current.is_dir() and not any(current.iterdir()),'非空本地状态目录不自动搬移，请先用清理工具检查')
            current.rmdir()
        if not target.exists():
            target.mkdir(mode=0o700);os.chown(target,*owner)
        plain(target);require(target.is_dir() and target.stat().st_uid==owner[0],'invalid persistent owner')
        target.chmod(0o700);linked(current,target,owner)


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
            save(path, data)


class VerificationError(ValueError):
    """Credential-free, structured failure shared by installer/update/recovery."""
    def __init__(self, diagnostic):
        self.diagnostic = diagnostic
        super().__init__('原厂校验失败：' + json.dumps(diagnostic, ensure_ascii=False, sort_keys=True))


VERIFY_LOCALE = {'LC_ALL': 'C', 'LC_COLLATE': 'C', 'LANG': 'C'}
VERIFY_DIAGNOSTIC = r'''
import hashlib,json,os,stat,sys
from pathlib import Path
home,src,tmp=map(Path,sys.argv[1:4])
result=dict(homeReadable=os.access(home/'INFO',os.R_OK),
            sourceAccessible=os.access(src,os.R_OK|os.X_OK),temporaryWritable=os.access(tmp,os.W_OK|os.X_OK))
try:
    expected=json.loads((home/'INFO').read_bytes()).get('abstract')
    paths=[]
    def failed(error):raise error
    for parent,dirs,names in os.walk(src,followlinks=False,onerror=failed):
        for name in names:
            path=Path(parent)/name
            if stat.S_ISREG(path.lstat().st_mode):paths.append(path)
    digest=hashlib.sha256()
    for path in sorted(paths,key=lambda p:os.fsencode(str(p))):
        part=hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):part.update(block)
        digest.update((part.hexdigest()+'\n').encode())
    # Firmware uses shell word splitting and glob expansion, not a safe read
    # loop. Report names it cannot verify, without printing source contents.
    unsupported=sum(any(ch in str(p) for ch in ' \t\r\n*?[') for p in paths)
    result.update(sourceFiles=len(paths),unsupportedFileNames=unsupported,
                  expectedDigest=expected if isinstance(expected,str) and len(expected)==64 and all(c in '0123456789abcdef' for c in expected) else None,
                  calculatedDigest=digest.hexdigest(),digestMatches=digest.hexdigest()==expected)
except OSError as error:result.update(diagnosticError=type(error).__name__,errno=error.errno)
except (ValueError,TypeError):result['diagnosticError']='invalid INFO'
print(json.dumps(result))
'''


def verification_environment(user, plugin, home, src, temporary=None):
    environment = env(user, plugin, home, src)
    environment.update(VERIFY_LOCALE, PATH='/usr/sbin:/usr/bin:/sbin:/bin')
    if temporary is not None:
        environment['PLUG_TMP_DIR'] = str(temporary)
    return environment


def verify(user, plugin, home, src, temporary=None):
    environment = verification_environment(user, plugin, home, src, temporary)
    # Set the locale after runuser/PAM too. Never change the login environment,
    # use PLUG_STATUS=installing to rewrite INFO, or replace the factory check.
    command = ['runuser', '-u', user, '--', '/usr/bin/env',
               *[key+'='+value for key,value in VERIFY_LOCALE.items()], '/usr/bin/plugin.sh', 'verify']
    failure = None
    try:
        result = call(command, environment=environment, check=False)
        if result.returncode == 0:
            return
        code, stdout, stderr = result.returncode, result.stdout, result.stderr
    except (OSError, subprocess.TimeoutExpired) as error:
        code, stdout, stderr = None, b'', b''
        failure = type(error).__name__
    diagnostic = dict(plugin=plugin, user=user, verifier='/usr/bin/plugin.sh',
                      exitCode=code, effectiveLocale=VERIFY_LOCALE.copy(),
                      inheritedLocale={key:os.environ.get(key,'')[:120] for key in VERIFY_LOCALE},
                      stdoutBytes=len(stdout or b''), stderrBytes=len(stderr or b''))
    if failure:
        diagnostic['executionError'] = failure
    try:
        probe = call(['runuser','-u',user,'--','/usr/bin/python3','-I','-B','-c',VERIFY_DIAGNOSTIC,
                      home,src,environment['PLUG_TMP_DIR']], environment=environment,check=False)
        if probe.returncode == 0:
            value = json.loads(probe.stdout)
            if isinstance(value,dict):diagnostic.update(value)
        else:diagnostic['diagnosticExitCode'] = probe.returncode
    except (OSError,ValueError,subprocess.TimeoutExpired) as error:
        diagnostic['diagnosticError'] = type(error).__name__
    if not diagnostic.get('homeReadable',True) or not diagnostic.get('sourceAccessible',True):
        reason = '插件用户无法访问 INFO 或源码目录'
    elif not diagnostic.get('temporaryWritable',True):reason = '插件用户不能写入原厂校验临时目录'
    elif diagnostic.get('diagnosticError'):reason = '无法完整读取源码或 INFO，未将不完整扫描当作有效摘要'
    elif diagnostic.get('unsupportedFileNames'):reason = '源码文件名包含原厂校验器不支持的空白或通配字符'
    elif diagnostic.get('digestMatches') is False:reason = '源码摘要与 INFO 不一致'
    elif diagnostic.get('sourceFiles') == 0:reason = '源码目录没有普通文件'
    else:reason = '独立摘要检查不能代替原厂结果；请查看 journalctl -t plugin.sh 中的 verify 记录'
    diagnostic['reason'] = reason
    # Raw subprocess output may include user-controlled names. Expose only
    # selected metadata; both CLI and updater retain this structured detail.
    raise VerificationError(diagnostic)


def install_units(start=True):
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
ExecStart=/usr/bin/python3 -B /data/minas/plugins/recovery/recovery.py reconcile
TimeoutStartSec=600
UMask=0077
'''
    watcher = '''[Unit]
Description=Watch Xiaomi NAS plugin recovery events
After=local-fs.target network.target
[Service]
Type=simple
ExecStart=/usr/bin/python3 -B /data/minas/plugins/recovery/recovery.py watch
Restart=on-failure
RestartSec=15
RuntimeDirectory=minas-plugin-recovery-v2
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
            ' || /usr/bin/python3 -B /data/minas/plugins/recovery/recovery.py bootstrap --quiet\n')
    cron_path = Path('/etc/cron.d') / UNIT
    if not cron_path.is_file() or cron_path.read_bytes() != cron.encode():
        atomic(cron_path, cron.encode(), 0o644)
    call(['systemctl', 'daemon-reload'])
    call(['systemctl', 'enable', *(['--now'] if start else []), WATCH_UNIT])
    if restart and start:
        call(['systemctl', 'restart', WATCH_UNIT])
    call(['systemctl', 'reload', 'crond.service'], check=False)


def snapshot(user, plugin, payload, retain_previous=False, running_intent=None):
    """Save only an already provisioned, verified installation."""
    home, owner = identity(user, plugin)
    src = source_path(home, user, plugin)
    entry = read(Path('/data/plugin') / (user + '.list'))[plugin]
    verify(user, plugin, home, src)
    updater = payload / 'installer/updater'
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
        if updater.is_dir():
            (stage / 'installer').mkdir(exist_ok=True)
            shutil.copytree(updater, stage / 'installer/updater')
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
                      running=bool(running_intent) if running_intent is not None else plugin in MANAGED and control(user, plugin, home, src, 'status', False).returncode == 0)
        atomic(stage / 'record.json', encoded(record))
        for copied in stage.rglob('*'):
            if copied.is_file() and not copied.is_symlink():
                with copied.open('rb') as stream:
                    os.fsync(stream.fileno())
        for folder in [*sorted((p for p in stage.rglob('*') if p.is_dir() and not p.is_symlink()),
                               key=lambda p: len(p.parts), reverse=True), stage]:
            fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        # A committed generation is selected by a same-directory atomic pointer.
        generation = 'generation-' + str(time.time_ns())
        os.replace(stage, directory / generation)
        atomic(directory / 'current', generation.encode())
        for old in directory.glob('generation-*'):
            if old.name != generation and not retain_previous:
                shutil.rmtree(old)
        (directory / 'removed').unlink(missing_ok=True)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    print('恢复登记完成：' + user + '/' + plugin)


def load_generation(directory):
    plain(directory)
    generation = (directory / 'current').read_text()
    require(re.fullmatch(r'generation-[0-9]+', generation), 'invalid recovery generation')
    return plain(directory / generation)






def manager_module():
    import importlib.util
    root = Path('/data/minas/plugins/manager')
    require((root / 'manager.py').is_file(), '统一安装引擎缺失，请运行统一安装器修复')
    sys.path.insert(0, str(root))
    import manager
    return manager


def restore_record(directory, intents=None, check_source=True):
    if (directory / 'removed').exists():
        return
    with record_lock(directory):
        return manager_module().repair_record(directory, intents, check_source)




def reconcile(requests=None):
    manager_module().recover_pending(already_locked=True)
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
    if Path('/data/minas/plugins/manager/maintenance.json').exists() or native_operation_busy():
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
    plain(Path('/etc/sudoers.d') / ('minas-update-' + user + '-' + plugin)).unlink(missing_ok=True)
    directory = BASE / 'records' / (user + '-' + plugin)
    if directory.is_dir():
        atomic(directory / 'removed', b'explicit manage.sh uninstall\n')
    if plugin in SERVERS:
        unit = Path('/etc/systemd/system') / daemon_unit(user, plugin)
        if unit.is_file():
            call(['systemctl', 'stop', unit.name])
            if memory_download(Path('/home')/user/'plugin'/plugin):download_runtime(user,'release',plugin)
            unit.unlink()
            call(['systemctl', 'daemon-reload'])
        for path in (Path('/etc/sudoers.d') / ('minas-service-' + user + '-' + plugin),
                     Path('/etc/minas-plugin-services') / (user + '-' + plugin)):
            plain(path).unlink(missing_ok=True)
    port_registry(user, plugin, '', remove=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('capture', 'normalize', 'reconcile', 'bootstrap', 'forget', 'status', 'service', 'watch','runtime','verify'))
    parser.add_argument('--user')
    parser.add_argument('--plugin', choices=sorted(PLUGINS))
    parser.add_argument('--package', '--payload', dest='payload', type=Path)
    parser.add_argument('--quiet', action='store_true')
    parser.add_argument('--operation', choices=('start', 'stop', 'restart', 'status', 'lifecycle-start', 'lifecycle-stop', 'update-start', 'update-stop', 'update-finish','prepare','release'))
    args = parser.parse_args()
    require(os.geteuid() == 0, 'root required')
    if args.action=='verify':
        require(args.user and args.plugin,'user/plugin required for verification')
        home, _ = identity(args.user,args.plugin)
        verify(args.user,args.plugin,home,source_path(home,args.user,args.plugin))
        return
    if args.action=='status':
        directory=BASE
        if not directory.exists():return
        _layout_secure(directory)
        records=directory/'records'
        for item in sorted(records.iterdir()) if records.exists() else []:
            print(item.name+(' removed' if (item/'removed').exists() else ' registered'))
        return
    if (Path('/run/minas-plugin-maintenance').exists() or Path('/data/minas/plugins/manager/maintenance.json').exists()) and args.action in ('watch','reconcile','bootstrap'):
        return
    if str(BASE)=='/data/minas/plugins/recovery':ensure_data_layout()
    protected(BASE)
    # Install/uninstall already owns this lock; their child coordinator must not
    # reacquire it. Timer/Cron use a nonblocking lock and retry on the next tick.
    with contextlib.ExitStack() as stack:
        if args.action=='runtime':
            require(args.plugin in SERVERS and args.user and args.operation in ('prepare','release'),'invalid runtime identity')
            download_runtime(args.user,args.operation,args.plugin)
            return
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
