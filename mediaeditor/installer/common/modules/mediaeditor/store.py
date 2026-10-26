"""Owned-row edits with protected plans, WAL snapshots and conflict-aware undo."""
import contextlib
import contextvars
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid

from sqlite_native import Database
import privileged_runtime as runtime

DB_PATH = Path('/nas/sys/video/mediacenter.db')
TOKENIZER = Path('/usr/lib/libmitrigram.so')
STATE = Path('/data/minas/plugins/data')
LEGACY_STATE = Path('/data/minas/plugins/mediaeditor')
LOCK_STATE = runtime.BASE/'modules/mediaeditor/var'
LABELS = {'midb_id':'影片代码','trans_name':'片名','season_number':'影片季号','season_name':'季名称',
          'collection_id':'系列归属','session':'文件季号','episode':'文件集号'}
MEDIA_FIELDS = {'midb_id','trans_name','season_number','season_name','collection_id'}
FILE_FIELDS = {'session','episode'}
SCOPE = contextvars.ContextVar('mediaeditor_scope', default='owned')
ADMIN = contextvars.ContextVar('mediaeditor_admin', default=False)


def is_admin(user):
    runtime.account(user)
    result = subprocess.run(['/sbin/uci','-q','get','user.'+user+'.permission'], capture_output=True, text=True, timeout=5,
                            env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LC_ALL':'C.UTF-8'})
    return result.returncode == 0 and result.stdout.strip() == 'admin'


@contextlib.contextmanager
def scope(user, value='owned'):
    if value not in ('owned','all'): raise ValueError('未知媒体库范围')
    admin = is_admin(user)
    if value == 'all' and not admin: raise ValueError('只有 NAS 管理员可以进入跨账号全库模式')
    a = SCOPE.set(value); b = ADMIN.set(admin)
    try: yield
    finally: SCOPE.reset(a); ADMIN.reset(b)


def integer(value, label, minimum=0, maximum=2147483647):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(label + '必须为范围内的整数')
    return value


def ident(value):
    integer(value, '记录编号', 1)
    return value


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',',':'))


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def storage(user):
    runtime.account(user)
    if os.path.lexists(LEGACY_STATE/user): raise ValueError('影视编辑旧记录尚未迁移，请完成插件更新')
    if not STATE.parent.exists():runtime.secure_directory(STATE.parent,create=True)
    for parent in (STATE,STATE/user,STATE/user/'mediaeditor'):
        runtime.secure_directory(parent,create=True,mode=0o711)
    directory = runtime.secure_directory(STATE/user/'mediaeditor/protected', create=True, mode=0o700)
    for name in ('plans','references','backups'):
        runtime.secure_directory(directory/name, create=True, mode=0o700)
    return directory


def atomic(path, data):
    runtime.secure_directory(path.parent)
    if os.path.lexists(path):
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o077:
            raise ValueError('编辑记录权限异常')
    fd, name = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            os.fchmod(stream.fileno(),0o600); stream.write(encoded(data)+'\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(name,path)
        directory = os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(name): os.unlink(name)


def read_record(path):
    s=path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o077 or s.st_size > 16777216:
        raise ValueError('编辑记录不安全')
    return json.loads(path.read_text())


def token(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-f0-9]{32}',value): raise ValueError('无效编辑记录编号')
    return value


@contextlib.contextmanager
def editing_lock():
    if os.path.lexists(LEGACY_STATE/'write.lock'): raise ValueError('影视编辑全局写锁尚未迁移，请完成插件更新')
    runtime.secure_directory(LOCK_STATE,create=True,mode=0o700)
    fd=os.open(LOCK_STATE/'write.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        s=os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o077: raise ValueError('编辑锁不安全')
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('另一项影视编辑正在进行，请稍后再试')
        yield
    finally: os.close(fd)


def database(writable=False, search=False):
    metadata=DB_PATH.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid!=0 or metadata.st_mode&0o022:
        raise ValueError('官方媒体库路径或权限异常')
    return Database(DB_PATH,writable, TOKENIZER if writable or search else None)


def uid(user): return pwd.getpwnam(user).pw_uid


def libraries(db,user):
    if SCOPE.get() == 'all': return db.query('SELECT library_id AS id,name,scan_status,user_id AS ownerUid FROM library ORDER BY user_id,library_id')
    return db.query('SELECT library_id AS id,name,scan_status FROM library WHERE user_id=? ORDER BY library_id',(uid(user),))


def series_list(db,user):
    if SCOPE.get() == 'all': return db.query('SELECT id,identifier,name FROM collection ORDER BY name,id')
    return db.query('''SELECT DISTINCT c.id,c.identifier,c.name FROM collection c JOIN media m ON m.collection_id=c.id
        JOIN file_media fm ON fm.media_id=m.media_id JOIN library_file lf ON lf.file_id=fm.file_id
        JOIN library l ON l.library_id=lf.library_id WHERE l.user_id=? ORDER BY c.name,c.id''',(uid(user),))


def owned(db,user,table,record_id,write=False):
    if table not in ('media','file'): raise ValueError('不支持的记录类别')
    key='media_id' if table=='media' else 'file_id'
    row=db.one('SELECT * FROM '+table+' WHERE '+key+'=?',(ident(record_id),))
    if not row: raise ValueError('目标记录已不存在')
    join='JOIN file_media fm ON fm.file_id=lf.file_id' if table=='media' else ''
    condition='fm.media_id=?' if table=='media' else 'lf.file_id=?'
    refs=db.query('''SELECT DISTINCT l.library_id,l.user_id,
        (SELECT count(*) FROM share s WHERE s.library_id=l.library_id) AS share_count
        FROM library_file lf JOIN library l ON l.library_id=lf.library_id '''+join+' WHERE '+condition+' ORDER BY l.library_id',(record_id,))
    if SCOPE.get() == 'all': return {'table':table,'id':record_id,'row':row,'refs':refs,'editable':True}
    if not any(r['user_id']==uid(user) for r in refs): raise ValueError('不能访问其他账号的媒体记录')
    editable=all(r['user_id']==uid(user) and r['share_count']==0 for r in refs)
    if write and not editable: raise ValueError('记录被其他账号或共享库引用，已拒绝直接修改共享元数据')
    return {'table':table,'id':record_id,'row':row,'refs':refs,'editable':editable}


def list_media(user,request):
    library_id=integer(request.get('libraryId',0),'媒体库编号')
    offset=integer(request.get('offset',0),'分页位置',0,10000000)
    q=request.get('query','')
    if not isinstance(q,str) or len(q)>100: raise ValueError('片名查询长度无效')
    with database() as db:
        params=[] if SCOPE.get() == 'all' else [uid(user)];where='1=1' if SCOPE.get() == 'all' else 'l.user_id=?'
        if library_id:
            where+=' AND l.library_id=?';params.append(library_id)
        if q.strip():
            where+=' AND (m.trans_name LIKE ? OR m.origin_name LIKE ? OR m.midb_id=? OR m.media_id=?)'
            params.extend(['%'+q.strip()+'%','%'+q.strip()+'%',q.strip(),int(q.strip()) if q.strip().isdigit() and len(q.strip())<11 else -1])
        rows=db.query('''SELECT DISTINCT m.media_id,m.trans_name,m.origin_name,m.year,m.class,m.midb_id,
            m.collection_id,m.season_number,c.name AS collection_name,c.identifier AS collection_identifier
            FROM media m LEFT JOIN file_media fm ON fm.media_id=m.media_id LEFT JOIN library_file lf ON lf.file_id=fm.file_id
            LEFT JOIN library l ON l.library_id=lf.library_id LEFT JOIN collection c ON c.id=m.collection_id
            WHERE '''+where+' ORDER BY m.trans_name,m.season_number,m.media_id LIMIT 501 OFFSET ?',tuple(params)+(offset,))
        return {'libraries':libraries(db,user),'series':series_list(db,user),'medias':rows[:500],'limit':500,'offset':offset,'hasMore':len(rows)>500}


def details(user,media_id):
    with database() as db:
        db.query('BEGIN')
        entry=owned(db,user,'media',media_id)
        row=dict(entry['row'])
        # Do not return unrelated raw actor metadata or private database internals.
        media={k:row.get(k) for k in ('media_id','midb_id','trans_name','origin_name','year','class','season_number','season_name','collection_id','brief')}
        if row.get('collection_id'):
            media['collection']=db.one('SELECT id,identifier,name FROM collection WHERE id=?',(row['collection_id'],))
        files=db.query('''SELECT DISTINCT f.file_id,f.file_path,f.session,f.episode,f.resolution,f.hdr,f.audio_codec,f.duration
            FROM file f JOIN file_media fm ON fm.file_id=f.file_id LEFT JOIN library_file lf ON lf.file_id=f.file_id
            LEFT JOIN library l ON l.library_id=lf.library_id WHERE fm.media_id=?'''+('' if SCOPE.get()=='all' else ' AND l.user_id=?')+' ORDER BY f.session,f.episode,f.file_path',
            (media_id,) if SCOPE.get()=='all' else (media_id,uid(user)))
        for f in files:
            f['name']=Path(f['file_path']).name
            snapshot=owned(db,user,'file',f['file_id'])
            f['editable']=snapshot['editable'];f['fingerprint']=fingerprint(snapshot)
        return {'media':media,'files':files,'editable':entry['editable'],'affectedLibraries':entry['refs'],'ownLibraryIds':[r['library_id'] for r in entry['refs'] if r['user_id']==uid(user)],'fingerprint':fingerprint(entry)}


def reference(user,reference_id,code):
    data=read_record(storage(user)/'references'/(token(reference_id)+'.json'))
    if time.time()-data['created']>3600: raise ValueError('搜索参考已过期，请重新搜索')
    selected=next((r for r in data['results'] if r['code']==code),None)
    if not selected: raise ValueError('搜索候选不存在')
    if not selected.get('seriesIdentifier'): raise ValueError('该搜索候选没有可关联的系列')
    return {'identifier':selected['seriesIdentifier'],'name':selected.get('seriesName') or selected['title']}


def validate_fields(db,user,table,record_id,fields,trusted_series=False,restoring=False):
    allowed=MEDIA_FIELDS if table=='media' else FILE_FIELDS
    if not isinstance(fields,dict) or set(fields)-allowed: raise ValueError('存在不支持编辑的字段')
    for key,value in fields.items():
        if value is None and restoring: continue
        if key=='midb_id':
            if not isinstance(value,str) or not re.fullmatch(r'[a-fA-F0-9]{32}',value): raise ValueError('影片代码必须为 32 位十六进制字符')
            other=db.one('SELECT media_id FROM media WHERE lower(midb_id)=lower(?) AND media_id<>?',(value,record_id))
            if other:
                conflicts = code_conflicts(db,user,record_id,value)
                raise ValueError('影片代码重复：'+ '、'.join((r['title']+'（#'+str(r['mediaId'])+'）') if r.get('mediaId') else r['title'] for r in conflicts)+ '。请先修改重复影片，再保存当前影片。')
            fields[key]=value.lower()
        elif key in ('trans_name','season_name'):
            if not isinstance(value,str) or len(value)>300 or '\x00' in value or (key=='trans_name' and not value.strip()): raise ValueError('片名或季名称无效')
        elif key=='collection_id':
            if value is not None:
                ident(value)
                if not trusted_series and value not in {s['id'] for s in series_list(db,user)}: raise ValueError('请从当前账号可见的系列中选择')
        else: integer(value,LABELS[key],0,9999)


def validate_episodes(db,user,targets,allow_duplicates=False):
    edits={t['id']:t['changes'] for t in targets if t['table']=='file'}
    if not edits or allow_duplicates: return
    media_ids=set()
    for fid in edits:
        media_ids.update(r['media_id'] for r in db.query('SELECT media_id FROM file_media WHERE file_id=?',(fid,)))
    for mid in media_ids:
        rows=db.query('SELECT DISTINCT f.file_id,f.session,f.episode FROM file f JOIN file_media fm ON fm.file_id=f.file_id WHERE fm.media_id=?',(mid,))
        grouped={}
        for row in rows:
            desired=dict(row,**edits.get(row['file_id'],{}))
            if desired['episode'] is None or desired['episode']<=0: continue
            key=(desired['session'],desired['episode'])
            grouped.setdefault(key,[]).append(row['file_id'])
        for key,ids in grouped.items():
            if len(ids)>1 and any(i in edits for i in ids): raise ValueError('修改后出现重复的季/集号；若确为多版本文件，请明确允许重复集号')


def preview(user,request):
    with editing_lock(),database() as db:
        targets=[]
        if request.get('kind')=='media':
            mid=ident(request.get('mediaId'));fields=request.get('fields',{})
            original=owned(db,user,'media',mid,True)
            if request.get('fingerprint')!=fingerprint(original): raise ValueError('影片数据已变化，请重新载入再编辑')
            validate_fields(db,user,'media',mid,fields)
            changes={k:v for k,v in fields.items() if original['row'].get(k)!=v}
            series_ref=None
            ref=request.get('seriesReference')
            if ref:
                if not isinstance(ref,dict) or set(ref)!={'searchId','code'}: raise ValueError('系列参考参数无效')
                series_ref=reference(user,ref['searchId'],ref['code'])
                existing=db.one('SELECT id FROM collection WHERE identifier=?',(series_ref['identifier'],))
                changes.pop('collection_id',None)
                if not existing or existing['id']!=original['row']['collection_id']:
                    changes['collection_id']=existing['id'] if existing else None
                else: series_ref=None
            if changes or series_ref: targets.append({'table':'media','id':mid,'changes':changes,'before':original,'seriesRef':series_ref})
        elif request.get('kind')=='episodes':
            edits=request.get('edits')
            if not isinstance(edits,list) or not 1<=len(edits)<=100: raise ValueError('每次请选择 1 至 100 个文件')
            seen=set()
            for edit in edits:
                if not isinstance(edit,dict) or set(edit)!={'fileId','session','episode','fingerprint'}: raise ValueError('文件编辑参数无效')
                fid=ident(edit['fileId'])
                if fid in seen: raise ValueError('同一个文件不能重复提交')
                seen.add(fid);old=owned(db,user,'file',fid,True)
                if fingerprint(old)!=edit['fingerprint']: raise ValueError('文件数据已变化，请重新载入集数表')
                fields={k:edit[k] for k in FILE_FIELDS};validate_fields(db,user,'file',fid,fields)
                changes={k:v for k,v in fields.items() if old['row'].get(k)!=v}
                if changes: targets.append({'table':'file','id':fid,'changes':changes,'before':old,'seriesRef':None})
        else: raise ValueError('未知编辑类别')
        if not targets: raise ValueError('没有需要保存的修改')
        allow=request.get('allowDuplicates',False)
        if not isinstance(allow,bool): raise ValueError('重复集号选项无效')
        validate_episodes(db,user,targets,allow)
        plan={'id':uuid.uuid4().hex,'user':user,'scope':SCOPE.get(),'created':int(time.time()),'state':'draft','targets':targets,'allowDuplicates':allow}
        atomic(storage(user)/'plans'/(plan['id']+'.json'),plan)
        return public_plan(plan,db)


def public_plan(plan,db=None):
    if plan.get('engine') == 'structural-v1': return public_structural_plan(plan)
    changes=[]
    for t in plan['targets']:
        before=t['before']['row']
        for key,value in t['changes'].items():
            old=before.get(key);new=value
            if key=='collection_id' and db:
                def label(v):
                    row=db.one('SELECT id,name,identifier FROM collection WHERE id=?',(v,)) if v else None
                    return (row['name']+' · 本地 #'+str(row['id'])+' · '+row['identifier']) if row else '无系列'
                old=label(old);new=label(value)
            if key=='collection_id' and t.get('seriesRef'): new=t['seriesRef']['name']+' · '+t['seriesRef']['identifier']+'（自动映射本地编号）'
            changes.append({'table':t['table'],'id':t['id'],'record':before.get('trans_name') or Path(before.get('file_path','')).name,'field':key,'label':LABELS[key],'before':old,'after':new})
    return {'planId':plan['id'],'created':plan['created'],'state':plan['state'],'changes':changes,'affectedRecords':len(plan['targets'])}


def backup(user,plan_id):
    destination=storage(user)/'backups'/(plan_id+'.sqlite3')
    if destination.exists(): raise ValueError('备份编号已存在，已停止重复写入')
    if shutil.disk_usage(destination.parent).free < DB_PATH.stat().st_size*2+128*1024*1024: raise ValueError('受保护备份目录空间不足')
    fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
    try:
        source=sqlite3.connect('file:'+str(DB_PATH)+'?mode=ro',uri=True,timeout=5)
        target=sqlite3.connect(str(destination))
        try: source.backup(target,pages=256,sleep=.01)
        finally: target.close();source.close()
        with Database(destination,False,TOKENIZER) as check:
            if check.query('PRAGMA quick_check')!=[{'quick_check':'ok'}]: raise ValueError('一致性备份校验失败')
        with destination.open('rb') as stream: os.fsync(stream.fileno())
        return destination.name
    except BaseException:
        destination.unlink(missing_ok=True);raise


def validate_database(db):
    if db.query('PRAGMA quick_check')!=[{'quick_check':'ok'}]: raise ValueError('修改后媒体库完整性检查失败')
    if db.query('PRAGMA foreign_key_check'): raise ValueError('修改后媒体库关联检查失败')


def execute(user,plan_id):
    plan = read_record(storage(user)/'plans'/(token(plan_id)+'.json'))
    with scope(user,plan.get('scope','owned')):
        if plan.get('engine') == 'structural-v1': return execute_structural(user,plan_id)
        return execute_fields(user,plan_id)


def execute_fields(user,plan_id):
    path=storage(user)/'plans'/(token(plan_id)+'.json')
    with editing_lock(),database(True) as db:
        plan=read_record(path)
        if plan.get('user')!=user: raise ValueError('编辑记录身份无效')
        if plan['state']=='applied': return dict(public_plan(plan),ok=True,alreadyApplied=True)
        if plan['state']=='prepared':
            if all(owned(db,user,t['table'],t['id'])==t.get('after') for t in plan['targets']):
                plan.update(state='applied',applied=int(time.time()));atomic(path,plan)
                return dict(public_plan(plan),ok=True,alreadyApplied=True)
            raise ValueError('上次操作未完成，请重新载入并核对修改记录')
        if plan['state']!='draft' or time.time()-plan['created']>900: raise ValueError('修改预览已过期，请重新预览')
        db.query('BEGIN IMMEDIATE');locked=True
        try:
            for t in plan['targets']:
                current=owned(db,user,t['table'],t['id'],True)
                if current!=t['before']: raise ValueError('预览后记录或关联范围已变化，已拒绝覆盖')
                validate_fields(db,user,t['table'],t['id'],t['changes'],bool(t.get('seriesRef') or plan.get('reverts')),bool(plan.get('reverts')))
            validate_episodes(db,user,plan['targets'],plan['allowDuplicates'])
            plan['backup']=backup(user,plan['id'])
            for t in plan['targets']:
                if t.get('seriesRef'):
                    ref=t['seriesRef'];col=db.one('SELECT id FROM collection WHERE identifier=?',(ref['identifier'],))
                    if not col:
                        db.query('INSERT INTO collection(identifier,name,score) VALUES(?,?,0)',(ref['identifier'],ref['name']))
                        col=db.one('SELECT id FROM collection WHERE identifier=?',(ref['identifier'],))
                    t['changes']['collection_id']=col['id']
                fields=t['changes'];columns=sorted(fields);key='media_id' if t['table']=='media' else 'file_id'
                db.query('UPDATE '+t['table']+' SET '+','.join(k+'=?' for k in columns)+' WHERE '+key+'=?',tuple(fields[k] for k in columns)+(t['id'],))
                if db.lib.sqlite3_changes(db.handle)!=1: raise ValueError('目标记录数量发生变化')
                t['after']=owned(db,user,t['table'],t['id'],True)
                if any(t['after']['row'].get(k)!=v for k,v in fields.items()): raise ValueError('修改回读不一致')
            validate_database(db)
            plan['state']='prepared';atomic(path,plan)
            db.query('COMMIT');locked=False
            plan.update(state='applied',applied=int(time.time()));atomic(path,plan)
            if plan.get('reverts'):
                old_path=storage(user)/'plans'/(token(plan['reverts'])+'.json')
                old=read_record(old_path);old.update(state='reverted',revertedBy=plan['id']);atomic(old_path,old)
            return dict(public_plan(plan),ok=True)
        except BaseException:
            if locked:
                db.query('ROLLBACK')
                # A backup is deliberately retained; failed plans cannot be replayed.
                plan.update(state='failed',failed=int(time.time()));atomic(path,plan)
            raise


def history(user):
    result=[]
    paths=sorted((storage(user)/'plans').glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
    for path in paths[:100]:
        plan=read_record(path)
        if plan.get('scope')=='all' and not ADMIN.get(): continue
        if plan.get('user')==user and plan.get('state')!='draft':
            value=public_plan(plan);value['canUndo']=plan['state']=='applied';value['applied']=plan.get('applied');value['backup']=plan.get('backup')
            result.append(value)
    return {'records':result}


def preview_undo(user,plan_id):
    original=read_record(storage(user)/'plans'/(token(plan_id)+'.json'))
    with scope(user,original.get('scope','owned')):
        if original.get('engine') == 'structural-v1': return undo_structural(user,original)
        return undo_fields(user,plan_id)


def undo_fields(user,plan_id):
    with editing_lock(),database() as db:
        original=read_record(storage(user)/'plans'/(token(plan_id)+'.json'))
        if original.get('user')!=user or original['state']!='applied': raise ValueError('该修改不能回退')
        targets=[]
        for t in original['targets']:
            current=owned(db,user,t['table'],t['id'],True)
            if current!=t.get('after'): raise ValueError('修改后记录已再次变化，不能直接回退覆盖')
            reverse={k:t['before']['row'].get(k) for k in t['changes']}
            targets.append({'table':t['table'],'id':t['id'],'changes':reverse,'before':current,'seriesRef':None})
        plan={'id':uuid.uuid4().hex,'user':user,'scope':SCOPE.get(),'created':int(time.time()),'state':'draft','targets':targets,'allowDuplicates':True,'reverts':original['id']}
        atomic(storage(user)/'plans'/(plan['id']+'.json'),plan)
        return public_plan(plan,db)


def file_fingerprints(user,details_result):
    with database() as db:
        for row in details_result['files']:
            row['fingerprint']=fingerprint(owned(db,user,'file',row['file_id']))
    return details_result


def code_conflicts(db,user,media_id,code):
    rows = db.query('SELECT media_id,trans_name,origin_name,year FROM media WHERE lower(midb_id)=lower(?) AND media_id<>? ORDER BY media_id',(code,media_id))
    result = []
    for row in rows:
        visible = ADMIN.get()
        needs_all = False
        try: needs_all = not owned(db,user,'media',row['media_id'])['editable']
        except ValueError: needs_all = True
        if not visible:
            try: owned(db,user,'media',row['media_id']); visible = True
            except ValueError: pass
        result.append({'mediaId':row['media_id'] if visible else None,
                       'title':(row['trans_name'] or row['origin_name'] or '未命名') if visible else '其他账号的影片（请联系管理员处理）',
                       'year':row['year'] if visible else None,'requiresAllScope':needs_all})
    return result


def check_code(user,request):
    mid = ident(request.get('mediaId')); code = request.get('code','')
    if not isinstance(code,str) or not re.fullmatch(r'[a-fA-F0-9]{32}',code): raise ValueError('影片代码必须为 32 位十六进制字符')
    with database() as db:
        owned(db,user,'media',mid)
        return {'conflicts':code_conflicts(db,user,mid,code),'forceSupported':False}


def list_files(user,request):
    query=request.get('query','');offset=integer(request.get('offset',0),'文件分页位置',0,10000000)
    if not isinstance(query,str) or len(query)>200: raise ValueError('文件查询无效')
    with database() as db:
        where='1=1' if SCOPE.get()=='all' else 'l.user_id=?';values=[] if SCOPE.get()=='all' else [uid(user)]
        if query.strip():
            where+=' AND (f.file_path LIKE ? OR f.file_id=?)';values += ['%'+query.strip()+'%',int(query.strip()) if query.strip().isdigit() and len(query.strip())<11 else -1]
        rows=db.query('SELECT DISTINCT f.file_id,f.file_path,f.session,f.episode FROM file f LEFT JOIN library_file lf ON lf.file_id=f.file_id LEFT JOIN library l ON l.library_id=lf.library_id WHERE '+where+' ORDER BY f.file_path,f.file_id LIMIT 101 OFFSET ?',tuple(values)+(offset,))
        for row in rows[:100]:
            entry=owned(db,user,'file',row['file_id']);row.update(name=Path(row['file_path']).name,editable=entry['editable'],fingerprint=fingerprint(entry),
                medias=db.query('SELECT m.media_id,m.trans_name FROM media m JOIN file_media fm ON fm.media_id=m.media_id WHERE fm.file_id=? ORDER BY m.media_id',(row['file_id'],)))
        return {'files':rows[:100],'hasMore':len(rows)>100,'offset':offset,'limit':100}


# Structural plans are simulated against a protected SQLite copy with the actual
# firmware triggers. Only the resulting reviewed delta is offered for execution.
TABLE_KEYS = {'library':'library_id','dir':'dir_id','file':'file_id','media':'media_id','collection':'id',
              'library_file':'insert_id','lib_dir':'insert_id','file_media':'insert_id','share':'share_id',
              'user_file':'insert_id','user_media':'insert_id','media_search':'rowid'}
SEARCH_FIELDS = ('trans_name','origin_name','other_name','director_names','actor_names','screenwriter_names')
SUPPORTED_SCHEMAS = {'baddbbc4e09730f06370d1e0c8dbb487a0aa5cb64274995daa62a1d93a01ada6'}  # inspected official firmware schema


def structural_schema(db):
    return db.query("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'media_search_%' ORDER BY type,name")


def require_structural_schema(db):
    if fingerprint(structural_schema(db)) not in SUPPORTED_SCHEMAS:
        raise ValueError('当前官方数据库结构未适配文件重分配/全库修复，已停止结构操作')


def logical_snapshot(db):
    return {name:db.query('SELECT '+('rowid,*' if key=='rowid' else '*')+' FROM '+name+' ORDER BY '+key)
            for name,key in TABLE_KEYS.items()} | {'sequence':db.query('SELECT name,seq FROM sqlite_sequence ORDER BY name'),
                                                 'schema':structural_schema(db)}


def snapshot_delta(before,after):
    result = []
    for name,key in TABLE_KEYS.items():
        a = {r[key]:r for r in before[name]}; b = {r[key]:r for r in after[name]}
        for identity in sorted(a.keys() | b.keys()):
            if a.get(identity) != b.get(identity): result.append({'table':name,'id':identity,'before':a.get(identity),'after':b.get(identity)})
    return result


@contextlib.contextmanager
def simulation(user):
    directory = storage(user)/'plans'
    fd,name = tempfile.mkstemp(prefix='.preview-',suffix='.sqlite3',dir=directory); os.fchmod(fd,0o600); os.close(fd)
    path = Path(name)
    try:
        source = sqlite3.connect('file:'+str(DB_PATH)+'?mode=ro',uri=True,timeout=5); target = sqlite3.connect(str(path))
        try: source.backup(target,pages=256,sleep=.01)
        finally: target.close(); source.close()
        with Database(path,True,TOKENIZER) as db: yield db
    finally:
        for suffix in ('','-wal','-shm','-journal'): Path(name+suffix).unlink(missing_ok=True)


def search_rebuild(db):
    db.query('DELETE FROM media_search')
    db.query('''INSERT INTO media_search(rowid,trans_name,origin_name,other_name,director_names,actor_names,screenwriter_names)
        SELECT media_id,CASE WHEN season_name IS NOT NULL AND season_name!='' THEN trans_name||' '||season_name ELSE trans_name END,
        origin_name,other_name,director_names,actor_names,screenwriter_names FROM media''')


def restore_delta(db,deltas):
    # Replaying DELETE triggers during undo would destroy the very history being
    # restored. Suppression is connection-local, temporary and only used for undo.
    db.query('PRAGMA defer_foreign_keys=ON')
    with db.restoration_triggers():
        for change in deltas:
            name = change['table']; key = TABLE_KEYS[name]; wanted = change['before']
            current = db.one('SELECT '+('rowid,*' if key=='rowid' else '*')+' FROM '+name+' WHERE '+key+'=?',(change['id'],))
            if wanted is None:
                if current: db.query('DELETE FROM '+name+' WHERE '+key+'=?',(change['id'],))
            elif current is None:
                columns = list(wanted)
                db.query('INSERT INTO '+name+'('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(wanted[k] for k in columns))
            else:
                columns = [k for k in wanted if k!=key]
                db.query('UPDATE '+name+' SET '+','.join(k+'=?' for k in columns)+' WHERE '+key+'=?',tuple(wanted[k] for k in columns)+(change['id'],))


def run_structural(db,commands):
    for command in commands:
        typ = command['type']
        if typ == 'move':
            source,target,fid = command['source'],command['target'],command['file']
            if not db.one('SELECT insert_id FROM file_media WHERE media_id=? AND file_id=?',(target,fid)):
                db.query('INSERT INTO file_media(media_id,file_id,insert_time) VALUES(?,?,?)',(target,fid,command['insertTime']))
            if source is not None: db.query('DELETE FROM file_media WHERE media_id=? AND file_id=?',(source,fid))
        elif typ == 'repair_set':
            name = command['table']; fields = command['fields']; key = TABLE_KEYS[name]
            allowed = {'media':{'quote','collection_id'},'file':{'quote','session','episode'},'user_media':{'last_seen_file_id'}}
            if name not in allowed or set(fields)-allowed[name]: raise ValueError('无效修复字段')
            columns = sorted(fields)
            db.query('UPDATE '+name+' SET '+','.join(k+'=?' for k in columns)+' WHERE '+key+'=?',tuple(fields[k] for k in columns)+(command['id'],))
        elif typ == 'repair_delete':
            name = command['table']
            if name not in {'file_media','library_file','lib_dir','share','user_file','user_media'}: raise ValueError('无效修复关联')
            db.query('DELETE FROM '+name+' WHERE '+TABLE_KEYS[name]+'=?',(command['id'],))
        elif typ == 'search_rebuild': search_rebuild(db)
        elif typ == 'restore': restore_delta(db,command['deltas'])
        else: raise ValueError('未知结构操作')


def no_new_corruption(db,before_fk):
    if db.query('PRAGMA quick_check') != [{'quick_check':'ok'}]: raise ValueError('媒体库完整性检查失败')
    after = db.query('PRAGMA foreign_key_check')
    if {encoded(r) for r in after} - {encoded(r) for r in before_fk}: raise ValueError('操作产生新的外键错误，已回滚')


def association_guard(db,media_ids,file_ids):
    def selected(table,column,values):
        if not values: return []
        return db.query('SELECT * FROM '+table+' WHERE '+column+' IN ('+','.join('?' for _ in values)+') ORDER BY '+TABLE_KEYS[table],tuple(values))
    return {'links':selected('file_media','media_id',media_ids),'history':selected('user_media','media_id',media_ids),
            'files':selected('file','file_id',file_ids),'libraries':selected('library_file','file_id',file_ids)}


def make_structural_plan(user,db,commands,kind,media_ids=(),file_ids=(),reverts=None,allowed_fk=()):
    require_structural_schema(db); before = logical_snapshot(db); fk = db.query('PRAGMA foreign_key_check')
    if allowed_fk:
        if kind!='undo' or not reverts: raise ValueError('只有精确回退可以恢复已记录的旧外键错误')
        db.query('PRAGMA foreign_keys=OFF')
    db.query('BEGIN IMMEDIATE')
    try:
        run_structural(db,commands); no_new_corruption(db,list(fk)+list(allowed_fk)); after = logical_snapshot(db)
        deltas = snapshot_delta(before,after)
        if not deltas: raise ValueError('没有需要保存的修改')
        plan = {'id':uuid.uuid4().hex,'user':user,'scope':SCOPE.get(),'engine':'structural-v1','kind':kind,'created':int(time.time()),'state':'draft',
                'commands':commands,'beforeHash':fingerprint(before),'afterHash':fingerprint(after),'deltas':deltas,
                'beforeForeignKeys':fk,'allowedForeignKeys':list(allowed_fk),
                'mediaIds':list(media_ids),'fileIds':list(file_ids),'afterGuard':association_guard(db,media_ids,file_ids)}
        if reverts: plan['reverts'] = reverts
        if len(encoded(plan).encode())>16000000: raise ValueError('本次修改记录过大，请减少选中文件或修复项目分批处理')
        atomic(storage(user)/'plans'/(plan['id']+'.json'),plan)
        return public_structural_plan(plan)
    finally:
        db.query('ROLLBACK')
        if allowed_fk: db.query('PRAGMA foreign_keys=ON')


def preview_relations(user,request):
    source = integer(request.get('sourceMediaId',0),'原影片编号'); target = ident(request.get('targetMediaId'))
    if source == target: raise ValueError('目标影片不能与原影片相同')
    files = request.get('files')
    if not isinstance(files,list) or not 1<=len(files)<=100: raise ValueError('每次请选择 1 至 100 个文件')
    with editing_lock(),simulation(user) as db:
        commands=[]; seen=set();media_ids={target}
        for file in files:
            if not isinstance(file,dict) or set(file)!={'fileId','fingerprint'}: raise ValueError('文件参数无效')
            fid = ident(file['fileId'])
            if fid in seen: raise ValueError('文件重复提交')
            seen.add(fid); entry = owned(db,user,'file',fid,True)
            if fingerprint(entry)!=file['fingerprint']: raise ValueError('文件已变化，请重新载入')
            sources=[r['media_id'] for r in db.query('SELECT media_id FROM file_media WHERE file_id=? ORDER BY media_id',(fid,))]
            if source:
                if source not in sources: raise ValueError('文件已不属于原影片')
                sources=[source]
            sources=[mid for mid in sources if mid!=target]
            if not sources and db.one('SELECT insert_id FROM file_media WHERE media_id=? AND file_id=?',(target,fid)): continue
            for mid in sources or [None]:
                if mid is not None: media_ids.add(mid)
                commands.append({'type':'move','source':mid,'target':target,'file':fid,'insertTime':int(time.time())})
        for mid in sorted(media_ids):
            entry = owned(db,user,'media',mid,True);count = db.one('SELECT count(*) AS n FROM file_media WHERE media_id=?',(mid,))['n']
            if entry['row']['quote'] != count: raise ValueError('影片引用计数异常，请先在全库修复中修正计数')
        return make_structural_plan(user,db,commands,'relations',sorted(media_ids),sorted(seen))


def public_structural_plan(plan):
    changes=[]; deleted_history=0; deleted_media=[]; deleted_files=0
    for delta in plan['deltas']:
        name=delta['table']; a=delta['before']; b=delta['after']; row=a or b
        title=row.get('trans_name') or Path(row.get('file_path','')).name or (('用户 '+str(row.get('user_id'))+' 的观看/收藏记录') if name in ('user_media','user_file') else name+' #'+str(delta['id']))
        if name in ('user_media','user_file') and a and not b: deleted_history+=1
        if name=='media' and a and not b: deleted_media.append({'mediaId':delta['id'],'title':title})
        if name=='file' and a and not b: deleted_files+=1
        if not a or not b:
            changes.append({'table':name,'id':delta['id'],'record':title,'field':name,'label':{'file_media':'文件与影片关联','user_media':'观看/收藏记录','user_file':'文件播放记录','media_search':'搜索索引'}.get(name,name),
                            'before':('存在 · '+str(a.get('media_id',''))+' '+str(a.get('file_id',''))) if a else '不存在',
                            'after':('存在 · '+str(b.get('media_id',''))+' '+str(b.get('file_id',''))) if b else '删除'})
        else:
            for key in b:
                if a.get(key)!=b[key]: changes.append({'table':name,'id':delta['id'],'record':title,'field':key,'label':LABELS.get(key,{'quote':'引用计数','last_seen_file_id':'上次观看文件'}.get(key,key)),
                                                     'before':str(a.get(key))[:300] if isinstance(a.get(key),str) else a.get(key),
                                                     'after':str(b[key])[:300] if isinstance(b[key],str) else b[key]})
    return {'planId':plan['id'],'created':plan['created'],'state':plan['state'],'kind':plan['kind'],'scope':plan['scope'],
            'changes':changes[:1000],'totalChanges':len(changes),'affectedRecords':len(plan['deltas']),
            'deletedHistoryRecords':deleted_history,'deletedMedia':deleted_media,'deletedFileRecords':deleted_files,
            'warning':('沿用官方删除关联逻辑：将删除 '+str(deleted_history)+' 条观看/收藏记录。' if deleted_history else '')+
                      ('原影片最后一个关联被移走后，将删除 '+str(len(deleted_media))+' 条影片记录。' if deleted_media else '')+
                      ('本次精确回退将恢复修复前已有的 '+str(len(plan['allowedForeignKeys']))+' 项外键错误。' if plan.get('allowedForeignKeys') else '')}


def execute_structural(user,plan_id):
    path=storage(user)/'plans'/(token(plan_id)+'.json')
    with editing_lock(),database(True) as db:
        plan=read_record(path)
        if plan.get('user')!=user: raise ValueError('修改记录身份不匹配')
        require_structural_schema(db)
        if plan['state']=='applied': return dict(public_plan(plan),ok=True,alreadyApplied=True)
        if plan['state']=='prepared':
            if fingerprint(logical_snapshot(db))!=plan['afterHash']: raise ValueError('上次修改状态需核对，请查看历史记录，勿重复执行')
            plan.update(state='applied',applied=int(time.time()));atomic(path,plan)
            return dict(public_plan(plan),ok=True,alreadyApplied=True)
        if plan['state']!='draft' or time.time()-plan['created']>900: raise ValueError('修改预览已过期，请重新预览')
        if plan.get('allowedForeignKeys'):
            if plan['kind']!='undo' or not plan.get('reverts'): raise ValueError('无效的历史修复回退')
            db.query('PRAGMA foreign_keys=OFF')
        db.query('BEGIN IMMEDIATE'); locked=True
        try:
            if fingerprint(logical_snapshot(db))!=plan['beforeHash']: raise ValueError('预览后媒体库发生变化，请重新预览，已拒绝覆盖')
            for fid in plan['fileIds']: owned(db,user,'file',fid,True)
            fk=db.query('PRAGMA foreign_key_check');plan['backup']=backup(user,plan['id'])
            run_structural(db,plan['commands']);no_new_corruption(db,fk+plan.get('allowedForeignKeys',[]))
            if fingerprint(logical_snapshot(db))!=plan['afterHash']: raise ValueError('官方触发器执行结果与预览不一致，已回滚')
            plan['state']='prepared';atomic(path,plan);db.query('COMMIT');locked=False
            plan.update(state='applied',applied=int(time.time()));atomic(path,plan)
            if plan.get('reverts'):
                old_path=storage(user)/'plans'/(token(plan['reverts'])+'.json');old=read_record(old_path);old.update(state='reverted',revertedBy=plan['id']);atomic(old_path,old)
            return dict(public_plan(plan),ok=True)
        except BaseException:
            if locked: db.query('ROLLBACK');plan.update(state='failed',failed=int(time.time()));atomic(path,plan)
            raise


def undo_structural(user,original):
    if original.get('user')!=user or original['state']!='applied': raise ValueError('该修改不能回退')
    with editing_lock(),simulation(user) as db:
        require_structural_schema(db)
        for delta in original['deltas']:
            name=delta['table'];key=TABLE_KEYS[name]
            row=db.one('SELECT '+('rowid,*' if key=='rowid' else '*')+' FROM '+name+' WHERE '+key+'=?',(delta['id'],))
            if row!=delta['after']: raise ValueError('相关记录已再次变化，不能回退覆盖')
        if association_guard(db,original['mediaIds'],original['fileIds'])!=original['afterGuard']: raise ValueError('相关关联或观看记录已变化，不能回退覆盖')
        for fid in original['fileIds']: owned(db,user,'file',fid,True)
        return make_structural_plan(user,db,[{'type':'restore','deltas':original['deltas']}],'undo',original['mediaIds'],original['fileIds'],original['id'],original.get('beforeForeignKeys',[]))


def scan_repairs(user):
    if SCOPE.get()!='all' or not ADMIN.get(): raise ValueError('全库修复需要管理员全库模式')
    with editing_lock(),database(False,True) as db:
        require_structural_schema(db);db.query('BEGIN'); snapshot=logical_snapshot(db);issues=[]
        def issue(kind,title,table,identity,command):
            issues.append({'id':kind+':'+str(identity),'kind':kind,'title':title,'table':table,'recordId':identity,'command':command})
        for table,relation,column,key in (('media','file_media','media_id','media_id'),('file','library_file','file_id','file_id')):
            for row in db.query('SELECT t.'+key+' AS id,t.quote,(SELECT count(*) FROM '+relation+' x WHERE x.'+column+'=t.'+key+') AS actual FROM '+table+' t WHERE t.quote IS NOT (SELECT count(*) FROM '+relation+' x WHERE x.'+column+'=t.'+key+')'):
                issue('quote-'+table,('影片' if table=='media' else '文件')+' #'+str(row['id'])+' 引用计数 '+str(row['quote'])+' → '+str(row['actual']),table,row['id'],{'type':'repair_set','table':table,'id':row['id'],'fields':{'quote':row['actual']}})
        for row in db.query('SELECT media_id,trans_name FROM media WHERE collection_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM collection WHERE id=media.collection_id)'):
            issue('collection','清除不存在的系列：'+str(row['trans_name'] or row['media_id']),'media',row['media_id'],{'type':'repair_set','table':'media','id':row['media_id'],'fields':{'collection_id':None}})
        for row in db.query("SELECT file_id,session,episode FROM file WHERE (session IS NOT NULL AND (typeof(session)!='integer' OR session<0 OR session>9999)) OR (episode IS NOT NULL AND (typeof(episode)!='integer' OR episode<0 OR episode>9999))"):
            fields={key:0 for key in ('session','episode') if row[key] is not None and (not isinstance(row[key],int) or row[key]<0 or row[key]>9999)}
            issue('episodes','文件 #'+str(row['file_id'])+' 无效季/集号归零','file',row['file_id'],{'type':'repair_set','table':'file','id':row['file_id'],'fields':fields})
        for row in db.query('SELECT insert_id,user_id,media_id FROM user_media WHERE last_seen_file_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM file_media WHERE media_id=user_media.media_id AND file_id=user_media.last_seen_file_id)'):
            issue('last-seen','用户 '+str(row['user_id'])+' 上次观看文件不属于影片 #'+str(row['media_id']),'user_media',row['insert_id'],{'type':'repair_set','table':'user_media','id':row['insert_id'],'fields':{'last_seen_file_id':None}})
        for violation in db.query('PRAGMA foreign_key_check'):
            table=violation['table']
            if table not in {'file_media','library_file','lib_dir','share','user_file','user_media'}: continue
            key=TABLE_KEYS[table]; row=db.one('SELECT '+key+' AS id FROM '+table+' WHERE rowid=?',(violation['rowid'],))
            if row and not any(i['id']=='dangling-'+table+':'+str(row['id']) for i in issues):
                issue('dangling-'+table,'删除失效关联 '+table+' #'+str(row['id']),table,row['id'],{'type':'repair_delete','table':table,'id':row['id']})
        expected=[]
        for row in snapshot['media']:
            title=(row['trans_name']+' '+row['season_name']) if row['trans_name'] is not None and row.get('season_name') else row['trans_name']
            expected.append({'rowid':row['media_id'],**{key:(title if key=='trans_name' else row.get(key)) for key in SEARCH_FIELDS}})
        if expected!=snapshot['media_search']: issue('search','重建官方搜索索引（修复缺失、过期或多余条目）','media_search',0,{'type':'search_rebuild'})
        counts={}
        for row in issues: counts[row['kind']]=counts.get(row['kind'],0)+1
        visible=issues[:500]; scan={'id':uuid.uuid4().hex,'user':user,'created':int(time.time()),'beforeHash':fingerprint(snapshot),'issues':visible}
        atomic(storage(user)/'references'/(scan['id']+'.json'),scan)
        return {'scanId':scan['id'],'issues':[{k:v for k,v in i.items() if k!='command'} for i in visible],'counts':counts,'total':len(issues),'limit':500,
                'orphanMedia':db.one('SELECT count(*) AS n FROM media WHERE NOT EXISTS(SELECT 1 FROM file_media WHERE media_id=media.media_id)')['n'],
                'note':'只扫描，不写入。无关联影片保留，可在全库模式编辑或重分配；每次最多预览 200 项，修复后可重新扫描。'}


def preview_repair(user,request):
    if SCOPE.get()!='all' or not ADMIN.get(): raise ValueError('全库修复需要管理员全库模式')
    scan=read_record(storage(user)/'references'/(token(request.get('scanId'))+'.json'))
    if scan.get('user')!=user or time.time()-scan['created']>900: raise ValueError('检查结果已过期，请重新扫描')
    choices=request.get('issues')
    if not isinstance(choices,list) or not 1<=len(choices)<=200 or any(not isinstance(x,str) for x in choices) or len(set(choices))!=len(choices): raise ValueError('请选择 1 至 200 项修复')
    available={i['id']:i for i in scan['issues']}
    if set(choices)-available.keys(): raise ValueError('未知修复项目')
    with editing_lock(),simulation(user) as db:
        if fingerprint(logical_snapshot(db))!=scan['beforeHash']: raise ValueError('扫描后媒体库已变化，请重新扫描')
        commands=[i['command'] for i in scan['issues'] if i['id'] in choices]
        return make_structural_plan(user,db,commands,'repair')
