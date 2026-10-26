"""Installer-owned migration of protected media state; never touches the media DB."""
import fcntl,hashlib,json,os,re,stat,sys
from pathlib import Path

BASE=Path('/data/minas/privileged')

def sync_directories(*paths):
    for path in paths:
        if not path.exists():continue
        fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(fd)
        finally:os.close(fd)

def load_store():
    if os.geteuid()!=0:raise PermissionError('迁移必须由 root 安装器执行')
    sys.dont_write_bytecode=True
    sys.path[:0]=[str(BASE/'common'),str(BASE/'modules/mediaeditor')]
    import store
    return store

def fingerprint(path):
    """Validate ownership and every entry; never follow links during migration."""
    result={}
    for node in [path,*sorted(path.rglob('*'))]:
        meta=node.lstat()
        if meta.st_uid!=0 or meta.st_gid!=0 or meta.st_mode&0o077 or not (stat.S_ISDIR(meta.st_mode) or stat.S_ISREG(meta.st_mode)):
            raise ValueError('受保护目录中存在不安全文件：'+str(node))
        result[str(node.relative_to(path))]=[stat.S_IMODE(meta.st_mode),meta.st_dev,meta.st_ino,
            hashlib.sha256(node.read_bytes()).hexdigest() if stat.S_ISREG(meta.st_mode) else None]
    return result

def migrate(store):
    """Caller holds the exclusive common component lock; resume a journal safely."""
    if os.geteuid()!=0:raise PermissionError('root required')
    runtime=store.runtime;old=store.LEGACY_STATE;locks=store.LOCK_STATE
    runtime.secure_directory(locks,create=True,mode=0o700)
    journal=locks/'storage-layout.json';old_lock=old/'write.lock';new_lock=locks/'write.lock'
    if os.path.lexists(old):runtime.secure_directory(old)
    state=store.read_record(journal) if journal.exists() else None
    if state and state.get('version')!=1:raise ValueError('未知媒体记录迁移版本')
    if state and state.get('phase')=='complete':
        if os.path.lexists(old):raise ValueError('迁移后旧目录重新出现，拒绝合并')
        meta=new_lock.lstat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid!=0 or meta.st_mode&0o077 or [meta.st_dev,meta.st_ino]!=state['lock']:raise ValueError('全局锁身份或权限异常')
        return state
    if state and (state.get('old')!=str(old) or state.get('data')!=str(store.STATE)):
        raise ValueError('迁移记录路径不一致')
    if not state or state.get('phase')=='rolled_back':
        users=[]
        if old.exists():
            for node in sorted(old.iterdir()):
                if node.name=='write.lock':continue
                if not re.fullmatch(r'u[0-9]+',node.name):raise ValueError('旧目录有未知条目，保留原数据：'+node.name)
                users.append({'user':node.name,'files':fingerprint(node)})
        if old_lock.exists() and new_lock.exists():raise ValueError('新旧全局锁同时存在，拒绝自动覆盖')
        chosen=old_lock if old_lock.exists() else new_lock
        fd=os.open(chosen,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            meta=os.fstat(fd)
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid!=0 or meta.st_mode&0o077:raise ValueError('全局锁权限异常')
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            state={'version':1,'phase':'prepared','old':str(old),'data':str(store.STATE),'users':users,'lock':[meta.st_dev,meta.st_ino]}
            store.atomic(journal,state)
        finally:os.close(fd)
    # The inode remains locked across all renames. A resumed journal must point
    # to the original inode, never a newly created replacement lock.
    chosen=old_lock if old_lock.exists() else new_lock
    fd=os.open(chosen,os.O_RDWR|os.O_NOFOLLOW)
    try:
        meta=os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or [meta.st_dev,meta.st_ino]!=state['lock'] or meta.st_uid!=0 or meta.st_mode&0o077:raise ValueError('迁移锁身份变化')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        for item in state['users']:
            user=item['user']
            if not re.fullmatch(r'u[0-9]+',user):raise ValueError('迁移账号无效')
            for parent in (store.STATE,store.STATE/user,store.STATE/user/'mediaeditor'):
                runtime.secure_directory(parent,create=True,mode=0o711)
            source=old/user;target=store.STATE/user/'mediaeditor/protected'
            if source.exists() and os.path.lexists(target):raise ValueError('新旧记录同时存在，拒绝覆盖：'+user)
            current=source if source.exists() else target
            runtime.secure_directory(current)
            if fingerprint(current)!=item['files']:raise ValueError('迁移期间记录发生变化：'+user)
            if source.exists():
                if source.stat().st_dev!=target.parent.stat().st_dev:raise ValueError('迁移需要同一文件系统')
                os.rename(source,target)
                sync_directories(source.parent,target.parent)
            if fingerprint(target)!=item['files']:raise ValueError('迁移后校验失败：'+user)
        if old_lock.exists():
            if new_lock.exists():raise ValueError('目标锁已存在')
            if old_lock.stat().st_dev!=locks.stat().st_dev:raise ValueError('全局锁迁移需要同一文件系统')
            os.rename(old_lock,new_lock)
            sync_directories(old,locks)
        if old.exists():old.rmdir()
        sync_directories(old.parent)
        state['phase']='complete';store.atomic(journal,state)
        return state
    except BaseException:
        # Restore all accounts, including moves completed before an interrupted
        # invocation, while the same exclusive component and write locks are held.
        runtime.secure_directory(old,create=True,mode=0o700)
        if new_lock.exists() and not old_lock.exists():os.rename(new_lock,old_lock)
        for item in reversed(state['users']):
            source=old/item['user'];target=store.STATE/item['user']/'mediaeditor/protected'
            if target.exists() and not source.exists() and fingerprint(target)==item['files']:os.rename(target,source)
            sync_directories(source.parent,target.parent)
        sync_directories(old,locks)
        state['phase']='rolled_back';store.atomic(journal,state)
        raise
    finally:os.close(fd)

def rollback(store):
    """Deployment rollback only, with the exclusive common component lock held."""
    state=store.read_record(store.LOCK_STATE/'storage-layout.json')
    if state.get('phase')!='complete':raise ValueError('迁移状态不可回退')
    if state.get('old')!=str(store.LEGACY_STATE) or state.get('data')!=str(store.STATE):raise ValueError('迁移路径不一致')
    old=store.LEGACY_STATE;new_lock=store.LOCK_STATE/'write.lock'
    if os.path.lexists(old):raise ValueError('旧目录已被占用')
    fd=os.open(new_lock,os.O_RDWR|os.O_NOFOLLOW)
    try:
        meta=os.fstat(fd)
        if [meta.st_dev,meta.st_ino]!=state['lock']:raise ValueError('锁身份变化')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        for item in state['users']:
            if not re.fullmatch(r'u[0-9]+',item['user']):raise ValueError('无效账号')
            target=store.STATE/item['user']/'mediaeditor/protected'
            store.runtime.secure_directory(target)
            if fingerprint(target)!=item['files']:raise ValueError('迁移后数据已变化，不能自动回退')
        store.runtime.secure_directory(old,create=True,mode=0o700)
        for item in state['users']:
            target=store.STATE/item['user']/'mediaeditor/protected'
            os.rename(target,old/item['user']);sync_directories(target.parent,old)
        os.rename(new_lock,old/'write.lock');sync_directories(old,store.LOCK_STATE)
        state['phase']='rolled_back';store.atomic(store.LOCK_STATE/'storage-layout.json',state)
        return state
    finally:os.close(fd)

def main():
    store=load_store()
    with store.runtime.lock('component.lock',exclusive=True,wait=30):
        result=migrate(store)
    print(json.dumps({'ok':True,'accounts':len(result['users']),'lockInodePreserved':True},ensure_ascii=False))

if __name__=='__main__':main()
