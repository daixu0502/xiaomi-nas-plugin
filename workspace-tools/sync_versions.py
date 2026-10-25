"""Generate release versions in standalone sources; never rewrite history or core versions."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from version_catalog import ROOT, load

SEMVER = r'[0-9]+\.[0-9]+\.[0-9]+'


def plan(root=ROOT):
    """Validate every target before writing any generated file."""
    root = Path(root)
    catalog = load(root)
    pending = {}

    def read(path):
        return pending.get(path, path.read_bytes()).decode('utf-8').replace('\r\n', '\n')

    def replace(path, pattern, value, count=1):
        text, found = re.subn(pattern, lambda m: m[1] + value + m[2], read(path), flags=re.M)
        if found != count:
            raise ValueError(f'{path.relative_to(root)}: expected {count} version fields, found {found}')
        pending[path] = text.encode('utf-8')

    def dump(path, data):
        pending[path] = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')

    for plugin, version in catalog['plugins'].items():
        base = root / plugin
        replace(base / 'manage.sh', r"^(PLUGIN_VERSION=')[^'\n]+(')$", version)
        html = base / 'src/ui/index.html'
        # Match only local CSS/JS cache keys and the plugin version footer.
        pattern = r'((?:href|src)="[^"?:/]+\.(?:css|js)\?v=)' + SEMVER + r'([&"])'
        expected = {'nascenter': 5, 'quickshare': 5}.get(plugin, 4)
        replace(html, pattern, version, expected)
        replace(html, r'(插件版本 )' + SEMVER + r'(?=[ <·])()', version)
        cgi = base / ('src/ui/' + plugin + '.cgi')
        if plugin in ('dockermanager', 'taskcenter', 'nascenter'):
            replace(cgi, r'(\.version\s*//\s*")' + SEMVER + r'(")', version)
            replace(cgi, r"((?:printf '|echo ))" + SEMVER + r"('?\))", version)
        elif plugin == 'quickshare':
            replace(cgi, r'(\.get\("version",\s*")' + SEMVER + r'("\))', version)
            replace(cgi, r'(except Exception: version=")' + SEMVER + r'(")', version)
        elif plugin == 'downloadcenter':
            replace(base / 'src/files/download_lib.py', r'^(VERSION = ")' + SEMVER + r'(")$', version)
        elif plugin == 'sshrecovery':
            replace(base / 'installer/install.py', r"^(PLUGIN, VERSION = 'sshrecovery', ')" + SEMVER + r"(')$", version)
        info_path = base / 'INFO'
        info = json.loads(read(info_path))
        if info['plugin'] != plugin:
            raise ValueError('INFO plugin mismatch: ' + plugin)
        # INFO size describes src only. Ignore Python caches and use generated
        # bytes rather than the old on-disk bytes.
        info['size'] = sum(len(pending.get(p, p.read_bytes()).replace(b'\r\n', b'\n'))
                           for p in (base / 'src').rglob('*') if p.is_file()
                           and '__pycache__' not in p.parts and p.suffix != '.pyc')
        dump(info_path, info)

    recovery = root / 'workspace-tools/plugin-recovery/recovery.py'
    replace(recovery, r"^(VERSION = ')" + SEMVER + r"(')$", catalog['components']['recovery'])
    for plugin in catalog['plugins']:
        pending[root / plugin / 'installer/recovery/recovery.py'] = pending[recovery]

    shared = root / 'workspace-tools/privileged-component/common/privileged_runtime.py'
    replace(shared, r"^(VERSION = ')" + SEMVER + r"(')$", catalog['components']['privileged'])
    for plugin in ('dockermanager', 'mihomo', 'taskcenter', 'nascenter', 'sshrecovery', 'mediaeditor'):
        common = root / plugin / 'installer/common'
        pending[common / 'common/privileged_runtime.py'] = pending[shared]
        manifest_path = common / 'manifest.json'
        manifest = json.loads(read(manifest_path))
        if manifest['plugin'] != plugin:
            raise ValueError('component plugin mismatch: ' + plugin)
        manifest['version'] = catalog['components']['privileged']
        manifest['files'] = {
            name: hashlib.sha256(pending.get(common / name, (common / name).read_bytes()).replace(b'\r\n', b'\n')).hexdigest()
            for name in manifest['files']}
        dump(manifest_path, manifest)

    return {path: data for path, data in pending.items()
            if not path.exists() or path.read_bytes().replace(b'\r\n', b'\n') != data}


def synchronize(root=ROOT, check=False):
    changes = plan(root)
    if check and changes:
        raise ValueError('Generated versions are stale; run workspace-tools/sync_versions.py:\n' +
                         '\n'.join(str(p.relative_to(root)) for p in changes))
    if not check:
        for path, data in changes.items():
            path.write_bytes(data)
    return list(changes)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='fail on drift without changing files')
    args = parser.parse_args()
    changed = synchronize(check=args.check)
    print('Plugin INFO versions consistent' if args.check else f'Generated {len(changed)} files from plugin INFO versions')
