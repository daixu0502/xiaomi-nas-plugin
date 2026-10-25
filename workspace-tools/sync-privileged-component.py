"""Sync shared code; private modules are maintained once, in their final path."""
import hashlib
import json
from pathlib import Path
from version_catalog import load
from sync_versions import synchronize as synchronize_versions

ROOT=Path(__file__).resolve().parents[1]
CANON=ROOT/'workspace-tools/privileged-component'
PROJECTS={'dockermanager':'dockermanager','taskcenter':'taskcenter','sshrecovery':'sshrecovery','mihomo':'mihomo','nascenter':'nascenter','mediaeditor':'mediaeditor'}
MODULE_FILES={'dockermanager':('docker-manager-helper',),'taskcenter':('task-center-helper','task_schedule.py','task_maintenance.py'),'sshrecovery':('ssh-recovery-helper','restore-ssh.sh'),'mihomo':('api.py','mihomo-docker-proxy','docker_proxy_status.py'),'nascenter':('api.py','nas-center-helper','process_metrics.py'),'mediaeditor':('api.py','store.py','sqlite_native.py','provider.py')}
CORE=('minas-helper','component_admin.py','common/privileged_runtime.py','common/component_admin.py','common/user_state_io.py','common/telemetry_cache.py')

def synchronize():
    synchronize_versions()
    version=load()['components']['privileged']
    for folder in ('quickshare','downloadcenter'):
        target=ROOT/folder/'installer/common'
        if target.exists():
            # Remove only the known generated bundle, never unrelated assets.
            for path in target.rglob('*'):
                assert not path.is_symlink(),str(path)
                if path.is_file():
                    assert path.relative_to(target).as_posix() in set(CORE)|{'manifest.json'},str(path)
            for path in sorted(target.rglob('*'),key=lambda p:len(p.parts),reverse=True):
                if path.is_file():path.unlink()
                elif path.is_dir():path.rmdir()
            target.rmdir()
    # Private code is authoritative at its installable path. Never create a
    # second maintenance tree or overwrite a private module during shared sync.
    for plugin,folder in PROJECTS.items():
        assert not (ROOT/folder/'payload/system').exists(),folder+': duplicate legacy source tree'
        files={name:(CANON/name).read_bytes().replace(b'\r\n',b'\n') for name in CORE}
        target=ROOT/folder/'installer/common'
        for name in MODULE_FILES[plugin]:
            relative='modules/'+plugin+'/'+name
            path=target/relative
            assert path.is_file() and not path.is_symlink(),str(path)
            files[relative]=path.read_bytes().replace(b'\r\n',b'\n')
        expected=set(files)|{'manifest.json'}
        if target.exists():
            for p in target.rglob('*'):
                assert not p.is_symlink(),str(p)
                if p.is_file():assert p.relative_to(target).as_posix() in expected,'Unexpected component asset: '+str(p)
        for name,value in files.items():
            if name in CORE:
                p=target/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(value)
        manifest={'apiVersion':1,'layoutVersion':3,'version':version,'plugin':plugin,'files':{name:hashlib.sha256(value).hexdigest() for name,value in sorted(files.items())}}
        (target/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(folder+': common '+version+' + '+str(len(files)-len(CORE))+' private files')

if __name__=='__main__':synchronize()
