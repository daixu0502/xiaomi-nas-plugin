"""Build and content-verify release archives without runtime data or credentials."""
import hashlib
import gzip
import io
import json
from pathlib import Path
import tarfile
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'workspace-tools'))
from version_catalog import load
from sync_versions import synchronize
VERSIONS=load()['plugins']
PRIVILEGED={'dockermanager':'dockermanager','taskcenter':'taskcenter',
            'sshrecovery':'sshrecovery','mihomo':'mihomo','nascenter':'nascenter'}


def package_paths(base):
    """Installation artifacts are not source/developer archives."""
    paths=[base/'INFO',base/'manage.sh',base/'README.md']
    paths += [base/name for name in ('LICENSE','THIRD_PARTY_NOTICES.md','UPSTREAM.json') if (base/name).is_file()]
    for directory in ('src','scripts','etc','installer','docs'):
        paths += [p for p in (base/directory).rglob('*') if p.is_file()]
    return sorted(paths)


def validate_component(base,folder):
    runtime=base/'src/files/plugin_paths.py'
    if runtime.exists():
        assert runtime.read_bytes().replace(b'\r\n',b'\n')==(ROOT/'workspace-tools/plugin-runtime/plugin_paths.py').read_bytes().replace(b'\r\n',b'\n'),folder+': stale user runtime helper'
        assert (base/'src/files/service_control.sh').read_bytes().replace(b'\r\n',b'\n')==(ROOT/'workspace-tools/plugin-runtime/service_control.sh').read_bytes().replace(b'\r\n',b'\n'),folder+': stale service control bridge'
    assert not (base/'payload').exists(),folder+': duplicate legacy resource tree'
    common=base/'installer/common'
    if folder not in PRIVILEGED:
        assert not common.exists(),folder+': unneeded privileged dependency'
        return
    policy=json.loads((common/'manifest.json').read_text(encoding='utf-8'))
    assert policy['plugin']==PRIVILEGED[folder] and policy['version']==load()['components']['privileged']
    for relative,digest in policy['files'].items():
        target=common/relative;assert target.is_file() and not target.is_symlink()
        value=target.read_bytes().replace(b'\r\n',b'\n')
        assert hashlib.sha256(value).hexdigest()==digest,folder+': stale component manifest'
        if relative.startswith('modules/'):
            assert relative.startswith('modules/'+PRIVILEGED[folder]+'/'),folder+': foreign private module'
        else:
            source=ROOT/'workspace-tools/privileged-component'/relative
            assert source.read_bytes().replace(b'\r\n',b'\n')==value,folder+': run sync-privileged-component.py before packaging'


def build(only=None):
    global VERSIONS
    VERSIONS=load()['plugins']
    if only:assert set(only)<=VERSIONS.keys(),'unknown plugin ID'
    synchronize()
    destination=Path(__file__).resolve().parent/'package-manifest.json'
    manifest=json.loads(destination.read_text(encoding='utf-8')) if only and destination.exists() else {}
    for folder,version in VERSIONS.items():
        if only and folder not in only:continue
        base=ROOT/folder
        validate_component(base,folder)
        info=json.loads((base/'INFO').read_text(encoding='utf-8'))
        assert info['version']==version and info['type']=='standard' and info['system'] is False and info['ext']=={}
        assert base.name==info['plugin']==folder
        assert {'plugin','name','id','version','tags','timestamp','desc','developer','publisher','changelog','system','size','port','type','forceupgrade','ext'}<=info.keys()
        for name in ('scripts/control','src/ui/index.html','src/ui/'+folder+'.cgi'):
            assert (base/name).is_file(),folder+': missing '+name
        assert (base/'src/files').is_dir()
        assert isinstance(info['port'],str) and info['id']>=98
        frontend=json.loads((base/'src/ui/config').read_text(encoding='utf-8'))
        assert all('#/' in entry['url'] for entry in frontend['url'])
        icon=(base/'src/ui/icon.png').read_bytes()
        assert icon[:8]==b'\x89PNG\r\n\x1a\n' and icon[16:24]==(300).to_bytes(4,'big')*2 and icon[25]==6
        recovery=ROOT/'workspace-tools/plugin-recovery/recovery.py'
        assert (base/'installer/recovery/recovery.py').read_bytes()==recovery.read_bytes(),folder+': stale recovery service'
        paths=package_paths(base)
        paths=[p for p in paths if not any(part in p.relative_to(base).parts for part in ['.git','__pycache__','tests','__tests__','releases','artifacts','screenshots','dist','offline','local','.cache','.test-cache','.pytest_cache','.tools','.deps','output','node_modules','target']) and p.suffix not in ['.pyc','.tar','.gz','.zip']]
        output=(ROOT/'dist')/(folder+'-v'+version+'.tar.gz')
        output.parent.mkdir(parents=True,exist_ok=True);expected={}
        with output.open('wb') as stream, gzip.GzipFile(filename='',mode='wb',fileobj=stream,mtime=0) as compressed, tarfile.open(fileobj=compressed,mode='w') as tf:
            for path in sorted(paths):
                assert not path.is_symlink(),str(path)
                data=path.read_bytes()
                if data.startswith(b'#!') or path.suffix in ['.md','.py','.js','.cjs','.html','.css','.cgi','.sh','.svg','.json','.yaml','.conf']:
                    data=data.replace(b'\r\n',b'\n')
                name=path.relative_to(ROOT).as_posix();expected[name]=hashlib.sha256(data).hexdigest()
                info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o755 if data.startswith(b'#!') else 0o644;tf.addfile(info,io.BytesIO(data))
        with tarfile.open(output) as tf:
            actual={p.name:hashlib.sha256(tf.extractfile(p).read()).hexdigest() for p in tf.getmembers()}
            assert actual==expected,folder+': archive/source mismatch'
        manifest[folder]={'version':version,'file':output.relative_to(ROOT).as_posix(),'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'files':len(expected)}
    destination.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    catalog={key:value for key,value in manifest.items() if key in VERSIONS}
    distribution=ROOT
    (distribution/'package-manifest.json').write_text(json.dumps(catalog,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (distribution/'dist/SHA256SUMS').write_text(''.join(value['sha256']+'  '+Path(value['file']).name+'\n' for value in catalog.values()),encoding='utf-8')
    print(json.dumps(manifest,ensure_ascii=False,indent=2))


if __name__=='__main__':build(sys.argv[1:] or None)
