"""One tool's transactional directory handover; no packaged foreign programs."""
DATA_LAYOUT_COPY_PATHS = {}
DATA_LAYOUT_PROGRAM = ''


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
    """Copy required shared state without removing a legacy plugin's files."""
    import os,shutil,stat
    from pathlib import Path
    for old,new in DATA_LAYOUT_COPY_PATHS.items():
        old,new=Path(old),Path(new)
        if not old.exists():continue
        _layout_secure(old)
        for p in old.rglob('*'):
            relative=p.relative_to(old);target=new/relative;s=p.lstat()
            if DATA_LAYOUT_SCOPE=='ssh':
                if relative.parts[0] not in ('keys','host') and relative.as_posix()!='operations.lock':continue
                if relative.parts[0]=='keys' and len(relative.parts)>1 and p.suffix!='.pub':continue
            if stat.S_ISLNK(s.st_mode) or s.st_uid!=0:raise ValueError('unsafe shared SSH state')
            if p.is_dir():
                target.mkdir(mode=stat.S_IMODE(s.st_mode),parents=True,exist_ok=True);_layout_secure(target);continue
            if not stat.S_ISREG(s.st_mode):raise ValueError('unsupported shared SSH state')
            target.parent.mkdir(mode=0o700,parents=True,exist_ok=True);_layout_secure(target.parent)
            if target.exists():continue
            if p.name.endswith('.lock'):os.link(p,target)
            else:
                shutil.copy2(p,target);os.chown(target,s.st_uid,s.st_gid)


def ensure_data_layout():
    _layout_initialize()
    _layout_tool_migrate()
