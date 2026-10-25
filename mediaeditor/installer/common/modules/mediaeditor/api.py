"""Narrow mediaeditor module for the existing shared sudo entry point."""
import os
import sys
from pathlib import Path
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parent))
import privileged_runtime as runtime
import store
import provider

ACTIONS={'status','list','details','search','preview','apply','history','preview_undo','check_code','files','preview_relations','scan_repairs','preview_repair'}


def api(user,request):
    runtime.authorize(user,'mediaeditor')
    with store.scope(user,request.get('scope','owned')):
        return scoped_api(user,request)


def scoped_api(user,request):
    action=request.get('action')
    if action not in ACTIONS: raise ValueError('未知影视库编辑操作')
    if action=='status':
        store.storage(user)
        with store.database() as db:
            libs=store.libraries(db,user)
        with store.database(False,True) as db:
            structural = store.fingerprint(store.structural_schema(db)) in store.SUPPORTED_SCHEMAS
        return {'officialSearch':provider.available(),'libraries':libs,'storage':str(store.STATE/user),
            'scope':'当前账号自有媒体；管理员可切换跨账号全库模式','maxBatch':100,
            'isAdmin':store.ADMIN.get(),'structuralSupported':structural,'forceDuplicateSupported':False}
    if action=='list': return store.list_media(user,request)
    if action=='files': return store.list_files(user,request)
    if action=='details': return store.details(user,store.ident(request.get('mediaId')))
    if action=='search': return provider.search(user,request)
    if action=='preview': return store.preview(user,request)
    if action=='apply': return store.execute(user,request.get('planId'))
    if action=='history': return store.history(user)
    if action=='check_code': return store.check_code(user,request)
    if action=='preview_relations': return store.preview_relations(user,request)
    if action=='scan_repairs': return store.scan_repairs(user)
    if action=='preview_repair': return store.preview_repair(user,request)
    return store.preview_undo(user,request.get('planId'))
