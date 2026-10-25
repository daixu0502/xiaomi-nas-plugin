#!/bin/sh
# Generated from docker_runtime_upgrade.py.
set -eu
umask 077
export PATH="${PATH:+$PATH:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/data/docker"
command -v python3 >/dev/null 2>&1 || { echo "缺少 python3" >&2; exit 2; }
worker=$(mktemp /tmp/minas-docker-upgrade.XXXXXXXX)
trap 'rm -f -- "$worker"' EXIT
cat > "$worker" <<'MINAS_DOCKER_UPGRADE_PY'
#!/usr/bin/env python3
"""Xiaomi NAS Docker 29.9.2 installer with offline, data-aware rollback."""
import argparse
import base64
import contextlib
import ctypes
import fcntl
import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import Request, ProxyHandler, build_opener

VERSION = '29.9.2'
ARCHIVE_SHA256 = '873b3e0207857c3b2f28392fb922f3ea6efe37620c16d4d4aaf2fef409c90b73'
URL = 'https://download.docker.com/linux/static/stable/aarch64/docker-' + VERSION + '.tgz'
BASE = Path('/data/minas/docker/runtime')
DATA = Path('/data/docker_data')
FACTORY = Path('/data/docker')
NATIVE_CONFIG = Path('/tmp/dockerd/daemon.json')
RUNTIME = BASE / 'runtime' / VERSION
WORKER = BASE / 'upgrade.py'
CONFIG = BASE / 'daemon.json'
MAINTENANCE = BASE / 'maintenance'
UNIT_ROOT = Path('/etc/systemd/system')
DROPIN = UNIT_ROOT / 'docker.service.d/80-minas-runtime.conf'
GUARDS = [UNIT_ROOT / (name + '.d/79-minas-upgrade-guard.conf') for name in ('docker.service', 'docker.socket')]
BOOT_CRON = Path('/etc/cron.d/minas-docker-runtime')
PROGRAMS = {'docker', 'dockerd', 'containerd', 'containerd-shim-runc-v2', 'ctr', 'runc', 'docker-proxy', 'docker-init'}
GO_PROGRAMS = PROGRAMS - {'docker-init'}
ID_PATTERN = re.compile(r'[0-9]{8}-[0-9]{6}-[a-f0-9]{8}')
RSYNC = ['rsync', '-aHAXS', '--checksum', '--modify-window=-1', '--numeric-ids', '--one-file-system', '--no-specials', '--info=NONREG0']
ENV = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/data/docker', 'LC_ALL': 'C', 'HOME': '/root'}

# BEGIN MINAS DATA LAYOUT (generated)
"""Small standalone layout primitives; generated constants select one owner."""
DATA_LAYOUT_VERSION = 1
DATA_LAYOUT_ROOT = '/data/minas'
DATA_LAYOUT_SCOPE = 'docker-runtime'
DATA_LAYOUT_PATHS = {'/data/.minas-docker-runtime': '/data/minas/docker/runtime', '/data/docker-upgrade-backups': '/data/minas/backups/docker'}


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
    if not root.exists():root.mkdir(mode=0o755)
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
    # Shared runtimes initialize their own parents; migration is installer-owned.
    # No plugin, SSH or Docker migration archive is embedded here.
    _layout_initialize()


def remap_data_path(value):
    import os
    from pathlib import Path
    path=Path(value)
    if not path.is_absolute() or '..' in path.parts:raise ValueError('invalid historical path')
    for old,new in DATA_LAYOUT_PATHS.items():
        if path==Path(old) or path.is_relative_to(old):
            target=Path(new)/path.relative_to(old)
            if os.path.lexists(target):
                _layout_secure(DATA_LAYOUT_ROOT)
                if os.path.lexists(path):raise ValueError('conflicting historical file: '+str(path))
                return target
    return path


def rebase_data_text(text):
    for old,new in sorted(DATA_LAYOUT_PATHS.items(),key=lambda pair:len(pair[0]),reverse=True):text=text.replace(old,new)
    return text


"""One tool's transactional directory handover; no packaged foreign programs."""
DATA_LAYOUT_COPY_PATHS = {}
DATA_LAYOUT_PROGRAM = 'docker/runtime/upgrade.py'


def _layout_tool_migrate():
    import contextlib,fcntl,hashlib,json,os,stat
    from pathlib import Path
    root=Path(DATA_LAYOUT_ROOT);work=root/'migration'/DATA_LAYOUT_SCOPE
    for directory in (root/'migration',work):
        if not directory.exists():directory.mkdir(mode=0o700)
        _layout_secure(directory)
    pointer=work/'state.json'
    fd=os.open('/run/minas-layout-'+DATA_LAYOUT_SCOPE+'.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    def save(state):_layout_atomic(pointer,json.dumps(state,ensure_ascii=False,indent=2)+'\n')
    def snapshot(state,path):
        key=str(path)
        if key in state['files']:return
        if not os.path.lexists(path):state['files'][key]=None;save(state);return
        s=path.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('unsafe tool entry: '+key)
        name=hashlib.sha256(key.encode()).hexdigest();_layout_atomic(work/name,path.read_bytes())
        state['files'][key]=dict(blob=name,mode=stat.S_IMODE(s.st_mode),owner=[s.st_uid,s.st_gid],times=[s.st_atime_ns,s.st_mtime_ns]);save(state)
    def rollback(state):
        globals().get('_layout_remove_compat',lambda:None)()
        for old,item in reversed(list(state['moves'].items())):
            old,new=Path(old),Path(item['new']);present=new if new.exists() else old
            if [present.stat().st_dev,present.stat().st_ino]!=item['inode']:raise ValueError('tool directory identity changed')
            if present==new:
                if old.exists():raise ValueError('conflicting rollback directory')
                os.rename(new,old)
        for name,item in reversed(list(state['files'].items())):
            path=Path(name)
            if item is None:path.unlink(missing_ok=True);continue
            _layout_atomic(path,(work/item['blob']).read_bytes(),item['mode'])
            os.chown(path,*item['owner']);os.utime(path,ns=tuple(item['times']))
        _layout_run(['systemctl','daemon-reload'])
        callback=globals().get('_layout_runtime_restart')
        if callback and state.get('runtime'):callback(state['runtime'],False)
        for unit,wanted in state.get('services',{}).items():
            if wanted:_layout_run(['systemctl','start',unit])
        state['phase']='rolled-back';save(state)
    locks=contextlib.ExitStack();cooperating=[]
    try:
        s=os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('unsafe tool migration lock')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        for name in globals().get('_layout_lock_paths',lambda:[])():
            path=Path(name)
            if not path.parent.exists():continue
            _layout_secure(path.parent)
            other=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
            locks.callback(os.close,other);s=os.fstat(other)
            if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('unsafe cooperating operation lock')
            if not any(os.fstat(previous).st_dev==s.st_dev and os.fstat(previous).st_ino==s.st_ino for previous in cooperating):
                fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
            cooperating.append(other)
        if pointer.exists():
            previous=json.loads(pointer.read_text())
            if previous.get('scope')!=DATA_LAYOUT_SCOPE:raise ValueError('migration scope mismatch')
            if previous['phase'] not in ('committed','rolled-back'):rollback(previous)
        moves={}
        for old,new in DATA_LAYOUT_PATHS.items():
            if not os.path.lexists(old):continue
            _layout_secure(old)
            if os.path.lexists(new):
                if globals().get('_layout_is_compat',lambda p:False)(old):continue
                raise ValueError('同一工具的新旧目录同时存在，未覆盖：'+old)
            parent=Path(new).parent
            while not parent.exists():parent=parent.parent
            _layout_secure(parent)
            if Path(old).stat().st_dev!=parent.stat().st_dev or os.path.ismount(old):raise ValueError('tool migration requires the same filesystem')
            moves[old]=dict(new=new,inode=[Path(old).stat().st_dev,Path(old).stat().st_ino])
        if not moves:
            _layout_copy_shared_state()
            if DATA_LAYOUT_SCOPE=='ssh':globals().get('_layout_after_commit',lambda:None)()
            return
        state=dict(scope=DATA_LAYOUT_SCOPE,phase='prepared',moves=moves,files={},services={},runtime=None)
        # Journal only programs/configuration referring to this tool's paths.
        entries=[]
        for old in moves:
            if Path(old).name.endswith('backups'):continue
            for p in Path(old).rglob('*'):
                relative=p.relative_to(old)
                if any(part in ('transactions','keys','host','backups','__pycache__') for part in relative.parts):continue
                if p.is_file() and not p.is_symlink() and (p.suffix in ('.py','.sh') or p.name=='daemon.json'):entries.append(p)
        for directory in ('/etc/systemd/system','/etc/cron.d'):
            for p in Path(directory).rglob('*'):
                if not p.is_file() or p.is_symlink():continue
                data=p.read_bytes()
                if any(old.encode() in data for old in moves):
                    entries.append(p)
                    if directory.endswith('system'):
                        unit=p.relative_to(directory).parts[0]
                        if unit.endswith('.d'):unit=unit[:-2]
                        if unit in globals().get('DATA_LAYOUT_SERVICES',()):state['services'][unit]=_layout_run(['systemctl','is-active','--quiet',unit],False).returncode==0
        program=root/DATA_LAYOUT_PROGRAM if DATA_LAYOUT_PROGRAM else None
        if program:
            original=next((Path(old)/program.relative_to(item['new']) for old,item in moves.items() if program.is_relative_to(item['new'])),program)
            snapshot(state,original)
        for p in globals().get('_layout_journal_paths',lambda:[])():snapshot(state,Path(p))
        for p in entries:snapshot(state,p)
        save(state)
        try:
            capture=globals().get('_layout_runtime_capture')
            if capture:state['runtime']=capture(moves);save(state)
            prepare=globals().get('_layout_runtime_pause')
            if prepare and state.get('runtime'):prepare(state['runtime'])
            else:
                for unit in state['services']:_layout_run(['systemctl','stop',unit])
            state['phase']='stopped';save(state)
            for old,item in moves.items():
                target=Path(item['new']);target.parent.mkdir(mode=0o755,parents=True,exist_ok=True);_layout_secure(target.parent)
                os.rename(old,target);save(state)
            state['phase']='moved';save(state)
            for name,item in state['files'].items():
                if item is None:continue
                target=Path(rebase_data_text(name));data=(work/item['blob']).read_bytes()
                if b'\0' not in data:
                    for old,new in DATA_LAYOUT_PATHS.items():data=data.replace(old.encode(),new.encode())
                _layout_atomic(target,data,item['mode']);os.chown(target,*item['owner'])
            # Install only this entry's own current Python worker. No unrelated
            # Docker/network/SSH installer is carried in an archive.
            source=globals().get('__file__','')
            if not source or not Path(source).is_file():source=os.environ.get('MINAS_LAYOUT_SHELL_SOURCE','')
            program=root/DATA_LAYOUT_PROGRAM if DATA_LAYOUT_PROGRAM else None
            if program and source and Path(source).is_file() and any(program.is_relative_to(item['new']) for item in moves.values()):
                s=Path(source).lstat()
                if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('unsafe current tool worker')
                _layout_atomic(program,Path(source).read_bytes(),0o700)
            _layout_copy_shared_state()
            _layout_run(['systemctl','daemon-reload'])
            restart=globals().get('_layout_runtime_restart')
            if restart and state.get('runtime'):restart(state['runtime'],True)
            else:
                for unit,wanted in state['services'].items():
                    if wanted:_layout_run(['systemctl','start',unit])
            globals().get('_layout_after_commit',lambda:None)()
            state['phase']='committed';save(state)
        except BaseException:
            rollback(state);raise
    finally:locks.close();os.close(fd)


def _layout_run(args,check=True):
    import subprocess
    return subprocess.run(args,check=check,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=180)


def _layout_copy_shared_state():
    return


def ensure_data_layout():
    _layout_initialize()
    _layout_tool_migrate()


"""Docker runtime handover only; image and volume directories are untouched."""

def _layout_lock_paths():
    roots=('/data/.minas-privileged','/data/minas/privileged')
    paths=[Path(root)/name for root in roots for name in ('component.lock','docker.lock')]
    paths += [Path(root)/'lock' for root in ('/data/docker-bridge-repair','/data/minas/docker/network')]
    paths += [Path(root)/'upgrade.lock' for root in ('/data/.minas-docker-runtime','/data/minas/docker/runtime')]
    return [Path('/data/plugin/.local-plugin-installer.lock'),*paths]


def _layout_journal_paths():
    return [Path(DATA_LAYOUT_ROOT)/'migration/docker-runtime/maintenance',
            *[UNIT_ROOT/(unit+'.d/98-minas-runtime-layout.conf') for unit in ('docker.service','docker.socket')]]


def _layout_runtime_capture(moves):
    old='/data/.minas-docker-runtime'
    if old not in moves:return None
    if any(json.loads(p.read_text()).get('phase') not in ('installed','rolled-back') for p in Path(old,'transactions').glob('*/state.json')):
        raise ValueError('Docker 有未完成的升级事务，请先处理后再迁移目录')
    if not DROPIN.is_file() or old not in DROPIN.read_text():return None
    service=active('docker.service');sock=active('docker.socket')
    if service!=sock:raise ValueError('Docker 服务与 socket 状态不一致，请先检查')
    if not service:return None
    info=json.loads(docker('info','--format','{{json .}}').stdout)
    if info.get('LiveRestoreEnabled') or info.get('Swarm',{}).get('LocalNodeState','inactive')!='inactive':raise ValueError('不支持迁移 live-restore 或 swarm 运行时')
    before=[dict(id=c['Id'],running=bool(c['State']['Running']),restart=c['HostConfig']['RestartPolicy']) for c in containers()]
    return dict(before=dict(containers=before),version=info['ServerVersion'])


def _layout_runtime_pause(state):
    paths=_layout_journal_paths();gate=paths[0]
    _layout_atomic(gate,b'maintenance\n')
    for path in paths[1:]:
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
        _layout_atomic(path,'[Unit]\nConditionPathExists=!'+str(gate)+'\n',0o644)
    _layout_run(['systemctl','daemon-reload'])
    for item in state['before']['containers']:docker('update','--restart=no',item['id'])
    ids=[c['id'] for c in state['before']['containers'] if c['running']]
    if ids:docker('stop','--time','30',*ids,timeout=180)
    _layout_run(['systemctl','stop','docker.socket','docker.service'])
    for proc in Path('/proc').glob('[0-9]*/exe'):
        try:exe=os.readlink(proc)
        except OSError:continue
        if exe.startswith('/data/.minas-docker-runtime/') and Path(exe).name in ('dockerd','containerd','containerd-shim-runc-v2','runc'):
            raise ValueError('旧 Docker 运行时尚未退出，未移动目录')


def _layout_runtime_restart(state,migrated):
    for path in _layout_journal_paths():path.unlink(missing_ok=True)
    _layout_run(['systemctl','daemon-reload'])
    _layout_run(['systemctl','start','docker.socket','docker.service'])
    deadline=time.monotonic()+60
    while True:
        result=docker('info','--format','{{json .}}',check=False,timeout=10)
        if result.returncode==0:break
        if time.monotonic()>=deadline:raise ValueError('迁移后 Docker 未能就绪')
        time.sleep(1)
    if json.loads(result.stdout)['ServerVersion']!=state['version']:raise ValueError('迁移前后 Docker 版本不一致')
    restore_running(state)
# END MINAS DATA LAYOUT

def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def plain(path):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, '路径必须为绝对路径且不能包含 ..')
    for node in (path, *path.parents):
        require(not node.is_symlink(), '拒绝符号链接路径：' + str(node))
    return path


def secure(path, create=False, private=False):
    path = plain(path)
    if create and not path.exists():
        secure(path.parent)
        path.mkdir(mode=0o700 if private else 0o755)
    for node in (path, *path.parents):
        value = node.stat()
        require(stat.S_ISDIR(value.st_mode) and value.st_uid == 0 and not value.st_mode & 0o022,
                '目录须由 root 拥有且不能由普通用户写入：' + str(node))
    if private:
        require(not path.stat().st_mode & 0o077, '事务或备份目录权限必须为 0700：' + str(path))
    return path


def atomic(path, data, mode=0o600):
    path = plain(path)
    if isinstance(data, dict): data = json.dumps(data, ensure_ascii=True, indent=2) + '\n'
    if isinstance(data, str): data = data.encode()
    secure(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data); output.flush(); os.fsync(output.fileno()); os.fchmod(output.fileno(), mode)
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def read(path):
    path = plain(path); value = path.stat()
    require(stat.S_ISREG(value.st_mode) and value.st_uid == 0 and not value.st_mode & 0o022, '状态文件权限异常')
    result = json.loads(path.read_text())
    require(isinstance(result, dict), '状态必须是 JSON 对象')
    return result


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): digest.update(block)
    return digest.hexdigest()


def run(argv, check=True, timeout=90, runtime=False):
    env = dict(ENV)
    if runtime: env['PATH'] = str(RUNTIME) + ':' + env['PATH']
    result = subprocess.run(list(map(str, argv)), capture_output=True, text=True, timeout=timeout, env=env, cwd='/')
    if check and result.returncode:
        raise RuntimeError('%s 失败：%s' % (Path(str(argv[0])).name, result.stderr.strip()[-1200:]))
    return result


def docker(*args, new=False, timeout=90, check=True):
    return run([(RUNTIME if new else FACTORY) / 'docker', '--host=unix:///run/docker.sock', *args],
               check=check, timeout=timeout, runtime=new)


def active(unit):
    return run(['systemctl', 'is-active', '--quiet', unit], check=False).returncode == 0


def mountinfo():
    def decode(value):
        return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)
    records = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split(); sep = fields.index('-')
        records.append(dict(path=decode(fields[4]), root=decode(fields[3]), device=fields[2], fs=fields[sep+1], source=decode(fields[sep+2])))
    return records


def filesystem(path):
    path = str(Path(path))
    return max((m for m in mountinfo() if path == m['path'] or path.startswith(m['path'].rstrip('/') + '/')),
               key=lambda m: len(m['path']))


def host_namespace():
    require(os.geteuid() == 0, '需要 NAS root 命令行')
    if os.readlink('/proc/self/ns/mnt') != os.readlink('/proc/1/ns/mnt'):
        fd = os.open('/proc/1/ns/mnt', os.O_RDONLY)
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.setns(fd, 0): raise OSError(ctypes.get_errno(), '进入 NAS 主机挂载命名空间失败')
        finally: os.close(fd)
    require(os.uname().machine in ('aarch64', 'arm64'), '本脚本仅支持 ARM64 NAS')
    require(Path('/etc/config/mi_docker').is_file(), '未找到原厂 mi_docker 配置')


def go_version(path):
    # Go 1.18+ inline build information; do not infer the build from random strings.
    with path.open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        start = data.find(b'\xff Go buildinf:')
        require(start >= 0 and start + 33 <= len(data) and data[start+15] & 2, '缺少可核验的 Go 编译信息：' + path.name)
        offset = start + 32; length = 0; shift = 0
        while True:
            require(offset < len(data) and shift <= 28, 'Go 编译信息损坏')
            value = data[offset]; offset += 1; length |= (value & 127) << shift
            if value < 128: break
            shift += 7
        text = data[offset:offset+length].decode('ascii')
    match = re.fullmatch(r'go(\d+)\.(\d+)\.(\d+)', text)
    require(match is not None, '拒绝非正式 Go 构建：' + text)
    version = tuple(map(int, match.groups()))
    safe = (version[:2] == (1,26) and version[2] >= 10) or (version[:2] == (1,27) and version[2] >= 3) or version >= (1,28,0)
    require(safe, 'Go 构建未修复 CVE-2026-111943：' + text)
    return text


def verify_runtime(directory=None, execute=True):
    directory = RUNTIME if directory is None else directory
    secure(directory)
    meta = read(directory / 'package.json')
    require(meta['archiveSha256'] == ARCHIVE_SHA256 and meta['version'] == VERSION and set(meta['files']) == PROGRAMS,
            '已安装运行时清单不匹配')
    result = {}
    for name in sorted(PROGRAMS):
        path = plain(directory / name); value = path.stat()
        require(stat.S_ISREG(value.st_mode) and value.st_uid == 0 and not value.st_mode & 0o022 and value.st_mode & 0o111,
                '运行文件权限异常：' + name)
        require(sha(path) == meta['files'][name], '运行文件摘要不匹配：' + name)
        with path.open('rb') as stream: header = stream.read(20)
        require(header[:6] == b'\x7fELF\x02\x01' and int.from_bytes(header[18:20], 'little') == 183, '运行文件不是 ARM64 ELF')
        if name in GO_PROGRAMS: result[name] = go_version(path)
    require(set(p.name for p in directory.iterdir()) == PROGRAMS | {'package.json'}, '运行时目录有未登记文件')
    if execute:
        for name in ('docker','dockerd'):
            require(re.search(r'\b' + re.escape(VERSION) + r'\b', run([directory/name,'--version'],runtime=True).stdout), 'Docker 版本不匹配')
    return result


def extract_runtime(archive, destination):
    require(sha(archive) == ARCHIVE_SHA256, 'Docker 官方包 SHA-256 不匹配，拒绝解压')
    with tarfile.open(archive, 'r:gz') as package:
        members = package.getmembers()
        files = [m for m in members if m.isfile()]
        require({m.name for m in files} == {'docker/' + n for n in PROGRAMS} and len(files) == len(PROGRAMS), 'Docker 包文件清单异常')
        require(all(m.isfile() or (m.isdir() and m.name.rstrip('/') == 'docker') for m in members), '包中含链接或特殊文件')
        require(all(0 < m.size <= 128*1024*1024 for m in files), 'Docker 包文件大小异常')
        hashes = {}
        for member in files:
            name = member.name.split('/')[1]; path = destination/name
            with package.extractfile(member) as source, path.open('xb') as output: shutil.copyfileobj(source, output)
            path.chmod(0o755); hashes[name] = sha(path)
    atomic(destination/'package.json', dict(version=VERSION, archiveSha256=ARCHIVE_SHA256, files=hashes))


def prepare(archive=None, proxy=None):
    secure(BASE, create=True, private=True); secure(BASE/'runtime', create=True)
    if RUNTIME.exists():
        verify_runtime(); return
    with tempfile.TemporaryDirectory(prefix='minas-docker-download-', dir='/tmp') as temporary:
        if archive is None:
            archive = Path(temporary)/'docker.tgz'
            opener = build_opener(ProxyHandler({'https': proxy} if proxy else {}))
            print('下载 Docker %s 官方 ARM64 静态包……' % VERSION, flush=True)
            with opener.open(Request(URL,headers={'User-Agent':'Minas-Docker-Upgrade'}), timeout=30) as response, archive.open('xb') as output:
                total = 0
                while True:
                    data = response.read(1024*1024)
                    if not data: break
                    total += len(data); require(total <= 160*1024*1024, 'Docker 包超过大小限制'); output.write(data)
        else: archive = Path(archive)
        stage = Path(tempfile.mkdtemp(prefix='.' + VERSION + '-', dir=BASE/'runtime'))
        try:
            extract_runtime(archive, stage); verify_runtime(stage)
            os.replace(stage, RUNTIME)
        finally:
            if stage.exists(): shutil.rmtree(stage)
    print('运行时校验通过，原厂 Docker 未切换。', flush=True)


def derived_config(original, require_event_active=True):
    require(isinstance(original, dict), '原厂 daemon.json 无效')
    require(original.get('data-root') == str(DATA) and original.get('exec-root') == '/tmp/docker_exec', '数据或临时目录与评估布局不符')
    require(original.get('storage-driver') == 'overlay2', '仅支持保留 overlay2 的迁移')
    require(not original.get('live-restore') and not original.get('containerd') and not original.get('runtimes'), '暂不支持 live-restore、外部 containerd 或自定义 runtimes')
    value = json.loads(json.dumps(original)); features = value.setdefault('features', {})
    require(isinstance(features, dict) and features.get('containerd-snapshotter') is not True, '不能同时迁移镜像存储方式')
    features['containerd-snapshotter'] = False
    if value.get('ipv6'):
        # This NAS uses a separate nftables NAT66 service, not kernel ip6_tables.
        unit = 'minas-docker-ipv6.service'
        require((not require_event_active or active(unit)) and run(['systemctl','is-enabled',unit],check=False).stdout.strip() == 'enabled'
                and '--watch-ipv6' in run(['systemctl','show',unit,'-p','ExecStart','--value']).stdout,
                'IPv6 已启用但未检测到事件驱动的 NAT66 恢复服务，先完成网络适配')
        value['ip6tables'] = False
    return value


def render_config():
    secure(BASE, private=True)
    # The event service starts AFTER Docker. It must be installed/enabled here,
    # but cannot already be active during Docker's own ExecStartPre at boot.
    config = derived_config(read(NATIVE_CONFIG),require_event_active=False)
    atomic(CONFIG, config)
    run([RUNTIME/'dockerd','--validate','--config-file='+str(CONFIG)], runtime=True)
    return config


def containers():
    ids = docker('ps','-aq','--no-trunc').stdout.split()
    require(all(re.fullmatch('[a-f0-9]{64}', value) for value in ids), '容器 ID 异常')
    if not ids: return []
    return json.loads(docker('inspect',*ids).stdout)


def inspect_system():
    for name in ('systemctl','rsync','du','file'):
        require(shutil.which(name, path=ENV['PATH']), '缺少命令：' + name)
    capabilities = run(['rsync','--version']).stdout
    require(all(x in capabilities and 'no '+x not in capabilities for x in ('hardlinks','ACLs','xattrs')), 'rsync 缺少备份必需能力')
    require(active('docker.service') and active('docker.socket'), '升级前 Docker 服务及 socket 必须运行')
    require('dockerd_prepare.sh' in run(['systemctl','show','docker.service','-p','ExecStartPre','--value']).stdout, '缺少原厂 Docker 配置生成步骤')
    require(str(FACTORY/'dockerd') in run(['systemctl','show','docker.service','-p','ExecStart','--value']).stdout or DROPIN.exists(), 'Docker 启动路径不属于原厂或本工具')
    info = json.loads(docker('info','--format','{{json .}}').stdout)
    require(info['DockerRootDir'] == str(DATA) and info['Driver'] == 'overlay2', 'Docker 数据布局不符')
    require(info.get('CgroupVersion') == '2' and info.get('CgroupDriver') == 'systemd', 'cgroup 模式与评估不符')
    require(not info.get('LiveRestoreEnabled') and info.get('Swarm',{}).get('LocalNodeState','inactive') == 'inactive', '仅支持非 swarm、未启用 live-restore 的设备')
    require(DATA.stat().st_ino != DATA.parent.stat().st_ino, 'Docker 数据根不能指向整个 /data')
    config = derived_config(read(NATIVE_CONFIG))
    items = containers()
    summary = [dict(id=c['Id'], name=c['Name'].lstrip('/'), running=bool(c['State']['Running']),
                    restart=c['HostConfig']['RestartPolicy'], mounts=c.get('Mounts',[])) for c in items]
    volumes = local_volumes()
    for entry, item in zip(summary, items): entry['image'] = item['Image']
    bind = sorted({m['Source'] for c in summary for m in c['mounts'] if m.get('Type') == 'bind' and m.get('RW')})
    external_mounts = {}
    for source in bind:
        if source == '/' or source.startswith(('/run/','/var/run/','/tmp/','/dev/','/proc/','/sys/')): continue
        record = filesystem(Path(source).resolve()); external_mounts[record['path']] = record
    return dict(version=info['ServerVersion'], driver=info['Driver'], dataRoot=info['DockerRootDir'],
                containers=summary, volumes=volumes, externalWritableBinds=bind, derivedConfig=config,
                dataIdentity=[DATA.stat().st_dev, DATA.stat().st_ino], externalMounts=list(external_mounts.values()),
                runtimePrepared=RUNTIME.exists(), originalRuntimePreserved=True)


def runtime_processes():
    found = []
    for proc in Path('/proc').glob('[0-9]*'):
        try: exe = os.readlink(proc/'exe')
        except OSError: continue
        if Path(exe).name in ('dockerd','containerd','containerd-shim','containerd-shim-runc-v2','runc') and (exe.startswith(str(FACTORY)+'/') or exe.startswith(str(BASE)+'/')):
            found.append((int(proc.name),exe))
    return found


def require_quiescent():
    require(not active('docker.service') and not active('docker.socket'), '引擎或 socket 尚未停止')
    require(not runtime_processes(), '旧容器运行时尚未退出，停止备份/回退')
    require(not any(m['path'].startswith(str(DATA)+'/') for m in mountinfo()), 'Docker 数据目录仍有子挂载，停止备份/回退')


def tree_entries(root, exclude=()):
    root=Path(root);excluded={Path(p) for p in exclude}
    def walk_error(error):raise error
    for parent,directories,files in os.walk(root,followlinks=False,onerror=walk_error):
        directories[:]=[name for name in directories if (Path(parent)/name).relative_to(root) not in excluded]
        for name in directories+files:
            path=Path(parent)/name
            if path.relative_to(root) not in excluded:yield path


def exact_symlink_modes(source, target, exclude=()):
    """Preserve NAS link modes without ever chmod'ing a link's referent."""
    source, target = Path(source), Path(target)
    groups = {}; changed_parents = set(); changed = 0
    for path in tree_entries(source,exclude):
        value=path.lstat()
        if stat.S_ISLNK(value.st_mode):
            groups.setdefault((value.st_dev,value.st_ino),[]).append((path,value))

    def publish(destination, create):
        temporary = destination.parent/('.minas-link-'+os.urandom(12).hex())
        created = False
        try:
            create(temporary); created = True
            require(stat.S_ISLNK(destination.lstat().st_mode), '链接目标类型已改变：'+str(destination))
            os.replace(temporary,destination)
            changed_parents.add(destination.parent)
        finally:
            if created and os.path.lexists(temporary): temporary.unlink()

    for members in groups.values():
        path, value = members[0]; destination = target/path.relative_to(source)
        actual = destination.lstat()
        require(stat.S_ISLNK(actual.st_mode), '复制后符号链接丢失：'+str(destination))
        require(os.readlink(destination) == os.readlink(path), '复制后链接指向不一致：'+str(destination))
        mode = stat.S_IMODE(value.st_mode)
        if stat.S_IMODE(actual.st_mode) != mode:
            attrs = {key:os.getxattr(path,key,follow_symlinks=False) for key in os.listxattr(path,follow_symlinks=False)}
            link = os.readlink(path)
            def create(temporary):
                previous = os.umask(0o777 ^ (mode & 0o777))
                try: os.symlink(link,temporary)
                finally: os.umask(previous)
                try:
                    require(stat.S_IMODE(temporary.lstat().st_mode) == mode,
                            '文件系统不能精确保存符号链接权限 '+oct(mode)+'：'+str(destination))
                    os.chown(temporary,value.st_uid,value.st_gid,follow_symlinks=False)
                    for key in os.listxattr(temporary,follow_symlinks=False):
                        if key not in attrs: os.removexattr(temporary,key,follow_symlinks=False)
                    for key,data in attrs.items(): os.setxattr(temporary,key,data,follow_symlinks=False)
                    os.utime(temporary,ns=(value.st_atime_ns,value.st_mtime_ns),follow_symlinks=False)
                except BaseException:
                    temporary.unlink(); raise
            publish(destination,create); changed += 1
        # Replacing one inode must not split hard-linked symbolic links.
        head = destination.lstat()
        for alias, _ in members[1:]:
            other = target/alias.relative_to(source); value_other = other.lstat()
            require(stat.S_ISLNK(value_other.st_mode), '复制后符号链接丢失：'+str(other))
            require(os.readlink(other) == os.readlink(alias), '复制后链接指向不一致：'+str(other))
            if (value_other.st_dev,value_other.st_ino) != (head.st_dev,head.st_ino):
                publish(other,lambda temporary:os.link(destination,temporary,follow_symlinks=False))
                changed += 1
    # Atomic link replacement changes its parent directory's mtime.
    for parent in changed_parents:
        value = (source/parent.relative_to(target)).stat(follow_symlinks=False)
        require(stat.S_ISDIR(value.st_mode) and stat.S_ISDIR(parent.lstat().st_mode), '链接父目录类型异常')
        os.utime(parent,ns=(value.st_atime_ns,value.st_mtime_ns),follow_symlinks=False)
    return changed


def rsync_differences(output):
    return [line for line in output.splitlines() if line.strip()]


def special_nodes(root, exclude=()):
    result = {}
    for path in tree_entries(root,exclude):
        value=path.lstat()
        if stat.S_ISFIFO(value.st_mode) or stat.S_ISSOCK(value.st_mode):
            result[path.relative_to(root)]=value
    return result


def node_metadata(path, value=None):
    value=path.lstat() if value is None else value
    attrs={key:os.getxattr(path,key,follow_symlinks=False) for key in os.listxattr(path,follow_symlinks=False)}
    return (value.st_mode,value.st_uid,value.st_gid,value.st_mtime_ns,attrs)


def exact_special_nodes(source,target,verify=False,exclude=()):
    """Copy dormant socket/FIFO inodes with Linux mknod, never bind/open them."""
    source,target=Path(source),Path(target)
    wanted=special_nodes(source,exclude);existing=special_nodes(target,exclude)
    if verify:
        require(set(wanted)==set(existing),'socket/FIFO 节点清单不一致：'+str(sorted(set(wanted)^set(existing))[:8]))
        source_links={};target_links={}
        for relative,value in sorted(wanted.items()):
            actual=existing[relative]
            require(node_metadata(source/relative,value)==node_metadata(target/relative,actual),'socket/FIFO 元数据不一致：'+str(relative))
            first=source_links.setdefault((value.st_dev,value.st_ino),relative)
            other=target_links.setdefault((actual.st_dev,actual.st_ino),relative)
            require(first==other,'socket/FIFO 硬链接关系不一致：'+str(relative))
        return
    parents=set()
    def parent_safe(path):
        require(path.is_relative_to(target),'节点路径超出目标目录')
        for node in (path.parent,*path.parent.parents):
            require(stat.S_ISDIR(node.lstat().st_mode),'节点父目录不是真实目录：'+str(node))
            if node==target:break
    for relative in set(existing)-set(wanted):
        path=target/relative;parent_safe(path);path.unlink();parents.add(path.parent)
    heads={}
    for relative,value in sorted(wanted.items()):
        path=target/relative;parent_safe(path)
        first=heads.get((value.st_dev,value.st_ino))
        actual=path.lstat() if os.path.lexists(path) else None
        if first is not None:
            head=first.lstat()
            same=actual is not None and (actual.st_dev,actual.st_ino)==(head.st_dev,head.st_ino)
        else:
            same=actual is not None and node_metadata(source/relative,value)==node_metadata(path,actual)
        if not same:
            temporary=path.parent/('.minas-node-'+os.urandom(12).hex());created=False
            try:
                if first is not None:
                    os.link(first,temporary,follow_symlinks=False);created=True
                else:
                    # rsync implements socket mknod via bind(), whose pathname
                    # has a much shorter limit. Linux mknod has no sun_path limit.
                    os.mknod(temporary,value.st_mode);created=True
                    os.chown(temporary,value.st_uid,value.st_gid,follow_symlinks=False)
                    attrs=node_metadata(source/relative,value)[4]
                    for key in os.listxattr(temporary,follow_symlinks=False):
                        if key not in attrs:os.removexattr(temporary,key,follow_symlinks=False)
                    for key,data in attrs.items():os.setxattr(temporary,key,data,follow_symlinks=False)
                    os.chmod(temporary,stat.S_IMODE(value.st_mode))
                    os.utime(temporary,ns=(value.st_atime_ns,value.st_mtime_ns),follow_symlinks=False)
                if actual is not None and stat.S_ISDIR(actual.st_mode):shutil.rmtree(path)
                os.replace(temporary,path);parents.add(path.parent)
            finally:
                if created and os.path.lexists(temporary):temporary.unlink()
        heads.setdefault((value.st_dev,value.st_ino),path)
    for parent in parents:
        # A receiver directory may have been replaced by the source's socket.
        try:parent_value=parent.lstat()
        except (FileNotFoundError,NotADirectoryError):continue
        if not stat.S_ISDIR(parent_value.st_mode):continue
        value=(source/parent.relative_to(target)).lstat()
        require(stat.S_ISDIR(value.st_mode),'节点源父目录不是真实目录')
        os.utime(parent,ns=(value.st_atime_ns,value.st_mtime_ns),follow_symlinks=False)


def rsync_tree(source, target, verify=False, diagnostic=None, exclude=()):
    argv = RSYNC + ['--delete']
    for value in exclude:
        value=Path(value)
        require(not value.is_absolute() and '..' not in value.parts and not any(c in str(value) for c in '*?[]\\'), '排除路径无效')
        argv += ['--exclude=/'+value.as_posix()]
    if verify: argv += ['--dry-run','--itemize-changes','--out-format=%i %n%L']
    result = run(argv + [str(source).rstrip('/')+'/', str(target).rstrip('/')+'/'], timeout=86400)
    if not verify:
        exact_special_nodes(source,target,exclude=exclude)
        exact_symlink_modes(source,target,exclude=exclude)
    if verify:
        differences = rsync_differences(result.stdout)
        if differences:
            if diagnostic is not None: atomic(diagnostic,result.stdout)
            details = '\n'.join(differences[:8])
            suffix = '\n完整差异：'+str(diagnostic) if diagnostic is not None else ''
            raise RuntimeError('备份或恢复内容与原数据不一致：\n'+details+suffix)
        try:exact_special_nodes(source,target,verify=True,exclude=exclude)
        except RuntimeError as error:
            if diagnostic is not None:atomic(diagnostic,str(error)+'\n')
            raise


def tree_digest(directory, special_hardlinks=False):
    """Protect content, links, owners, permissions and overlay/ACL xattrs."""
    digest = hashlib.sha256(); inodes = {}
    for path in [directory] + sorted(directory.rglob('*')):
        value = path.lstat(); relative = path.relative_to(directory).as_posix()
        attrs = {key: base64.b64encode(os.getxattr(path,key,follow_symlinks=False)).decode()
                 for key in sorted(os.listxattr(path,follow_symlinks=False))}
        row = [relative,value.st_mode,value.st_uid,value.st_gid,value.st_mtime_ns,attrs]
        if stat.S_ISREG(value.st_mode):
            inode = (value.st_dev,value.st_ino)
            if inode in inodes: row += ['hardlink',inodes[inode]]
            else: inodes[inode] = relative; row += ['file',sha(path)]
        elif stat.S_ISLNK(value.st_mode): row += ['link',os.readlink(path)]
        elif stat.S_ISCHR(value.st_mode) or stat.S_ISBLK(value.st_mode): row += ['device',value.st_rdev]
        elif special_hardlinks and (stat.S_ISFIFO(value.st_mode) or stat.S_ISSOCK(value.st_mode)):
            inode=(value.st_dev,value.st_ino)
            if inode in inodes:row += ['hardlink',inodes[inode]]
            else:inodes[inode]=relative;row += ['special']
        else: require(stat.S_ISDIR(value.st_mode) or stat.S_ISFIFO(value.st_mode) or stat.S_ISSOCK(value.st_mode), '数据含不支持的文件类型')
        digest.update(json.dumps(row,sort_keys=True,ensure_ascii=True,separators=(',',':')).encode()+b'\n')
    return digest.hexdigest()


def transaction(ident):
    require(ID_PATTERN.fullmatch(ident) is not None, '事务编号格式错误')
    folder = secure(BASE/'transactions'/ident, private=True); state = read(folder/'state.json')
    require(state['id'] == ident and state['targetVersion'] == VERSION, '事务版本不符')
    return folder,state


def save(folder, state, phase=None):
    if phase is not None: state['phase'] = phase
    atomic(folder/'state.json', state)


def guard_bytes():
    return ('# Managed by minas Docker runtime upgrade\n[Unit]\nConditionPathExists=!'+str(MAINTENANCE)+'\n').encode()


def dropin_bytes():
    return ('# Managed by minas Docker runtime upgrade\n[Service]\n'
            'Environment="PATH='+str(RUNTIME)+':'+ENV['PATH']+'"\n'
            'ExecStartPre=/usr/bin/python3 -B '+str(WORKER)+' --render-config\n'
            'ExecStart=\nExecStart='+str(RUNTIME)+'/dockerd -H fd:// --config-file='+str(CONFIG)+'\n').encode()


def owned_files_valid():
    for path, data in [(DROPIN,dropin_bytes())] + [(p,guard_bytes()) for p in GUARDS]:
        require(not path.exists() or (not path.is_symlink() and path.read_bytes() == data), '本工具配置被修改，拒绝覆盖或删除：'+str(path))


def startup_cron_bytes(ident):
    require(ID_PATTERN.fullmatch(ident) is not None,'事务编号格式错误')
    return ('# Managed by minas Docker runtime upgrade\nSHELL=/bin/sh\n'
            'PATH=/usr/sbin:/usr/bin:/sbin:/bin\nMAILTO=""\n'
            '@reboot root /usr/bin/timeout -k 10 360 /usr/bin/python3 -B '+str(WORKER)+
            ' --boot-reconcile '+ident+'\n').encode()


def startup_hook_valid(ident):
    plain(BOOT_CRON)
    require(not BOOT_CRON.exists() or (stat.S_ISREG(BOOT_CRON.stat().st_mode) and
            BOOT_CRON.stat().st_uid==0 and not BOOT_CRON.stat().st_mode&0o022 and
            BOOT_CRON.read_bytes()==startup_cron_bytes(ident)),
            'Docker 开机校正配置被修改或属于其他事务，拒绝覆盖：'+str(BOOT_CRON))


def startup_hook(ident,enable):
    startup_hook_valid(ident)
    if enable:
        secure(BOOT_CRON.parent);atomic(BOOT_CRON,startup_cron_bytes(ident),0o644)
    else:BOOT_CRON.unlink(missing_ok=True)
    run(['systemctl','reload','crond.service'])


def runtime_selection():
    """Report the real process and API separately from systemd's loaded config."""
    configured=run(['systemctl','show','docker.service','-p','ExecStart','--value'],check=False).stdout
    result=dict(configuredForTarget=str(RUNTIME/'dockerd') in configured,
                serviceActive=active('docker.service'),executable=None,serverVersion=None)
    pid=run(['systemctl','show','docker.service','-p','MainPID','--value'],check=False).stdout.strip()
    if pid.isdigit() and int(pid):
        try:result['executable']=os.readlink('/proc/'+pid+'/exe')
        except OSError:pass
    try:response=docker('info','--format','{{json .}}',check=False,timeout=10)
    except (OSError,subprocess.SubprocessError):response=None
    if response is not None and response.returncode==0:
        try:result['serverVersion']=json.loads(response.stdout).get('ServerVersion')
        except (ValueError,AttributeError):pass
    result['runningTarget']=result['executable']==str(RUNTIME/'dockerd') and result['serverVersion']==VERSION
    return result


def repair_startup(folder,state,automatic=False):
    """Reconcile cached factory units; never restore or roll back Docker data."""
    require(state['phase']=='installed','仅已成功安装的升级事务允许开机校正')
    require(not Path('/data/minas/migration/maintenance').exists(),'目录迁移正在进行')
    owned_files_valid();startup_hook_valid(state['id'])
    require(DROPIN.is_file(),'升级启动配置缺失，拒绝猜测安装状态')
    require_data_environment(state);verify_runtime(RUNTIME)
    plan_file=BASE/'startup-repair.json'
    plan=read(plan_file) if plan_file.exists() else None
    pending=plan is not None and plan.get('phase')!='complete'
    if pending:
        require(plan.get('id')==state['id'],'未完成的开机校正属于其他事务')
        require(not automatic or plan.get('bootId')==Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                '上次开机校正未完成，需要显式修复，不能自动重放旧容器状态')
    require(not MAINTENANCE.exists() or (pending and not automatic),
            'Docker 处于维护状态；开机校正不会解除安装或回退的保护锁')
    run(['systemctl','daemon-reload'])
    selection=runtime_selection()
    if selection['runningTarget'] and not pending:
        require(all(exe.startswith(str(RUNTIME)+'/') for _,exe in runtime_processes()),'新旧容器运行时混用')
        return dict(changed=False,**selection)
    require(pending or (selection['serviceActive'] and selection['executable']==str(FACTORY/'dockerd') and
                       selection['serverVersion']=='20.10.17'),
            '当前引擎不是已评估的原厂 20.10.17 或目标运行时，停止自动切换')
    if pending and selection['serviceActive']:
        require(selection['executable'] in (str(FACTORY/'dockerd'),str(RUNTIME/'dockerd')) and
                selection['serverVersion'] in ('20.10.17',VERSION),'未完成校正期间引擎已改变，拒绝重放旧状态')
    if selection['serviceActive']:
        rows=containers()
        if pending:
            require({row['Id'] for row in rows}=={row['id'] for row in plan['before']['containers']},
                    '未完成校正后的容器集合已改变，请先评估状态')
        else:
            plan=dict(id=state['id'],phase='captured',bootId=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),before=dict(containers=[
                dict(id=row['Id'],running=row['State']['Running'],restart=row['HostConfig']['RestartPolicy']) for row in rows]))
    else:
        ids={p.name for p in (DATA/'containers').iterdir() if p.is_dir()}
        require(pending and ids=={row['id'] for row in plan['before']['containers']},'离线校正的容器集合已改变')
    render_config()
    if not pending:atomic(plan_file,plan)
    try:
        guards(True)
        plan['phase']='switching';atomic(plan_file,plan)
        if selection['serviceActive']:
            for row in plan['before']['containers']:docker('update','--restart=no',row['id'])
            running=docker('ps','-q','--no-trunc').stdout.split()
            if running:docker('stop','--time','30',*running,timeout=180)
        stop_engine();require_data_environment(state)
        start_engine(new=True);verify_engine(plan)
        plan['phase']='complete';atomic(plan_file,plan)
        return dict(changed=True,**runtime_selection())
    except BaseException:
        # Preserve data and desired container states for explicit retry.
        # rollback() must never run here: it would restore old engine metadata.
        plan['phase']='failed';atomic(plan_file,plan)
        guards(True)
        run(['systemctl','stop','docker.socket','docker.service'],check=False,timeout=180)
        raise


def guards(enable):
    if enable:
        atomic(MAINTENANCE, b'maintenance\n')
        for path in GUARDS:
            secure(path.parent,create=True); atomic(path,guard_bytes(),0o644)
    else:
        MAINTENANCE.unlink(missing_ok=True)
    run(['systemctl','daemon-reload'])


def stop_engine():
    run(['systemctl','stop','docker.socket','docker.service'], timeout=180)
    deadline = time.monotonic()+30
    while runtime_processes() and time.monotonic()<deadline: time.sleep(.5)
    require_quiescent()


def restore_policies(state, new=False):
    for item in state['before']['containers']:
        policy = item['restart']; value = policy.get('Name') or 'no'
        if value == 'on-failure' and policy.get('MaximumRetryCount'): value += ':'+str(policy['MaximumRetryCount'])
        docker('update','--restart='+value,item['id'],new=new)


def restore_running(state, new=False):
    before = state['before']['containers']; wanted = {c['id'] for c in before if c['running']}
    current = set(docker('ps','-q','--no-trunc',new=new).stdout.split())
    for ident in sorted(current-wanted): docker('stop','--time','30',ident,new=new,timeout=120)
    restore_policies(state,new)
    for ident in sorted(wanted-current): docker('start',ident,new=new,timeout=120)
    actual = set(docker('ps','-q','--no-trunc',new=new).stdout.split())
    require(actual == wanted, '容器运行集合与升级前不一致')
    require(set(docker('ps','-aq','--no-trunc',new=new).stdout.split()) == {c['id'] for c in before}, '容器数量或 ID 发生变化')


def start_engine(new=False):
    MAINTENANCE.unlink(missing_ok=True)
    run(['systemctl','reset-failed','docker.service','docker.socket'],check=False)
    run(['systemctl','start','docker.socket','docker.service'],timeout=180)
    deadline = time.monotonic()+60
    while time.monotonic()<deadline:
        result = docker('info','--format','{{json .}}',new=new,check=False,timeout=10)
        if result.returncode == 0: return json.loads(result.stdout)
        time.sleep(1)
    raise RuntimeError('Docker 启动后未能就绪')


def verify_engine(state):
    info = json.loads(docker('info','--format','{{json .}}',new=True).stdout)
    require(info['ServerVersion'] == VERSION and info['Driver'] == 'overlay2' and info['DockerRootDir'] == str(DATA), '升级后版本或存储模式不符')
    pid = int(run(['systemctl','show','docker.service','-p','MainPID','--value']).stdout)
    require(os.readlink('/proc/'+str(pid)+'/exe') == str(RUNTIME/'dockerd'), '当前 dockerd 不是选定的新运行时')
    processes = runtime_processes()
    require(any(Path(exe).name == 'containerd' for _,exe in processes), '未发现新 containerd')
    require(all(exe.startswith(str(RUNTIME)+'/') for _,exe in processes), '新旧容器运行时混用')
    # Legacy CLI is used by the factory NAS and existing plugins.
    require(json.loads(docker('version','--format','{{json .Server}}').stdout)['Version'] == VERSION, '原厂客户端 API 无法连接新引擎')
    restore_running(state,new=True)


def backup_data(folder, state):
    require_data_environment(state)
    require_quiescent(); backup = secure(Path(state['backup']), private=True)
    target = backup/'data'; require(not target.exists(), '备份数据目录已存在')
    target.mkdir(mode=0o700)
    rsync_tree(DATA,target); rsync_tree(DATA,target,verify=True,diagnostic=folder/'backup-verify.diff')
    state['symlinkModePolicy'] = 'exact-v1'
    state['specialNodePolicy'] = 'mknod-v1'
    state['backupDigest'] = tree_digest(target,special_hardlinks=True)
    save(folder,state,'backed-up')


def validate_backup(state):
    backup = secure(remap_data_path(state['backup']), private=True)
    require(filesystem(backup)['fs'] in ('ext4','btrfs','xfs'), '备份文件系统不支持本工具的还原要求')
    require(not backup.is_relative_to(DATA) and not DATA.is_relative_to(backup), '备份与数据目录不能相互包含')
    require(state.get('symlinkModePolicy') == 'exact-v1', '旧备份未保证原符号链接权限，不能用精确恢复模式还原；请保留备份并使用建立该事务的旧版回退程序')
    require(state.get('backupDigest') and tree_digest(backup/'data',special_hardlinks=state.get('specialNodePolicy')=='mknod-v1') == state['backupDigest'], '一致备份丢失或被修改；保留维护状态，拒绝还原')
    return backup/'data'


def require_data_environment(state):
    require([DATA.stat().st_dev, DATA.stat().st_ino] == state['before']['dataIdentity'], 'Docker 数据盘或目录身份已改变，停止操作')
    actual = mountinfo()
    for expected in state['before'].get('externalMounts',[]):
        # Anonymous/FUSE device numbers are allocated anew after reboot. Match
        # their mount point, filesystem, root and source instead of that number.
        keys = ('path','root','fs','source') if expected['device'].startswith('0:') else tuple(expected)
        require(any(all(item.get(key) == expected.get(key) for key in keys) for item in actual),
                '外部数据挂载未恢复：'+expected['path'])


def volume_names(root):
    """Inspect immediate volume directories without walking application data."""
    root=plain(root)
    if not root.exists():return []
    require(root.is_dir(), '卷目录类型异常')
    names=[]
    for path in root.iterdir():
        require(not path.is_symlink(), '卷注册目录含符号链接：'+str(path))
        if path.is_dir():
            require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',path.name) is not None, '卷名称无效')
            payload=plain(path/'_data')
            require(payload.is_dir(), '本地卷数据目录缺失：'+str(payload))
            names.append(path.name)
    return sorted(names)


def local_volumes():
    names=sorted(docker('volume','ls','-q').stdout.split())
    volumes=json.loads(docker('volume','inspect',*names).stdout) if names else []
    require(sorted(v['Name'] for v in volumes)==names, '无法完整读取卷清单')
    for volume in volumes:
        require(volume.get('Driver')=='local' and not volume.get('Options'), '发现远程卷或带挂载选项的卷，需要单独评估迁移')
        require(volume.get('Mountpoint')==str(DATA/'volumes'/volume['Name']/'_data'), '卷挂载路径不符')
    return volumes


def identity(path):
    value=plain(path).stat()
    require(stat.S_ISDIR(value.st_mode), '保留数据路径不是真实目录：'+str(path))
    return [value.st_dev,value.st_ino]


def volume_stash(folder):
    require(ID_PATTERN.fullmatch(folder.name) is not None, '卷保留事务编号无效')
    # DATA may itself be a bind mount. Equal st_dev does not make a rename
    # across mount points atomic: keep both ends beneath the same mount root.
    return DATA/('.minas-rollback-volumes-'+folder.name)


def payload_location(folder, plan, name):
    candidates=[volume_stash(folder)/name/'_data', DATA/'volumes'/name/'_data']
    found=[path for path in candidates if os.path.lexists(path)]
    require(len(found)==1 and identity(found[0])==plan['payloads'][name], '保留卷位置或 inode 异常：'+name)
    return found[0]


def rollback_plan(folder, state, source, restore_all=False, automatic=False):
    mode='all-data' if restore_all else 'keep-volumes'
    existing=state.get('rollbackPlan')
    if existing:
        require(existing['mode']==mode, '已开始的回退不能更换数据模式，请使用原参数继续')
        if mode=='keep-volumes':
            require(volume_names(source/'volumes')==sorted(existing['payloads']), '回退卷清单与备份不符')
            for name in existing['payloads']:payload_location(folder,existing,name)
        return existing
    if restore_all:return dict(mode=mode)
    require(state['phase'] not in ('restoring-data','rollback-failed'), '旧版中断的回退未记录卷保留状态；需先人工评估或显式 --restore-all-data 继续原恢复')
    stash=volume_stash(folder)
    require(not os.path.lexists(stash) and not os.path.lexists(source/stash.name), '发现未登记的卷保留目录，停止操作')
    names=volume_names(source/'volumes')
    require(names==volume_names(DATA/'volumes'), '当前卷清单与升级前不同；默认回退不能删除或丢弃新增/缺失的卷，请先单独迁移')
    if names:
        if automatic:
            volumes=state['before'].get('volumes')
            require(volumes is not None, '事务缺少离线卷清单；不能自动推断卷类型')
        else:
            volumes=local_volumes()
            current={c['Id']:c for c in containers()}
            require(set(current)=={c['id'] for c in state['before']['containers']}, '容器已新增、删除或重建；请先单独迁移再保留卷回退')
            for item in state['before']['containers']:
                old=read(source/'containers'/item['id']/'config.v2.json')
                require(old['Image']==current[item['id']]['Image'], '容器镜像已改变，不能将当前卷直接交给旧应用：'+item['id'])
        require(sorted(v['Name'] for v in volumes)==names, '卷注册信息与数据目录不符')
        require(all(v.get('Driver')=='local' and not v.get('Options') for v in volumes), '卷类型不支持保留数据回退')
    root=DATA/'volumes'
    return dict(mode=mode, volumeRoot=identity(root) if root.exists() else None,
                payloads={name:identity(root/name/'_data') for name in names})


def durable_rename(source, target):
    plain(source);plain(target)
    require(not os.path.lexists(target), '拒绝覆盖保留数据：'+str(target))
    os.rename(source,target)
    for parent in {source.parent,target.parent}:
        fd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)


def restore_keep_volumes(folder, state, source):
    plan=state['rollbackPlan'];root=DATA/'volumes';stash=volume_stash(folder)
    excluded_root=('volumes',stash.name)
    require_quiescent()
    if plan['volumeRoot'] is not None:
        if not stash.exists():
            require(identity(root)==plan['volumeRoot'], '卷根目录身份已改变')
            require(volume_names(root)==sorted(plan['payloads']), '停止服务前后卷清单发生变化，拒绝继续')
            durable_rename(root,stash)
        require(identity(stash)==plan['volumeRoot'], '卷保留目录身份已改变')
        require(stash.stat().st_uid==0, '卷保留目录必须由 root 拥有')
        stash.chmod(0o700)
    for name in plan['payloads']:payload_location(folder,plan,name)
    if (source/'volumes').exists() and not root.exists():root.mkdir(mode=0o700)
    # The whole live volume tree is outside rsync's delete scope. Restore the
    # old volume registry separately while excluding every application's _data.
    rsync_tree(source,DATA,exclude=excluded_root)
    if (source/'volumes').exists():
        excluded=tuple(Path(name)/'_data' for name in sorted(plan['payloads']))
        rsync_tree(source/'volumes',root,exclude=excluded)
        for name in plan['payloads']:
            path=payload_location(folder,plan,name);destination=root/name/'_data'
            if path!=destination:durable_rename(path,destination)
        # Renaming payloads changes parent mtimes. The excluded trees remain
        # untouched while restoring/validating just the surrounding metadata.
        rsync_tree(source/'volumes',root,exclude=excluded)
        rsync_tree(source/'volumes',root,verify=True,exclude=excluded,diagnostic=folder/'restore-volumes-verify.diff')
    rsync_tree(source,DATA,verify=True,exclude=excluded_root,diagnostic=folder/'restore-verify.diff')
    for name in plan['payloads']:
        require(payload_location(folder,plan,name)==root/name/'_data', '卷尚未归位：'+name)


def cleanup_new_forward_rules():
    # Only these exact Docker-owned hooks are removed. Never flush a table or
    # touch independent NAS/NAT66 rules. The factory daemon rebuilds its hooks.
    prefix=['iptables','-w','5']
    known=[['-A','FORWARD','-j','DOCKER-FORWARD'],
           ['-A','FORWARD','-m','set','--match-set','docker-ext-bridges-v4','dst','-j','DOCKER'],
           ['-A','FORWARD','-m','set','--match-set','docker-ext-bridges-v4','dst','-m','conntrack','--ctstate','RELATED,ESTABLISHED','-j','ACCEPT']]
    for line in run(prefix+['-S','FORWARD']).stdout.splitlines():
        rule=shlex.split(line)
        if rule in known:run(prefix+['-D',*rule[1:]])


def rollback_check(folder,state,restore_all=False,automatic=False):
    require(state['phase'] != 'rolled-back', '该事务已经回退')
    owned_files_valid()
    require_data_environment(state)
    source = validate_backup(state) if state.get('dataTouched') else None
    plan=rollback_plan(folder,state,source,restore_all,automatic) if source is not None else None
    return source,plan


def rollback(folder, state, restore_all=False, automatic=False):
    source,plan=rollback_check(folder,state,restore_all,automatic)
    if plan is not None:
        state['rollbackPlan']=plan;save(folder,state)
    startup_hook_valid(state['id'])
    guards(True)
    try:
        # Stop containers gracefully before stopping the engine, even if the new
        # daemon is only partly functional. If it cannot stop cleanly, fail closed.
        if active('docker.service'):
            cli_new = state.get('dataTouched',False)
            ids = docker('ps','-q','--no-trunc',new=cli_new,check=False).stdout.split()
            if ids: docker('stop','--time','30',*ids,new=cli_new,timeout=180)
        stop_engine()
        if BOOT_CRON.exists():startup_hook(state['id'],False)
        if source is not None:
            save(folder,state,'restoring-data')
            if restore_all:
                rsync_tree(source,DATA);rsync_tree(source,DATA,verify=True,diagnostic=folder/'restore-verify.diff')
            else:restore_keep_volumes(folder,state,source)
            cleanup_new_forward_rules()
        if DROPIN.exists(): DROPIN.unlink()
        run(['systemctl','daemon-reload'])
        start_engine(); restore_running(state)
        for path in GUARDS: path.unlink(missing_ok=True)
        run(['systemctl','daemon-reload']); save(folder,state,'rolled-back')
        detail = ('已恢复原厂引擎和原容器运行集合；'+('全部 Docker 数据恢复到升级前。' if restore_all else '本地卷数据保持回退时状态，镜像及容器层恢复到升级前。')) if state.get('dataTouched') else '已恢复原厂引擎和原容器运行集合；正式数据未被新引擎修改。'
        print(detail+'备份保留：'+str(remap_data_path(state['backup'])),flush=True)
    except BaseException as error:
        guards(True); save(folder,state,'rollback-failed')
        raise RuntimeError('回退未完成，Docker 已保留维护锁；不要删除备份，修正问题后用同一事务编号重试：'+str(error)) from error


def preflight_backup(parent, required):
    parent = secure(Path(parent))
    require(filesystem(parent)['fs'] in ('ext4','btrfs','xfs'), '备份请选择真实 ext4/btrfs/xfs 目录，不使用 FUSE、tmpfs 或网络盘')
    require(not parent.is_relative_to(DATA) and not DATA.is_relative_to(parent), '备份目录与数据目录不能相互包含')
    require(shutil.disk_usage(parent).free > required, '备份空间不足')
    # Check that backup storage actually preserves metadata before downtime.
    secure(DATA.parent)
    require(DATA.parent.stat().st_dev == DATA.stat().st_dev, '数据根的父目录不在同一文件系统，无法安全完成精确权限预检')
    with tempfile.TemporaryDirectory(prefix='.minas-backup-check-',dir=parent) as temporary, \
         tempfile.TemporaryDirectory(prefix='.minas-source-check-',dir=DATA.parent) as source_temporary:
        source=Path(source_temporary)/'source';dest=Path(temporary)/'dest';source.mkdir();dest.mkdir()
        file=source/'probe';file.write_bytes(b'backup-check');os.setxattr(file,'user.minas-backup',b'yes')
        os.link(file,source/'hardlink');(source/'symlink').symlink_to('probe')
        for mode in (0o755,0o700,0o777):
            previous=os.umask(0o777 ^ mode)
            try:(source/('link-'+oct(mode))).symlink_to('probe')
            finally:os.umask(previous)
        os.link(source/'symlink',source/'symlink-hardlink',follow_symlinks=False)
        deep=source/('node-probe-'+'x'*100);deep.mkdir()
        os.mknod(deep/'socket',stat.S_IFSOCK|0o660);os.mkfifo(deep/'fifo',0o640)
        os.link(deep/'socket',source/'socket-hardlink');os.link(deep/'fifo',source/'fifo-hardlink')
        rsync_tree(source,dest);rsync_tree(source,dest,verify=True)
        require(tree_digest(source,special_hardlinks=True)==tree_digest(dest,special_hardlinks=True), '备份预检的完整元数据指纹不一致')
        require((dest/'probe').stat().st_ino == (dest/'hardlink').stat().st_ino and os.getxattr(dest/'probe','user.minas-backup') == b'yes', '备份存储不保留硬链接或扩展属性')
        require((dest/'symlink').lstat().st_ino == (dest/'symlink-hardlink').lstat().st_ino, '备份存储不保留符号链接的硬链接关系')
    return parent


@contextlib.contextmanager
def probe_paths():
    root=Path(tempfile.mkdtemp(prefix='.probe-',dir=BASE))
    runroot=Path(tempfile.mkdtemp(prefix='minas-docker-probe-',dir='/run'))
    cleanup={'safe':True}
    try:yield root,runroot,cleanup
    finally:
        if cleanup['safe']:
            shutil.rmtree(root);shutil.rmtree(runroot)
        else:
            print('隔离进程未确认退出，保留其临时目录供处理：'+str(root)+' '+str(runroot),file=sys.stderr)


def probe_engine():
    verify_runtime()
    with probe_paths() as (root,runroot,cleanup):
        socket=runroot/'docker.sock'
        config=dict(hosts=['unix://'+str(socket)], **{'data-root':str(root/'data'),'exec-root':str(runroot/'exec'),
                    'pidfile':str(runroot/'pid'),'bridge':'none','iptables':False,'ip6tables':False,'ip-forward':False,
                    'ip-masq':False,'userland-proxy':False,'storage-driver':'vfs','features':{'containerd-snapshotter':False}})
        atomic(root/'daemon.json',config)
        env=dict(ENV,PATH=str(RUNTIME)+':'+ENV['PATH'])
        with (root/'daemon.log').open('wb') as output:
            child=subprocess.Popen([str(RUNTIME/'dockerd'),'--config-file='+str(root/'daemon.json')],stdout=output,stderr=output,
                                   env=env,start_new_session=True)
            cleanup['safe']=False
            try:
                deadline=time.monotonic()+60
                while time.monotonic()<deadline:
                    require(child.poll() is None, '隔离引擎提前退出；未切换原引擎')
                    result=run([RUNTIME/'docker','--host=unix://'+str(socket),'info','--format','{{json .}}'],check=False,timeout=5,runtime=True)
                    if not result.returncode:
                        info=json.loads(result.stdout);require(info['ServerVersion']==VERSION and info['DockerRootDir']==str(root/'data'), '隔离引擎信息不符')
                        print('隔离引擎基础启动测试通过；未创建容器或 bridge。',flush=True);break
                    time.sleep(1)
                else:raise RuntimeError('隔离引擎启动超时')
            finally:
                # The daemon can exit before its managed containerd. Drain the
                # whole private process group before removing probe data.
                def members():
                    result=[]
                    for entry in Path('/proc').glob('[0-9]*'):
                        try:
                            if os.getpgid(int(entry.name)) == child.pid:result.append(int(entry.name))
                        except ProcessLookupError:pass
                    return result
                try:os.killpg(child.pid,signal.SIGTERM)
                except ProcessLookupError:pass
                try:child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=10)
                deadline=time.monotonic()+10
                while members() and time.monotonic()<deadline:time.sleep(.2)
                if members():
                    try:os.killpg(child.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                    time.sleep(.5)
                require(not members(),'隔离测试进程未退出，停止后再清理测试目录')
                cleanup['safe']=True


def install(args):
    before=inspect_system()
    require(before['version']=='20.10.17', '此迁移脚本仅针对已评估的原厂 20.10.17；其他版本先单独评估')
    require(not DROPIN.exists() and not MAINTENANCE.exists() and not any(p.exists() for p in GUARDS), '已有切换或维护配置，先查看状态/完成回退')
    require(not BOOT_CRON.exists(),'已有开机校正配置，先查看原事务状态')
    require(args.bind_backups_confirmed or not before['externalWritableBinds'], '存在外部可写挂载，请备份后添加 --bind-backups-confirmed；--check 会列出路径')
    owned_files_valid()
    size=int(run(['du','-skx',DATA],timeout=300).stdout.split()[0])*1024
    parent=preflight_backup(args.backup_dir,int(size*1.25)+512*1024*1024)
    prepare(args.archive,args.proxy); render_config(); probe_engine()
    if not args.yes:
        require(sys.stdin.isatty(), '非交互安装需明确添加 --yes')
        require(input('将停止容器并备份/切换 Docker。输入 INSTALL 继续：').strip()=='INSTALL', '已取消')
    secure(BASE/'transactions',create=True)
    ident=time.strftime('%Y%m%d-%H%M%S')+'-'+os.urandom(4).hex()
    folder=secure(BASE/'transactions'/ident,create=True,private=True)
    backup=secure(parent/('docker-before-'+ident),create=True,private=True)
    state=dict(id=ident,targetVersion=VERSION,phase='captured',before=before,backup=str(backup),dataTouched=False)
    atomic(WORKER,Path(__file__).read_bytes(),0o700);save(folder,state)
    print('事务编号：'+ident,flush=True)
    try:
        guards(True)
        # Disarm policies before the consistent backup. Restore from the journal
        # afterwards; this avoids auto-starting deliberately stopped containers.
        save(folder,state,'disarming-policies')
        for item in before['containers']:docker('update','--restart=no',item['id'])
        ids=[c['id'] for c in before['containers'] if c['running']]
        if ids:docker('stop','--time','30',*ids,timeout=180)
        stop_engine();save(folder,state,'stopped');backup_data(folder,state)
        secure(DROPIN.parent,create=True);atomic(DROPIN,dropin_bytes(),0o644)
        run(['systemctl','daemon-reload'])
        state['dataTouched']=True;save(folder,state,'starting-new')
        require_data_environment(state)
        start_engine(new=True);restore_running(state,new=True);verify_engine(state)
        save(folder,state,'installed')
        startup_hook(state['id'],True)
        print('Docker '+VERSION+' 已切换并通过基础校验。请继续验证网络、Portainer、智能中枢和重启。\n'
              '手动回退：python3 -B '+str(WORKER)+' --rollback '+ident+'\n'
              '默认回退保留本地卷数据，恢复升级前镜像、容器层和引擎元数据；外部挂载不作还原。',flush=True)
    except BaseException:
        save(folder,state,'install-failed')
        try:rollback(folder,state,automatic=True)
        except BaseException as error:print('自动回退未完成：'+str(error)+'；事务 '+ident,file=sys.stderr,flush=True)
        raise


@contextlib.contextmanager
def locks():
    secure(BASE,create=True,private=True)
    with contextlib.ExitStack() as stack:
        for path in (BASE/'upgrade.lock',Path('/data/minas/privileged/component.lock'),Path('/data/minas/privileged/docker.lock'),Path('/data/minas/docker/network/lock'),Path('/data/plugin/.local-plugin-installer.lock')):
            if not path.parent.exists():continue
            secure(path.parent)
            fd=os.open(plain(path),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
            stack.callback(os.close,fd);value=os.fstat(fd)
            require(stat.S_ISREG(value.st_mode) and value.st_uid==0 and not value.st_mode&0o022,'操作锁权限异常')
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('Docker、网络修复、插件安装或定时任务正在操作，请稍后重试')
        yield


def boot_reconcile(ident):
    # Cron reads /etc after the overlay is mounted, unlike PID 1's initial
    # unit transaction. Wait only for boot mounts/API/operation locks, bounded
    # by both this deadline and the outer timeout in the fixed Cron command.
    deadline=time.monotonic()+300
    while True:
        try:
            with locks():
                folder,state=transaction(ident)
                if MAINTENANCE.exists():
                    run(['systemctl','daemon-reload'])
                    run(['systemctl','stop','docker.socket','docker.service'],timeout=180)
                    raise RuntimeError('维护保护仍在，已停止开机误启动的引擎；请完成原安装或回退事务')
                require_data_environment(state)
                if not active('docker.service'):
                    raise RuntimeError('等待 Docker 开机服务')
                selection=runtime_selection()
                if selection['serverVersion'] is None:raise RuntimeError('等待 Docker 开机接口')
                result=repair_startup(folder,state,automatic=True)
                atomic(BASE/'startup-last.json',dict(id=ident,ok=True,time=int(time.time()),
                       bootId=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),**result))
                print(json.dumps(result,ensure_ascii=False));return
        except (RuntimeError,OSError,ValueError,subprocess.SubprocessError) as error:
            retry=any(text in str(error) for text in ('正在操作，请稍后重试','外部数据挂载未恢复','等待 Docker 开机'))
            if retry and time.monotonic()<deadline:
                time.sleep(2);continue
            atomic(BASE/'startup-last.json',dict(id=ident,ok=False,time=int(time.time()),error=str(error)))
            raise


def status():
    result=dict(targetVersion=VERSION,defaultRollbackMode='keep-volumes',runtimePrepared=RUNTIME.exists(),maintenance=MAINTENANCE.exists(),
                startupHookInstalled=BOOT_CRON.is_file(),runtimeSelection=runtime_selection(),transactions=[])
    directory=BASE/'transactions'
    if directory.exists():
        secure(directory)
        for path in sorted(directory.iterdir()):
            if path.is_dir() and ID_PATTERN.fullmatch(path.name):
                _,state=transaction(path.name)
                item={k:state.get(k) for k in ('id','phase','backup','targetVersion')}
                if state.get('backup'):item['backupResolved']=str(remap_data_path(state['backup']))
                item['startupHookMatches']=BOOT_CRON.is_file() and not BOOT_CRON.is_symlink() and BOOT_CRON.read_bytes()==startup_cron_bytes(state['id'])
                result['transactions'].append(item)
    last=BASE/'startup-last.json'
    if last.exists():result['lastStartupRepair']=read(last)
    print(json.dumps(result,ensure_ascii=False,indent=2))


def main(argv=None):
    parser=argparse.ArgumentParser(prog='upgrade-docker-29.9.2.sh',description='小米 NAS Docker 29.9.2 安装与一致数据回退；默认只读检查')
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--check',action='store_true',help='只读检查当前引擎、配置及外部数据挂载')
    group.add_argument('--prepare',action='store_true',help='下载和验证运行时，不切换引擎')
    group.add_argument('--probe',action='store_true',help='准备运行时并启动独立测试引擎；不接管现有数据或网络')
    group.add_argument('--install',action='store_true',help='备份数据并切换；失败自动尝试回退')
    group.add_argument('--rollback',metavar='ID',help='恢复原厂引擎及旧元数据，默认保留当前本地卷数据')
    group.add_argument('--rollback-check',metavar='ID',help='只读校验备份及回退兼容性，不停止服务或写入事务')
    group.add_argument('--status',action='store_true',help='只读查看事务与维护状态')
    group.add_argument('--repair-startup',metavar='ID',help='为成功升级事务补装开机一次性校正；必要时短暂停止容器切回目标引擎，不还原数据')
    group.add_argument('--boot-reconcile',metavar='ID',help=argparse.SUPPRESS)
    group.add_argument('--render-config',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--backup-dir',type=Path,help='已存在、由 root 管理的 ext4/btrfs/xfs 备份父目录')
    parser.add_argument('--archive',type=Path,help='离线官方 ARM64 包，仍核对固定 SHA-256')
    parser.add_argument('--proxy',help='仅下载使用的 HTTP/HTTPS 代理，例如 http://127.0.0.1:7890')
    parser.add_argument('--bind-backups-confirmed',action='store_true',help='明确确认已备份外部可写挂载数据')
    parser.add_argument('--restore-all-data',action='store_true',help='与回退参数合用，明确将本地卷也覆盖为升级前数据')
    parser.add_argument('--yes',action='store_true',help='明确允许安装/回退/开机修复中断容器，跳过交互输入')
    args=parser.parse_args(argv)
    if args.install and args.backup_dir is None:parser.error('--install 必须指定 --backup-dir')
    if args.yes and not (args.install or args.rollback or args.repair_startup):parser.error('--yes 仅用于安装、回退或开机修复')
    if args.restore_all_data and not (args.rollback or args.rollback_check):parser.error('--restore-all-data 仅用于回退或回退检查')
    host_namespace()
    if args.boot_reconcile:boot_reconcile(args.boot_reconcile);return
    if args.render_config:
        require(not MAINTENANCE.exists(),'Docker 处于维护状态');render_config();return
    if args.status:status();return
    if args.rollback_check:
        folder,state=transaction(args.rollback_check)
        _,plan=rollback_check(folder,state,args.restore_all_data)
        print(json.dumps(dict(id=state['id'],backupVerified=bool(state.get('dataTouched')),mode=plan['mode'] if plan else 'untouched',
                              preservedVolumes=sorted(plan.get('payloads',{})) if plan else [],servicesChanged=False),ensure_ascii=False,indent=2));return
    if not (args.prepare or args.probe or args.install or args.rollback or args.repair_startup):
        print(json.dumps(inspect_system(),ensure_ascii=False,indent=2));return
    if str(BASE)=='/data/minas/docker/runtime':ensure_data_layout()
    if args.backup_dir is not None:args.backup_dir=remap_data_path(args.backup_dir)
    if args.archive is not None:args.archive=remap_data_path(args.archive)
    require(not Path('/data/minas/migration/maintenance').exists(),'目录迁移正在进行')
    with locks():
        if args.install:install(args)
        elif args.repair_startup:
            folder,state=transaction(args.repair_startup)
            require(state['phase']=='installed','需要已成功安装的升级事务')
            if MAINTENANCE.exists():
                plan=read(BASE/'startup-repair.json') if (BASE/'startup-repair.json').exists() else {}
                require(plan.get('id')==state['id'] and plan.get('phase') in ('captured','switching','failed'),
                        '维护锁属于其他操作，不能用开机修复解除')
            owned_files_valid();startup_hook_valid(state['id']);require_data_environment(state);verify_runtime(RUNTIME)
            require(DROPIN.is_file(),'升级启动配置缺失')
            if not args.yes:
                require(sys.stdin.isatty(),'非交互开机修复需 --yes')
                require(input('将保留当前数据并校正 Docker 启动，可能短暂停止容器。输入 REPAIR 继续：').strip()=='REPAIR','已取消')
            atomic(WORKER,Path(__file__).read_bytes(),0o700)
            startup_hook(state['id'],True)
            print(json.dumps(repair_startup(folder,state),ensure_ascii=False,indent=2))
        elif args.rollback:
            folder,state=transaction(args.rollback)
            if not args.yes:
                require(sys.stdin.isatty(),'非交互回退需 --yes')
                word='RESTORE-ALL-DATA' if args.restore_all_data else 'ROLLBACK'
                text='全部 Docker 数据（包括卷）将覆盖为升级前状态。' if args.restore_all_data else '保留当前本地卷，镜像及容器层恢复到升级前。'
                require(input(text+'将停止容器，输入 '+word+' 继续：').strip()==word,'已取消')
            rollback(folder,state,args.restore_all_data)
        else:
            prepare(args.archive,args.proxy)
            if args.probe:probe_engine()


if __name__=='__main__':
    try:main()
    except (RuntimeError,OSError,ValueError,subprocess.SubprocessError,KeyboardInterrupt) as error:
        print('错误：'+str(error),file=sys.stderr);sys.exit(1)
MINAS_DOCKER_UPGRADE_PY
python3 -B "$worker" "$@"
