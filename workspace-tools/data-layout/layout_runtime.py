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


if __name__=='__main__':ensure_data_layout()
