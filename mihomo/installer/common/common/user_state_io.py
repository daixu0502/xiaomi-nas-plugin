"""Read/write disposable plugin state as the NAS user, never as root.

Only fixed plugin names and basenames are accepted. Configuration that drives
privileged actions, execution code, authorization and secrets must not use this
store: its owner can edit it. No network or sudo entry point is provided here.
"""
import contextlib
import json
import os
from pathlib import Path
import pwd
import re
import select
import subprocess
import sys
import array
import ctypes
import fcntl
import socket
import stat

HOME_ROOT = Path('/home')
PLUGINS = {'dockermanager', 'nascenter', 'mihomo', 'taskcenter'}
MAX_BYTES = 4 * 1024 * 1024


def _host_mount_namespace():
    fd=os.open('/proc/1/ns/mnt',os.O_RDONLY)
    try:
        if ctypes.CDLL(None,use_errno=True).setns(fd,0):raise OSError(ctypes.get_errno(),'cannot enter host mount namespace')
    finally:os.close(fd)

_PROGRAM = r'''
import fcntl,json,os,stat,sys,tempfile
from pathlib import Path
home,operation,name=sys.argv[1:4]
home=Path(home)
for node in (home.parent,home):
    if node.is_symlink() or not node.is_dir():raise ValueError('plugin directory unavailable')
link=home/'tmp'
if not link.is_symlink():raise ValueError('official cache link unavailable')
resolved=link.resolve(strict=True)
import re
match=re.fullmatch(r'/nas/(pool[^/]+)/'+home.parts[2]+r'/plugin/plugintmp/'+home.name,str(resolved))
if not match or not os.path.ismount('/nas/'+match[1]):raise ValueError('data pool unavailable')
if resolved.stat().st_uid!=os.geteuid():raise ValueError('cache owner mismatch')
directory=resolved/'cache'
if directory.is_symlink():raise ValueError('state directory is a symlink')
directory.mkdir(mode=0o700,exist_ok=True)
if not directory.is_dir():raise ValueError('state directory unavailable')
if directory.stat().st_uid!=os.geteuid() or directory.stat().st_mode&0o077:raise ValueError('unsafe cache permissions')

if home.name in ('taskcenter','nascenter'):
    import ctypes
    fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    if operation=='prepare':
        import socket,array
        sock=socket.socket(fileno=int(sys.argv[4]))
        sock.sendmsg([b'cache'],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[fd]))])
        sys.exit(0)
    buf=ctypes.create_string_buffer(256)
    if ctypes.CDLL(None,use_errno=True).fstatfs(fd,buf)!=0 or ctypes.c_long.from_buffer(buf).value!=0x01021994:
        raise ValueError('memory cache unavailable; refusing disk fallback')
    os.close(fd)
target=directory/name
if target.is_symlink():raise ValueError('state file is a symlink')
if operation=='lock':
    fd=os.open(target,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):raise ValueError('invalid lock')
    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:print('busy',flush=True);sys.exit(2)
    print('locked',flush=True)
    sys.stdin.buffer.read()
elif operation=='delete':
    target.unlink(missing_ok=True)
    print('null')
elif operation=='read':
    try:
        fd=os.open(target,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            metadata=os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size>4194304:raise ValueError('invalid state file')
            data=json.load(stream)
        print(json.dumps(data if isinstance(data,dict) else None))
    except (FileNotFoundError,ValueError):print('null')
elif operation=='write':
    raw=sys.stdin.buffer.read(4194305)
    if len(raw)>4194304 or not isinstance(json.loads(raw),dict):raise ValueError('invalid state')
    fd,temp=tempfile.mkstemp(prefix='.state-',dir=directory)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,target)
    finally:
        if os.path.exists(temp):os.unlink(temp)
    print('null')
else:raise ValueError('unknown operation')
'''


def _arguments(user, plugin, name, operation):
    if not re.fullmatch(r'u[0-9]+', user or '') or plugin not in PLUGINS:
        raise ValueError('无效插件状态身份')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}', name):
        raise ValueError('无效状态文件名')
    account = pwd.getpwnam(user)
    if account.pw_uid==0:raise ValueError('插件运行状态必须属于普通 NAS 用户')
    def demote():
        if os.geteuid() == 0:
            if plugin in ('taskcenter','nascenter') and os.readlink('/proc/self/ns/mnt')!=os.readlink('/proc/1/ns/mnt'):
                _host_mount_namespace()
            os.initgroups(account.pw_name, account.pw_gid)
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)
        elif os.geteuid() != account.pw_uid:
            raise PermissionError('禁止访问其他用户的插件状态')
    if plugin in ('taskcenter','nascenter') and operation!='prepare' and os.geteuid()==0:
        _memory_cache(user,plugin,account)
    return dict(args=[sys.executable, '-I', '-c', _PROGRAM,
                      str(HOME_ROOT / user / 'plugin' / plugin), operation, name],
                cwd='/', env={'PATH':'/usr/bin:/bin','LC_ALL':'C'},
                preexec_fn=demote, close_fds=True)


def _memory_cache(user,plugin,account,release=False):
    # SSH and CGI may have private mount namespaces. Publish one host-wide
    # layer so every request and Cron observes the same bounded memory cache.
    if os.readlink('/proc/self/ns/mnt')!=os.readlink('/proc/1/ns/mnt'):
        code="import sys,pwd;sys.path.insert(0,sys.argv[1]);import user_state_io as m;m._host_mount_namespace();m._memory_cache(sys.argv[2],sys.argv[3],pwd.getpwnam(sys.argv[2]),sys.argv[4]=='1')"
        subprocess.run([sys.executable,'-I','-B','-c',code,str(Path(__file__).resolve().parent),user,plugin,'1' if release else '0'],check=True,capture_output=True,timeout=15)
        return
    fd=os.open('/run/minas-plugin-memory-cache.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'a') as lock:
        if os.fstat(fd).st_uid!=0:raise ValueError('unsafe cache mount lock')
        fcntl.flock(lock,fcntl.LOCK_EX)
        _mount_memory_cache(user,plugin,account,release)


def release_memory_cache(user,plugin):
    if os.geteuid()!=0 or plugin not in ('taskcenter','nascenter') or not re.fullmatch(r'u[0-9]+',user):
        raise ValueError('invalid memory cache removal')
    if (HOME_ROOT/user/'plugin'/plugin).is_dir():
        _memory_cache(user,plugin,pwd.getpwnam(user),release=True)


def _mount_memory_cache(user,plugin,account,release=False):
    # The unprivileged worker opens the validated official cache directory.
    # SCM_RIGHTS pins that inode across renames; root never traverses user cache.
    parent,child=socket.socketpair()
    parent.settimeout(8)
    process=None
    fd=None
    try:
        args=_arguments(user,plugin,'memory','prepare')
        args['args'].append(str(child.fileno()))
        process=subprocess.Popen(**args,pass_fds=(child.fileno(),),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        child.close()
        _,ancillary,_,_=parent.recvmsg(16,socket.CMSG_SPACE(array.array('i').itemsize))
        for level,kind,data in ancillary:
            if level==socket.SOL_SOCKET and kind==socket.SCM_RIGHTS:
                values=array.array('i');values.frombytes(data[:len(data)//values.itemsize*values.itemsize]);fd=values[0]
        if fd is None:raise ValueError('cache descriptor missing')
        process.wait(timeout=8)
        if process.returncode:raise ValueError('cache preparation failed')
        metadata=os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid!=account.pw_uid or metadata.st_mode&0o077:
            raise ValueError('unsafe memory cache directory')
        libc=ctypes.CDLL(None,use_errno=True)
        def mounted():
            buffer=ctypes.create_string_buffer(256)
            if libc.fstatfs(fd,buffer):raise OSError(ctypes.get_errno(),'fstatfs')
            return ctypes.c_long.from_buffer(buffer).value==0x01021994
        target=('/proc/self/fd/'+str(fd)).encode()
        if release:
            if mounted() and libc.umount2(target,2):raise OSError(ctypes.get_errno(),'cannot detach memory cache')
            return
        if mounted():return
        options=('size=16m,mode=0700,uid=%d,gid=%d' % (account.pw_uid,account.pw_gid)).encode()
        if libc.mount(b'minas-cache',target,b'tmpfs',2|4|8,options):
            raise OSError(ctypes.get_errno(),'cannot mount memory cache')
    finally:
        parent.close();child.close()
        if fd is not None:os.close(fd)
        if process is not None and process.poll() is None:
            process.kill();process.wait()


def read_state(user, plugin, name):
    result = subprocess.run(**_arguments(user, plugin, name, 'read'),
                            capture_output=True, timeout=8)
    if result.returncode:
        raise ValueError('无法读取插件运行状态')
    return json.loads(result.stdout)


def write_state(user, plugin, name, value):
    if not isinstance(value, dict):raise ValueError('插件状态必须是对象')
    data = json.dumps(value, ensure_ascii=False).encode()
    if len(data)>MAX_BYTES:raise ValueError('插件状态过大')
    result = subprocess.run(**_arguments(user, plugin, name, 'write'),
                            input=data, capture_output=True, timeout=8)
    if result.returncode:raise ValueError('无法保存插件运行状态')


def delete_state(user, plugin, name):
    result=subprocess.run(**_arguments(user,plugin,name,'delete'),capture_output=True,timeout=8)
    if result.returncode:raise ValueError('无法移除插件缓存')


@contextlib.contextmanager
def state_lock(user, plugin, name):
    process = subprocess.Popen(**_arguments(user, plugin, name, 'lock'),
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    try:
        ready = select.select([process.stdout], [], [], 8)[0]
        locked = bool(ready and process.stdout.readline().strip()==b'locked')
        yield locked
    finally:
        process.stdin.close()
        try:process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(timeout=3)
        process.stdout.close()
