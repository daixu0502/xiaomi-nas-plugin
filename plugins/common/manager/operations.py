"""User-UID source staging and read-only activity probes shared by all installers."""
import json, os, pwd, re, stat, subprocess
from pathlib import Path
from layout import ENV, REGISTRY, require, lock as maintenance_lock
HOMES = Path('/home')
PRIVILEGED = Path('/data/minas/privileged')
def run(args, **kwargs):
    result = subprocess.run([str(x) for x in args], env=ENV, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    return result.stdout

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
 for file in stage.rglob('*'):
  if file.is_file() and not file.is_symlink():
   with file.open('rb') as f:os.fsync(f.fileno())
 for directory in [*sorted((p for p in stage.rglob('*') if p.is_dir()),key=lambda p:len(p.parts),reverse=True),stage,home/'scripts']:
  fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
  try:os.fsync(fd)
  finally:os.close(fd)
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


def idle_cache_locks(user, plugin, home):
    names = {'mihomo': ('update-operation.lock','core-update.lock','node-test.lock'),
             'downloadcenter': ('core-update.lock','tracker-update.lock','lifecycle.lock')}.get(plugin, ())
    if not names:
        return
    code = '''import fcntl,os,stat,sys
from pathlib import Path
base=Path(sys.argv[1])/'tmp/runtime'
for name in sys.argv[2:]:
 p=base/name
 try:fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
 except FileNotFoundError:continue
 try:
  if not stat.S_ISREG(os.fstat(fd).st_mode):raise ValueError('invalid activity lock')
  fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
 finally:os.close(fd)
'''
    try:
        run(['runuser','-u',user,'--','/usr/bin/python3','-I','-B','-c',code,home,*names], timeout=15)
    except subprocess.CalledProcessError as error:
        raise ValueError('插件有核心更新、测速或服务切换操作，或活动锁不可读取：' + plugin) from error


def idle(user, plugin, home):
    # Read-only probes run with the plugin UID, including access to its caches.
    idle_cache_locks(user, plugin, home)
    if plugin == 'downloadcenter':
        # A deliberately stopped engine reports RPC errors and can retain an
        # old active snapshot. Neither is an active transfer; preserve its stop.
        service = subprocess.run(['systemctl','is-active','--quiet','minas-plugin-downloadcenter-' + user + '.service'], env=ENV, capture_output=True, timeout=15)
        if service.returncode != 0:
            return
        environment = dict(ENV, REQUEST_METHOD='POST', CONTENT_TYPE='application/json', CONTENT_LENGTH='2', QUERY_STRING='action=status')
        result = subprocess.run(['runuser', '-u', user, '--', str(home / 'src/ui/downloadcenter.cgi')], input=b'{}', env=environment, capture_output=True, timeout=45)
        try:
            data = json.loads(re.split(br'\r?\n\r?\n', result.stdout, maxsplit=1)[1])
            require(data.get('ok') and not data.get('error'), '无法确认下载服务空闲，请稍后重试')
            require(not any(data.get(key, {}).get('state') in ('pending','checking','downloading','installing','running')
                            for key in ('coreUpdate','qbUpdate','job','trackerApply')), '下载中心有后台更新或批量操作，请稍后维护')
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
        for relative in ('docker.lock', 'modules/mediaeditor/var/write.lock',
                         'modules/sshrecovery/var/operations.lock', 'modules/mihomo/proxy.lock'):
            path = PRIVILEGED / relative
            if path.exists():
                with maintenance_lock(path, timeout=0):
                    pass


