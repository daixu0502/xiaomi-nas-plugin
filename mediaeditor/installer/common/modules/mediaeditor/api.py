"""Narrow mediaeditor module for the existing shared sudo entry point."""
import os
import sys
from pathlib import Path
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parent))
import privileged_runtime as runtime
import store
import provider

ACTIONS={'status','list','details','search','preview','apply','history','preview_undo'}


def api(user,request):
    runtime.authorize(user,'mediaeditor')
    action=request.get('action')
    if action not in ACTIONS: raise ValueError('未知影视库编辑操作')
    if action=='status':
        store.storage(user)
        with store.database() as db:
            libs=store.libraries(db,user)
        return {'officialSearch':provider.available(),'libraries':libs,'storage':str(store.STATE/user),
            'scope':'仅当前账号拥有且未共享给其他账号的媒体记录','maxBatch':100}
    if action=='list': return store.list_media(user,request)
    if action=='details': return store.details(user,store.ident(request.get('mediaId')))
    if action=='search': return provider.search(user,request)
    if action=='preview': return store.preview(user,request)
    if action=='apply': return store.execute(user,request.get('planId'))
    if action=='history': return store.history(user)
    return store.preview_undo(user,request.get('planId'))
