"""Owned-row edits with protected plans, WAL snapshots and conflict-aware undo."""
import contextlib
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
import tempfile
import time
import uuid

from sqlite_native import Database
import privileged_runtime as runtime

DB_PATH = Path('/nas/sys/video/mediacenter.db')
TOKENIZER = Path('/usr/lib/libmitrigram.so')
STATE = Path('/data/minas/plugins/mediaeditor')
LABELS = {'midb_id':'影片代码','trans_name':'片名','season_number':'影片季号','season_name':'季名称',
          'collection_id':'系列归属','session':'文件季号','episode':'文件集号'}
MEDIA_FIELDS = {'midb_id','trans_name','season_number','season_name','collection_id'}
FILE_FIELDS = {'session','episode'}


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
    if not STATE.parent.exists(): runtime.secure_directory(STATE.parent,create=True,mode=0o711)
    runtime.secure_directory(STATE, create=True, mode=0o700)
    directory = runtime.secure_directory(STATE / user, create=True, mode=0o700)
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
    if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o077 or s.st_size > 2097152:
        raise ValueError('编辑记录不安全')
    return json.loads(path.read_text())


def token(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-f0-9]{32}',value): raise ValueError('无效编辑记录编号')
    return value


@contextlib.contextmanager
def editing_lock():
    if not STATE.parent.exists(): runtime.secure_directory(STATE.parent,create=True,mode=0o711)
    runtime.secure_directory(STATE,create=True,mode=0o700)
    fd=os.open(STATE/'write.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        s=os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o077: raise ValueError('编辑锁不安全')
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('另一项影视编辑正在进行，请稍后再试')
        yield
    finally: os.close(fd)


def database(writable=False):
    metadata=DB_PATH.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid!=0 or metadata.st_mode&0o022:
        raise ValueError('官方媒体库路径或权限异常')
    return Database(DB_PATH,writable, TOKENIZER if writable else None)


def uid(user): return pwd.getpwnam(user).pw_uid


def libraries(db,user):
    return db.query('SELECT library_id AS id,name,scan_status FROM library WHERE user_id=? ORDER BY library_id',(uid(user),))


def series_list(db,user):
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
    if not any(r['user_id']==uid(user) for r in refs): raise ValueError('不能访问其他账号的媒体记录')
    editable=all(r['user_id']==uid(user) and r['share_count']==0 for r in refs)
    if write and not editable: raise ValueError('记录被其他账号或共享库引用，已拒绝直接修改共享元数据')
    return {'table':table,'id':record_id,'row':row,'refs':refs,'editable':editable}


def list_media(user,request):
    library_id=integer(request.get('libraryId',0),'媒体库编号')
    q=request.get('query','')
    if not isinstance(q,str) or len(q)>100: raise ValueError('片名查询长度无效')
    with database() as db:
        params=[uid(user)];where='l.user_id=?'
        if library_id:
            where+=' AND l.library_id=?';params.append(library_id)
        if q.strip():
            where+=' AND (m.trans_name LIKE ? OR m.origin_name LIKE ? OR m.midb_id=?)'
            params.extend(['%'+q.strip()+'%','%'+q.strip()+'%',q.strip()])
        rows=db.query('''SELECT DISTINCT m.media_id,m.trans_name,m.origin_name,m.year,m.class,m.midb_id,
            m.collection_id,m.season_number,c.name AS collection_name,c.identifier AS collection_identifier
            FROM media m JOIN file_media fm ON fm.media_id=m.media_id JOIN library_file lf ON lf.file_id=fm.file_id
            JOIN library l ON l.library_id=lf.library_id LEFT JOIN collection c ON c.id=m.collection_id
            WHERE '''+where+' ORDER BY m.trans_name,m.season_number,m.media_id LIMIT 500',tuple(params))
        return {'libraries':libraries(db,user),'series':series_list(db,user),'medias':rows,'limit':500}


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
            FROM file f JOIN file_media fm ON fm.file_id=f.file_id JOIN library_file lf ON lf.file_id=f.file_id
            JOIN library l ON l.library_id=lf.library_id WHERE fm.media_id=? AND l.user_id=? ORDER BY f.session,f.episode,f.file_path''',(media_id,uid(user)))
        for f in files:
            f['name']=Path(f['file_path']).name
            snapshot=owned(db,user,'file',f['file_id'])
            f['editable']=snapshot['editable'];f['fingerprint']=fingerprint(snapshot)
        return {'media':media,'files':files,'editable':entry['editable'],'ownLibraryIds':[r['library_id'] for r in entry['refs'] if r['user_id']==uid(user)],'fingerprint':fingerprint(entry)}


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
            if other: raise ValueError('影片代码已由另一条媒体记录使用；请先核对文件关联，不能直接覆盖')
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
        plan={'id':uuid.uuid4().hex,'user':user,'created':int(time.time()),'state':'draft','targets':targets,'allowDuplicates':allow}
        atomic(storage(user)/'plans'/(plan['id']+'.json'),plan)
        return public_plan(plan,db)


def public_plan(plan,db=None):
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
        if plan.get('user')==user and plan.get('state')!='draft':
            value=public_plan(plan);value['canUndo']=plan['state']=='applied';value['applied']=plan.get('applied');value['backup']=plan.get('backup')
            result.append(value)
    return {'records':result}


def preview_undo(user,plan_id):
    with editing_lock(),database() as db:
        original=read_record(storage(user)/'plans'/(token(plan_id)+'.json'))
        if original.get('user')!=user or original['state']!='applied': raise ValueError('该修改不能回退')
        targets=[]
        for t in original['targets']:
            current=owned(db,user,t['table'],t['id'],True)
            if current!=t.get('after'): raise ValueError('修改后记录已再次变化，不能直接回退覆盖')
            reverse={k:t['before']['row'].get(k) for k in t['changes']}
            targets.append({'table':t['table'],'id':t['id'],'changes':reverse,'before':current,'seriesRef':None})
        plan={'id':uuid.uuid4().hex,'user':user,'created':int(time.time()),'state':'draft','targets':targets,'allowDuplicates':True,'reverts':original['id']}
        atomic(storage(user)/'plans'/(plan['id']+'.json'),plan)
        return public_plan(plan,db)


def file_fingerprints(user,details_result):
    with database() as db:
        for row in details_result['files']:
            row['fingerprint']=fingerprint(owned(db,user,'file',row['file_id']))
    return details_result
