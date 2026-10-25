"""Read-only audit of the shared privileged component in current local packages."""
import hashlib
import json
from pathlib import Path
import re
import tarfile
from version_catalog import load

ROOT = Path(__file__).resolve().parents[1]
PROJECTS={'dockermanager':'dockermanager','taskcenter':'taskcenter','sshrecovery':'sshrecovery','mihomo':'mihomo','nascenter':'nascenter','quickshare':'quickshare','downloadcenter':'downloadcenter'}
FILES=('minas-helper','component_admin.py','common/privileged_runtime.py','common/component_admin.py','common/user_state_io.py','common/telemetry_cache.py')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    manifest = json.loads((ROOT / 'workspace-tools/package-manifest.json').read_text())
    reference={name:(ROOT/'workspace-tools/privileged-component'/name).read_bytes().replace(b'\r\n',b'\n') for name in FILES}
    version=load()['components']['privileged']
    report={'commonVersion':version,'packages':{}}
    for folder,plugin in PROJECTS.items():
        entry=manifest[folder];archive=ROOT/entry['file']
        assert sha(archive.read_bytes())==entry['sha256'],folder+': archive mismatch'
        with tarfile.open(archive) as package:
            members=[m.name.removeprefix(folder+'/') for m in package.getmembers()]
            assert not any(p.startswith(('payload/system/','tests/','tools/')) for p in members),folder+': developer files in installation package'
            prefix=folder+'/installer/common/'
            included={m.name.removeprefix(prefix):package.extractfile(m).read() for m in package.getmembers() if m.isfile() and m.name.startswith(prefix)}
            if plugin in ('quickshare','downloadcenter'):
                assert not included and not (ROOT/folder/'installer/common').exists()
                installer=package.extractfile(folder+'/manage.sh').read()
                assert b'component_admin.py' not in installer and b'.minas-privileged' not in installer
                report['packages'][folder]={'version':entry['version'],'archiveVerified':True,'noComponentDependency':True}
                continue
            policy=json.loads(included.pop('manifest.json'))
            assert policy['plugin']==plugin and policy['version']==version
            assert set(included)==set(policy['files'])
            assert all(sha(value)==policy['files'][name] for name,value in included.items())
            assert all(included[name]==value for name,value in reference.items())
            private=[p for p in included if p.startswith('modules/')]
            assert all(p.startswith('modules/'+plugin+'/') for p in private)
            assert bool(private)==(plugin not in ('quickshare','downloadcenter'))
            for name,value in included.items():assert (ROOT/folder/'installer/common'/name).read_bytes().replace(b'\r\n',b'\n')==value
            installer=package.extractfile(folder+'/manage.sh').read()
            if plugin=='sshrecovery':installer=package.extractfile(folder+'/installer/install.py').read()
            assert b'component_admin.py' in installer and b'install' in installer
            report['packages'][folder]={'version':entry['version'],'commonVersion':version,'privateFiles':len(private),'archiveVerified':True,'noForeignModules':True}

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
