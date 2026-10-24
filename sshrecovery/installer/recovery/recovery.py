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
PLUGINS = {'mihomo', 'dockermanager', 'taskcenter', 'nascenter', 'sshrecovery', 'quickshare', 'downloadcenter'}
SERVERS = {'mihomo', 'quickshare', 'downloadcenter'}
MANAGED = SERVERS | {'taskcenter'}
UNIT = 'minas-plugin-recovery'
VERSION = '1.6.1'
WATCH_UNIT = UNIT + '-watch.service'
RUNTIME = Path('/run/minas-plugin-recovery')

# BEGIN MINAS DATA LAYOUT (generated)
"""Build-time authority; embedded in standalone packages and root scripts."""
DATA_LAYOUT_VERSION = 1
DATA_LAYOUT_ROOT = '/data/minas'
DATA_LAYOUT_PATHS = {
    '/data/.minas-privileged-backups': '/data/minas/backups/privileged',
    '/data/.sshrecovery-backups': '/data/minas/backups/plugins/sshrecovery',
    '/data/.taskcenter-backups': '/data/minas/backups/plugins/taskcenter',
    '/data/.minas-privileged': '/data/minas/privileged',
    '/data/.minas-plugin-data': '/data/minas/plugins/data',
    '/data/.minas-plugin-recovery': '/data/minas/plugins/recovery',
    '/data/ssh-autostart-fix': '/data/minas/ssh',
    '/data/minas-ssh-bootstrap': '/data/minas/backups/ssh-bootstrap',
    '/data/docker-bridge-repair': '/data/minas/docker/network',
    '/data/.minas-docker-runtime': '/data/minas/docker/runtime',
    '/data/docker-upgrade-backups': '/data/minas/backups/docker',
}


def _layout_secure(path):
    import os,stat
    from pathlib import Path
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts:raise ValueError('invalid layout path')
    for node in (path,*path.parents):
        s=node.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:
            raise ValueError('unsafe data layout directory: '+str(node))
    return path


def _layout_automatic_migration():
    """Run the packaged coordinator, never a program from a user directory."""
    import base64,gzip,json,os,shutil,subprocess,tempfile
    from pathlib import Path
    payload=globals().get('DATA_LAYOUT_AUTO_PAYLOAD')
    if not payload:raise ValueError('旧目录布局需要带自动迁移组件的新安装包')
    _layout_secure('/run')
    stage=Path(tempfile.mkdtemp(prefix='minas-auto-layout-',dir='/run'));unit=None
    try:
        stage.chmod(0o700)
        files=json.loads(gzip.decompress(base64.b64decode(payload)))
        if files.get('format')!=2:raise ValueError('unsupported migration asset format')
        for relative,content in files['files'].items():
            path=Path(relative)
            if path.is_absolute() or '..' in path.parts:raise ValueError('invalid migration asset')
            target=stage/path;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            data=content.replace('# MINAS_RESTORE_LAYOUT_HELPER\n',files['helper']).encode()
            # Hydrate the identical shared helper after decompression, avoiding
            # recursively embedding an archive inside its own build inputs.
            end=b'\n# END MINAS DATA LAYOUT'
            block=("\n\n# BEGIN MINAS AUTO LAYOUT PAYLOAD (generated)\nDATA_LAYOUT_AUTO_PAYLOAD = '"+
                   payload+"'\n# END MINAS AUTO LAYOUT PAYLOAD").encode()
            if end in data:data=data.replace(end,block+end)
            target.write_bytes(data);target.chmod(0o600)
        for worker,entry in (('docker-upgrade.py','upgrade-docker-29.9.2.sh'),('docker-network.py','fix-docker-bridge.sh')):
            program=(stage/'system'/worker).read_text()
            wrapper=('#!/bin/sh\nset -eu\numask 077\n'
                     'export PATH="${PATH:+$PATH:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/data/docker"\n'
                     'worker=$(mktemp /tmp/minas-runtime.XXXXXXXX)\ntrap \'rm -f -- "$worker"\' EXIT\n'
                     'cat > "$worker" <<\'MINAS_EMBEDDED_RUNTIME_PY\'\n'+program.rstrip()+
                     '\nMINAS_EMBEDDED_RUNTIME_PY\npython3 -B "$worker" "$@"\n')
            target=stage/'system'/entry;target.write_text(wrapper);target.chmod(0o600)
        print('检测到旧目录，自动迁移数据和服务入口到 /data/minas/……',flush=True)
        command=[os.sys.executable,'-B',str(stage/'migrate.py'),'--auto','--assets',str(stage)]
        if Path('/run/systemd/system').is_dir() and Path('/usr/bin/systemd-run').exists():
            # An independent host cgroup survives stopping the calling recovery
            # service and can run from an offline chroot without setns rights.
            unit='minas-data-layout-'+os.urandom(5).hex()
            command=['/usr/bin/systemd-run','--quiet','--wait','--collect',
                     '--unit='+unit,'--property=Type=oneshot',
                     '--property=TimeoutStartSec=infinity',*command[:-2],'--cleanup-assets',*command[-2:]]
        result=subprocess.run(command,
                              env=dict(os.environ,PATH=os.environ.get('PATH','')+':/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/data/docker'))
        if unit:
            subprocess.run(['/usr/bin/journalctl','--no-pager','-n','100','-o','cat','-u',unit+'.service'],check=False)
        if result.returncode:raise ValueError('自动目录迁移未完成，已停止后续安装；请查看上方迁移事务和错误')
    finally:
        # The independent coordinator owns cleanup once dispatched. Losing the
        # installer connection must not remove files underneath a live job.
        if unit is None:shutil.rmtree(stage)


def ensure_data_layout():
    """Migrate an existing layout before creating any new component state."""
    import json,os,tempfile
    from pathlib import Path
    if os.geteuid()!=0:raise ValueError('root required for data layout')
    old=[p for p in DATA_LAYOUT_PATHS if os.path.lexists(p)]
    pending=False
    current=Path(DATA_LAYOUT_ROOT)/'migration/current'
    if current.exists() and os.environ.get('MINAS_LAYOUT_COORDINATOR')!='1':
        import re,stat
        _layout_secure(current.parent)
        s=current.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('unsafe migration pointer')
        ident=current.read_text().strip()
        if not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[a-f0-9]{8}',ident):raise ValueError('invalid migration pointer')
        state=current.parent/'transactions'/ident/'state.json';_layout_secure(state.parent)
        s=state.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('unsafe migration state')
        pending=json.loads(state.read_text())['phase'] not in ('committed','rolled-back')
        if pending:
            import fcntl
            try:
                fd=os.open('/run/minas-auto-layout.lock',os.O_RDONLY|os.O_NOFOLLOW)
            except FileNotFoundError:pass
            else:
                try:
                    s=os.fstat(fd)
                    if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('unsafe layout lock')
                    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:pending=False
                finally:os.close(fd)
    if old or (pending and globals().get('DATA_LAYOUT_AUTO_PAYLOAD')):
        _layout_automatic_migration()
        if any(os.path.lexists(p) for p in DATA_LAYOUT_PATHS):raise ValueError('旧目录迁移未完成')
    root=Path(DATA_LAYOUT_ROOT)
    for path,mode in ((root,0o755),(root/'plugins',0o755),(root/'docker',0o755),(root/'backups',0o700),(root/'backups/plugins',0o700)):
        _layout_secure(path.parent)
        if not path.exists():path.mkdir(mode=mode);path.chmod(mode)
        _layout_secure(path)
        if path==root and not path.stat().st_mode&0o001:raise ValueError('layout root must allow user traversal')
    marker=root/'layout.json'
    if os.path.lexists(marker):
        s=marker.lstat()
        import stat
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('unsafe layout marker')
        value=json.loads(marker.read_text())
        if value.get('version')!=DATA_LAYOUT_VERSION or value.get('root')!=DATA_LAYOUT_ROOT:raise ValueError('unsupported data layout')
    else:
        fd,name=tempfile.mkstemp(prefix='.layout-',dir=root)
        try:
            with os.fdopen(fd,'w') as stream:
                json.dump(dict(version=DATA_LAYOUT_VERSION,root=DATA_LAYOUT_ROOT),stream)
                stream.write('\n');stream.flush();os.fchmod(stream.fileno(),0o644);os.fsync(stream.fileno())
            os.replace(name,marker)
            directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:
            if os.path.exists(name):os.unlink(name)


def remap_data_path(value):
    """Read-only legacy references: fixed roots, protected marker, no symlinks."""
    import json,os,stat
    from pathlib import Path
    path=Path(value)
    if not path.is_absolute() or '..' in path.parts:raise ValueError('invalid historical path')
    for old,new in DATA_LAYOUT_PATHS.items():
        if path==Path(old) or path.is_relative_to(old):
            marker=Path(DATA_LAYOUT_ROOT)/'layout.json'
            _layout_secure(marker.parent)
            s=marker.lstat()
            if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('unsafe layout marker')
            data=json.loads(marker.read_text())
            if data.get('version')!=DATA_LAYOUT_VERSION or data.get('root')!=DATA_LAYOUT_ROOT:raise ValueError('unsupported layout mapping')
            if os.path.lexists(old):raise ValueError('conflicting legacy data root: '+old)
            return Path(new)/path.relative_to(old)
    return path


def rebase_data_text(text):
    """Only for trusted historical service bodies, never for user data."""
    for old,new in DATA_LAYOUT_PATHS.items():text=text.replace(old,new)
    return text

# BEGIN MINAS AUTO LAYOUT PAYLOAD (generated)
DATA_LAYOUT_AUTO_PAYLOAD = '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'
# END MINAS AUTO LAYOUT PAYLOAD
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


def memory_download(home):
    try:return tuple(int(n) for n in json.loads((home/'INFO').read_text())['version'].split('.')) >= (1,1,21)
    except (OSError,ValueError,KeyError):return False


_MEMORY_RUNTIME = r'''
import os,re,sys,stat,socket,array,json,tempfile,ctypes
from pathlib import Path
home=Path(sys.argv[1]);sock=socket.socket(fileno=int(sys.argv[2]))
if home.is_symlink() or home.parent.is_symlink():raise ValueError('redirected plugin home')
link=home/'tmp'
if not link.is_symlink():raise ValueError('official cache link required')
base=link.resolve(strict=True)
match=re.fullmatch(r'/nas/(pool[0-9]+)/'+home.parts[2]+r'/plugin/plugintmp/downloadcenter',str(base))
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
if ctypes.c_long.from_buffer(buf).value!=0x01021994:
    # Pending bulk commands are tasks, not disposable memory state.
    old=directory/'job.json';target=home/'var/job.json'
    if old.is_symlink() or target.is_symlink():raise ValueError('redirected operation queue')
    if old.exists():
        with old.open('rb') as source:raw=source.read(4194305)
        if len(raw)>4194304 or not isinstance(json.loads(raw),dict):raise ValueError('invalid operation queue')
        if target.exists() and target.read_bytes()!=raw:raise ValueError('conflicting operation queues')
        if not target.exists():
            tmpf,tmp=tempfile.mkstemp(prefix='.job-',dir=target.parent)
            with os.fdopen(tmpf,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
            os.replace(tmp,target)
        old.unlink()
sock.sendmsg([b'runtime'],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[fd]))])
'''


def download_runtime(user, operation='prepare'):
    require(operation in ('prepare','release'), 'invalid runtime operation')
    home,owner=identity(user,'downloadcenter')
    require(owner[0]!=0, 'ordinary plugin user required')
    if os.readlink('/proc/self/ns/mnt')!=os.readlink('/proc/1/ns/mnt'):
        def host_namespace():
            fd=os.open('/proc/1/ns/mnt',os.O_RDONLY)
            try:require(ctypes.CDLL(None,use_errno=True).setns(fd,0)==0, 'host namespace unavailable')
            finally:os.close(fd)
        subprocess.run([sys.executable,'-B','-','runtime','--user',user,'--plugin','downloadcenter','--operation',operation],input=Path(__file__).read_bytes(),preexec_fn=host_namespace,check=True,timeout=30)
        return
    protected(RUNTIME)
    with open(plain(RUNTIME/'download-runtime.lock'),'a') as lock:
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
                options=('size=96m,mode=0700,uid=%d,gid=%d'%owner).encode()
                require(libc.mount(b'minas-download-runtime',target,b'tmpfs',2|4|8,options)==0,'cannot mount memory runtime')
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
        ('ExecStartPre=+/usr/bin/python3 -B '+str(BASE/'recovery.py')+' runtime --user '+user+' --plugin downloadcenter --operation prepare\n' if plugin=='downloadcenter' and memory_download(home) else '') +
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
        if plugin=='downloadcenter' and operation=='lifecycle-stop' and memory_download(identity(user,plugin)[0]):
            download_runtime(user,'release')
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
            ' || /usr/bin/python3 -B /data/minas/plugins/recovery/recovery.py bootstrap --quiet\n')
    cron_path = Path('/etc/cron.d') / UNIT
    if not cron_path.is_file() or cron_path.read_bytes() != cron.encode():
        atomic(cron_path, cron.encode(), 0o644)
    call(['systemctl', 'daemon-reload'])
    call(['systemctl', 'enable', '--now', WATCH_UNIT])
    if restart:
        call(['systemctl', 'restart', WATCH_UNIT])
    call(['systemctl', 'reload', 'crond.service'], check=False)


def snapshot(user, plugin, payload, retain_previous=False):
    home, src, owner, entry = normalize(user, plugin, payload)
    prepare_tmp(user, src, Path(os.readlink(home / 'tmp')), owner)
    if plugin in SERVERS:
        unit = Path('/etc/systemd/system') / daemon_unit(user, plugin)
        migrate_running = not unit.exists() and legacy_control(user, plugin, home, src, 'status', False).returncode == 0
        if migrate_running:
            legacy_control(user, plugin, home, src, 'stop')
        if plugin=='downloadcenter' and memory_download(home):
            persist_state(user,plugin,home,src,owner,entry.get('enable',True))
            protected(BASE)
            atomic(BASE/'recovery.py',Path(__file__).read_bytes(),0o700)
            download_runtime(user)
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
    if plugin=='downloadcenter' and memory_download(home):download_runtime(user)
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
            if old.name != generation and not retain_previous:
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
    required = (Path('/data/minas/privileged/minas-helper'),
                Path('/data/minas/privileged/registry.json'),
                Path('/etc/sudoers.d') / (plugin + '-' + user), Path('/etc/cron.d/minas-privileged'))
    manifest = read(common / 'manifest.json')
    code_present = all(plain(Path('/data/minas/privileged') / name).is_file() for name in manifest['files'])
    if code_present and all(plain(path).is_file() for path in required):
        grants = read(Path('/data/minas/privileged/registry.json')).get('users', {}).get(user, {})
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
            if plugin=='downloadcenter' and memory_download(home):download_runtime(user)
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
            if plugin=='downloadcenter' and memory_download(Path('/home')/user/'plugin'/plugin):download_runtime(user,'release')
            unit.unlink()
            call(['systemctl', 'daemon-reload'])
        for path in (Path('/etc/sudoers.d') / ('minas-service-' + user + '-' + plugin),
                     Path('/etc/minas-plugin-services') / (user + '-' + plugin)):
            plain(path).unlink(missing_ok=True)
    port_registry(user, plugin, '', remove=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('capture', 'normalize', 'reconcile', 'bootstrap', 'forget', 'status', 'service', 'watch','runtime'))
    parser.add_argument('--user')
    parser.add_argument('--plugin', choices=sorted(PLUGINS))
    parser.add_argument('--package', '--payload', dest='payload', type=Path)
    parser.add_argument('--quiet', action='store_true')
    parser.add_argument('--operation', choices=('start', 'stop', 'restart', 'status', 'lifecycle-start', 'lifecycle-stop', 'update-start', 'update-stop', 'update-finish','prepare','release'))
    args = parser.parse_args()
    require(os.geteuid() == 0, 'root required')
    if args.action=='status':
        directory=BASE if BASE.exists() else Path('/data/.minas-plugin-recovery')
        if not directory.exists():return
        _layout_secure(directory)
        records=directory/'records'
        for item in sorted(records.iterdir()) if records.exists() else []:
            print(item.name+(' removed' if (item/'removed').exists() else ' registered'))
        return
    if str(BASE)=='/data/minas/plugins/recovery':ensure_data_layout()
    if Path('/data/minas/migration/maintenance').exists() and args.action in ('watch','reconcile','bootstrap','service'):
        raise RuntimeError('目录迁移正在进行，请稍后重试')
    protected(BASE)
    # Install/uninstall already owns this lock; their child coordinator must not
    # reacquire it. Timer/Cron use a nonblocking lock and retry on the next tick.
    with contextlib.ExitStack() as stack:
        if args.action=='runtime':
            require(args.plugin=='downloadcenter' and args.user and args.operation in ('prepare','release'),'invalid runtime identity')
            download_runtime(args.user,args.operation)
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
