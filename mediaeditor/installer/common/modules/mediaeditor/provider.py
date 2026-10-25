"""Version-pinned local official search. Auth remains inside the NAS library."""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
import uuid

LIBRARY = Path('/usr/lib/libmc_xunlei.so')
SUPPORTED = {'03d48880d84855404389e522ab5a5ad3f3a8a10c9e6a35300a065ec2c169ab9d'}


def available():
    try:
        s=LIBRARY.lstat()
        return stat.S_ISREG(s.st_mode) and s.st_uid==0 and not s.st_mode&0o022 and hashlib.sha256(LIBRARY.read_bytes()).hexdigest() in SUPPORTED
    except OSError: return False


def search(user,request):
    import store
    keyword=request.get('keyword','')
    if not isinstance(keyword,str) or not 1<=len(keyword.strip())<=100 or '\x00' in keyword: raise ValueError('请输入 1 至 100 字的影片名称')
    if not available(): raise ValueError('官方刮削库版本尚未适配；本地浏览和编辑仍可使用')
    with store.editing_lock():
        env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LC_ALL':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1','HOME':'/root'}
        process=subprocess.Popen(['/usr/bin/python3','-I','-B',str(Path(__file__).resolve()),'search'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,cwd='/',start_new_session=True)
        try: out,_=process.communicate(json.dumps({'keyword':keyword.strip()}).encode(),timeout=40)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL);process.communicate();raise ValueError('官方搜索超时，请稍后重试')
        if process.returncode or len(out)>1048576: raise ValueError('官方搜索失败，请稍后重试')
        value=json.loads(out)
        if not value.get('ok'): raise ValueError('官方刮削源暂未返回可用结果')
        results=value['results']
        with store.database() as db:
            for row in results:
                col=db.one('SELECT id FROM collection WHERE identifier=?',(row['seriesIdentifier'],)) if row['seriesIdentifier'] else None
                row['localCollectionId']=col['id'] if col else None
                local=db.one('''SELECT DISTINCT m.media_id FROM media m JOIN file_media fm ON fm.media_id=m.media_id
                    JOIN library_file lf ON lf.file_id=fm.file_id JOIN library l ON l.library_id=lf.library_id WHERE m.midb_id=? AND l.user_id=?''',(row['code'],store.uid(user)))
                row['existingMediaId']=local['media_id'] if local else None
        reference={'id':uuid.uuid4().hex,'user':user,'created':int(time.time()),'keyword':keyword.strip(),'results':results}
        store.atomic(store.storage(user)/'references'/(reference['id']+'.json'),reference)
        return {'searchId':reference['id'],'results':results,'provider':'官方迅雷刮削源'}


def worker():
    import ctypes as C
    import resource
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    resource.setrlimit(resource.RLIMIT_AS,(768*1024*1024,768*1024*1024))
    raw=sys.stdin.buffer.read(4097)
    if len(raw)>4096 or not available(): raise ValueError('unsupported request')
    keyword=json.loads(raw)['keyword']
    if not isinstance(keyword,str) or not 1<=len(keyword)<=100: raise ValueError('invalid keyword')
    lib=C.CDLL(str(LIBRARY));fn=lib.mc_scraper_search
    fn.argtypes=[C.c_char_p,C.c_int,C.POINTER(C.c_void_p)];fn.restype=C.c_int
    libc=C.CDLL(None);libc.free.argtypes=[C.c_void_p]
    pointer=C.c_void_p()
    rc=fn(keyword.encode(),20,C.byref(pointer))
    results=[]
    if pointer:
        try:
            data=json.loads(C.string_at(pointer))
            if rc==0:
                for row in data.get('data',[])[:20]:
                    code=row.get('id');kind=row.get('class')
                    if not isinstance(code,str) or not re.fullmatch(r'[a-f0-9]{32}',code) or kind not in ('movie','tv'): continue
                    cid=row.get('collection_id')
                    cid=cid if isinstance(cid,int) and not isinstance(cid,bool) and 0<cid<2147483647 else 0
                    identifier=('tmdb_movie_' if kind=='movie' else 'tmdb_')+str(cid) if cid else ''
                    def text(key,limit=300): return str(row.get(key) or '')[:limit]
                    results.append({'code':code,'title':text('title'),'originalTitle':text('origin_name'),'year':row.get('year'),
                        'type':kind,'sourceCollectionId':cid or None,'seriesIdentifier':identifier,'seriesName':text('collection_name'),
                        'season':row.get('season',0),'seasonName':text('season_name')})
        finally: libc.free(pointer)
    print(json.dumps({'ok':rc==0,'results':results},ensure_ascii=False))


if __name__=='__main__':
    try:
        if sys.argv[1:]!=['search'] or os.geteuid()!=0: raise ValueError('invalid mode')
        worker()
    except BaseException:
        print('{"ok":false,"results":[]}');sys.exit(1)
