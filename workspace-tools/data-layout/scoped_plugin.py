"""Finalize only the current user's plugin after new source/recovery verification."""
DATA_LAYOUT_PLUGIN = ''
DATA_LAYOUT_PRIVATE_FILES = []
DATA_LAYOUT_FLAT_FILES = []


def _layout_empty_parents(path,stop):
    from pathlib import Path
    path,stop=Path(path),Path(stop)
    while path!=stop and path.is_relative_to(stop):
        try:path.rmdir()
        except OSError:break
        path=path.parent


def _layout_remove_record(path):
    import shutil,stat
    from pathlib import Path
    path=Path(path)
    if not path.exists():return
    _layout_secure(path)
    # A recovery snapshot may contain its official src/INFO link. rmtree never
    # follows it; reject user-controlled directories before touching the record.
    for node in path.rglob('*'):
        s=node.lstat()
        if not stat.S_ISLNK(s.st_mode) and s.st_uid!=0:raise ValueError('unsafe legacy recovery record')
    shutil.rmtree(path)


def _layout_retire_public(base,known,refs):
    """Only known public files are disposable after every private owner left."""
    import os,stat
    from pathlib import Path
    base=Path(base)
    if refs or not base.exists():return False
    _layout_secure(base)
    # Do not delete a program still in use by an old process.
    for proc in Path('/proc').glob('[0-9]*/cmdline'):
        try:args=proc.read_bytes().split(b'\0')
        except OSError:continue
        if any(arg.startswith(os.fsencode(base)+b'/') for arg in args):return False
    remaining=[]
    for parent,dirs,files in os.walk(base,followlinks=False):
        for name in files:
            p=Path(parent)/name;rel=p.relative_to(base).as_posix();s=p.lstat()
            if rel not in known or not stat.S_ISREG(s.st_mode) or s.st_uid!=0:remaining.append(rel)
        for name in dirs:
            if (Path(parent)/name).is_symlink():remaining.append(str(Path(parent)/name))
    if remaining:return False
    for rel in known:
        p=base/rel
        if p.is_file():p.unlink()
    for p in sorted(base.rglob('*'),key=lambda p:len(p.parts),reverse=True):
        if p.is_dir():
            try:p.rmdir()
            except OSError:pass
    try:base.rmdir();return True
    except OSError:return False


def _layout_retire_copied(folder,target,receipts):
    import hashlib
    if not folder.is_dir():return
    _layout_secure(folder)
    for p in folder.rglob('*'):
        q=target/p.relative_to(folder)
        if p.is_file() and not p.is_symlink():
            proof=receipts.get(str(p),{})
            proved=proof.get('target')==str(q) and proof.get('sha256')==hashlib.sha256(p.read_bytes()).hexdigest()
            if proved or (q.is_file() and not q.is_symlink() and p.read_bytes()==q.read_bytes()):p.unlink()
    for p in sorted(folder.rglob('*'),key=lambda p:len(p.parts),reverse=True):
        if p.is_dir() and not p.is_symlink():
            try:p.rmdir()
            except OSError:pass
    try:folder.rmdir()
    except OSError:pass


def finish_plugin_layout(user):
    import fcntl,hashlib,json,os,re,stat,time
    from pathlib import Path
    plugin=DATA_LAYOUT_PLUGIN
    if not re.fullmatch(r'u[0-9]+',user):raise ValueError('invalid migration user')
    root=Path(DATA_LAYOUT_ROOT)
    receipts={}
    receipt_dir=root/'privileged/migration'/plugin
    for receipt in receipt_dir.glob('u*.json'):
        _layout_secure(receipt.parent)
        receipts.update(json.loads(receipt.read_text()).get('files',{}))
    record=root/'plugins/recovery/records'/(user+'-'+plugin)
    _layout_secure(record)
    current=(record/'current').read_text().strip()
    if not re.fullmatch(r'generation-[0-9]+',current):raise ValueError('invalid new recovery generation')
    saved=record/current
    value=json.loads((saved/'record.json').read_text())
    if value.get('user')!=user or value.get('plugin')!=plugin:raise ValueError('new recovery identity mismatch')
    home=Path('/home')/user/'plugin'/plugin
    def digest(src):
        out=hashlib.sha256();files=[]
        for parent,dirs,names in os.walk(src,followlinks=False):
            files.extend(Path(parent)/name for name in names if stat.S_ISREG((Path(parent)/name).lstat().st_mode))
        for p in sorted(files,key=lambda p:os.fsencode(str(p))):out.update((hashlib.sha256(p.read_bytes()).hexdigest()+'\n').encode())
        return out.hexdigest()
    info=json.loads((home/'INFO').read_text())
    if digest(home/'src')!=info['abstract'] or digest(saved/'src')!=info['abstract']:
        raise ValueError('new source/recovery not verified; legacy files retained')
    _layout_remove_record(Path('/data/.minas-plugin-recovery/records')/(user+'-'+plugin))
    # The new installer generated its own code. Only its old grant is removed;
    # the public old helper and every other user's/plugin's grant stay in place.
    old=Path('/data/.minas-privileged')
    if old.exists():
        _layout_secure(old)
        lock=old/'component.lock';fd=os.open(lock,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            s=os.fstat(fd)
            if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('unsafe old component lock')
            deadline=time.monotonic()+15
            while True:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>=deadline:raise ValueError('旧组件忙，当前插件已安装；保留旧文件以便下次清理')
                    time.sleep(.05)
            registry=old/'registry.json'
            data=json.loads(registry.read_text()) if registry.is_file() else dict(users={},modules={},packages={})
            grants=data.get('users',{}).get(user,[])
            data.setdefault('users',{})[user]=[p for p in grants if p!=plugin]
            if not data['users'][user]:data['users'].pop(user,None)
            data.get('settings',{}).get(user,{}).pop(plugin,None)
            remaining=any(plugin in grants for grants in data.get('users',{}).values())
            if not remaining:
                data.get('modules',{}).pop(plugin,None);data.get('packages',{}).pop(plugin,None)
            if registry.exists():_layout_atomic(registry,json.dumps(data,ensure_ascii=False,indent=2)+'\n')
            if plugin=='taskcenter':
                receipt=receipt_dir/(user+'.json')
                if receipt.is_file():
                    value=json.loads(receipt.read_text())
                    if 'pendingScheduler' in value and not value.get('schedulerActivated'):
                        new_task=root/'privileged/users'/user/'state.json'
                        current_state=json.loads(new_task.read_text());current_state['scheduler']=value['pendingScheduler']
                        _layout_atomic(new_task,json.dumps(current_state,ensure_ascii=False,indent=2)+'\n')
                        value['schedulerActivated']=True;_layout_atomic(receipt,json.dumps(value,indent=2)+'\n')
                folder=old/'users'/user
                _layout_retire_copied(folder,root/'privileged/users'/user,receipts)
                task_lock=folder/'lock'
                if folder.is_dir() and list(folder.iterdir())==[task_lock] and task_lock.is_file() and not task_lock.is_symlink() and task_lock.stat().st_size==0:
                    task_lock.unlink();folder.rmdir()
            if plugin=='sshrecovery':
                ssh_worker=Path('/data/ssh-autostart-fix/restore-ssh.sh')
                ssh_compat=ssh_worker.is_file() and ssh_worker.read_text()=='#!/bin/sh\n# minas-ssh-layout-forwarder\nexec /bin/sh /data/minas/ssh/restore-ssh.sh "$@"\n'
                ssh_standalone=ssh_worker.exists() and not ssh_compat
            if plugin=='sshrecovery' and not ssh_standalone:
                for folder in (old/'modules/sshrecovery/var',old/'sshrecovery'):
                    for suffix in ('.pub','.settings.json'):
                        key=folder/'keys'/(user+suffix);target=root/'privileged/modules/sshrecovery/var/keys'/(user+suffix)
                        proof=receipts.get(str(key),{})
                        if key.is_file() and target.is_file() and (key.read_bytes()==target.read_bytes() or proof.get('sha256')==hashlib.sha256(key.read_bytes()).hexdigest()):key.unlink()
                    _layout_retire_copied(folder/'tmp'/('generated-'+user),root/'privileged/modules/sshrecovery/var/tmp'/('generated-'+user),receipts)
            if not remaining:
                for name in DATA_LAYOUT_PRIVATE_FILES:
                    p=old/'modules'/plugin/name
                    if p.is_file() and not p.is_symlink():p.unlink()
                for name in DATA_LAYOUT_FLAT_FILES:
                    p=old/name
                    if p.is_file() and not p.is_symlink():p.unlink()
                # Root-owned task/SSH state is retired only after exact copies
                # exist at the new component; unknown/newer data is preserved.
                if plugin=='sshrecovery' and not ssh_standalone:
                    for leaf in (old/'modules'/plugin/'var',old/'sshrecovery'):
                        _layout_retire_copied(leaf,root/'privileged/modules'/plugin/'var',receipts)
                    if ssh_compat:
                        ssh_worker.unlink()
                        try:ssh_worker.parent.rmdir()
                        except OSError:pass
                _layout_empty_parents(old/'modules'/plugin,old)
                # Namespace-separated new Cron never replaces this old file.
                cron=Path('/etc/cron.d/minas-privileged')
                if cron.is_file():
                    previous=cron.read_text();lines=[]
                    for line in previous.splitlines(keepends=True):
                        own=(str(old/'minas-helper')+' '+plugin+' ') in line
                        own=own or any(str(old/name) in line for name in DATA_LAYOUT_FLAT_FILES)
                        if not own:lines.append(line)
                    if ''.join(lines)!=previous:_layout_atomic(cron,''.join(lines),0o644)
        finally:os.close(fd)
        public={'minas-helper','component_admin.py','privileged_runtime.py','user_state_io.py','common/privileged_runtime.py','common/component_admin.py',
                'common/user_state_io.py','common/telemetry_cache.py','registry.json','component.lock','docker.lock'}
        refs=bool(data.get('users') or data.get('modules') or data.get('packages'))
        _layout_retire_public(old,public,refs)
    # Source data has already been individually moved by persist_state().
    legacy_data=Path('/data/.minas-plugin-data')
    _layout_empty_parents(legacy_data/user/plugin,legacy_data)
    try:legacy_data.rmdir()
    except OSError:pass
    recovery=Path('/data/.minas-plugin-recovery')
    if recovery.exists():
        _layout_remove_record(recovery/'records'/(user+'-'+plugin))
        _layout_empty_parents(recovery/'records',recovery)
        # Public recovery files are retained until all records have migrated.
        if not (recovery/'records').exists():
            import subprocess
            for name in ('minas-plugin-recovery-watch.service','minas-plugin-recovery.service','minas-plugin-recovery.timer'):
                unit=Path('/etc/systemd/system')/name
                if unit.is_file() and str(recovery) in unit.read_text():
                    subprocess.run(['systemctl','disable','--now',name],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                    unit.unlink()
            cron=Path('/etc/cron.d/minas-plugin-recovery')
            if cron.is_file() and str(recovery) in cron.read_text():cron.unlink()
            _layout_retire_public(recovery,{'recovery.py','lock','recovery.lock','watch.lock'},False)
    receipt=receipt_dir/(user+'.json')
    if receipt.is_file():
        value=json.loads(receipt.read_text())
        if plugin=='taskcenter':value.pop('originalTask',None);value.pop('paused',None)
        value['activated']=True;_layout_atomic(receipt,json.dumps(value,indent=2)+'\n')
    print('已完成当前插件迁移；其他使用者和无法确认用途的旧文件已保留：'+user+'/'+plugin)


def abort_plugin_layout(user):
    import base64,hashlib,json,re
    from pathlib import Path
    if not re.fullmatch(r'u[0-9]+',user or ''):raise ValueError('invalid migration user')
    receipt=Path(DATA_LAYOUT_ROOT)/'privileged/migration'/DATA_LAYOUT_PLUGIN/(user+'.json')
    if not receipt.exists():return
    _layout_secure(receipt.parent);value=json.loads(receipt.read_text())
    task=Path('/data/.minas-privileged/users')/user/'state.json'
    if not value.get('schedulerActivated') and value.get('paused') and task.is_file() and hashlib.sha256(task.read_bytes()).hexdigest()==value['paused']:
        _layout_atomic(task,base64.b64decode(value['originalTask']))
        value.pop('paused',None);value.pop('originalTask',None)
        _layout_atomic(receipt,json.dumps(value,indent=2)+'\n')
