"""Build signed offline suites and dependency-separated online update payloads."""
import argparse
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
MANAGER = ROOT / 'plugins/common/manager'
sys.path.insert(0, str(MANAGER))
sys.path.insert(0, str(ROOT / 'workspace-tools/sync'))
from layout import OWNERSHIP, PLUGINS, PRIVILEGED, SERVERS, file_contract
from packages import Bundle, COMPONENT_FILES, code_version
from adapters import DEPENDENCIES, PORTS

IGNORED = {'__pycache__', 'tests', '__tests__', '.cache', '.deps', '.tools', 'local', 'dist', 'node_modules'}
CORE_BINARY = {'mihomo', 'aria2c', 'qbittorrent-nox'}


def load_script(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def executable(path):
    with path.open('rb') as stream:
        prefix = stream.read(4)
    return prefix.startswith(b'#!') or prefix == b'\x7fELF'


def put(path, data, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    path.chmod(0o755 if executable else 0o644)


def copy(source, destination):
    data = source.read_bytes()
    if data.startswith(b'#!') or source.suffix in ('.py', '.sh', '.cgi', '.md', '.json', '.js', '.css', '.html', '.yaml', '.txt') or source.name in ('INFO', 'config', 'control', 'hotplug', 'LICENSE'):
        data = data.replace(b'\r\n', b'\n')
    put(destination, data, data.startswith(b'#!') or source.name in CORE_BINARY)


def archive(root, paths, output):
    with output.open('wb') as stream, gzip.GzipFile(fileobj=stream, mode='wb', mtime=0) as gz, tarfile.open(fileobj=gz, mode='w') as tf:
        for path in sorted(p for p in paths if p.is_file()):
            if path.is_symlink():
                raise ValueError('invalid package file: ' + str(path))
            member = tarfile.TarInfo(path.relative_to(root).as_posix())
            member.size = path.stat().st_size
            member.mode = 0o755 if executable(path) else 0o644
            with path.open('rb') as source:
                tf.addfile(member, source)
    return dict(asset=output.name, size=output.stat().st_size, sha256=sha(output))


def fetch(url, checksum, cache):
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.is_file() or sha(cache) != checksum:
        print('Fetching pinned core: ' + cache.name, flush=True)
        temporary = cache.with_suffix(cache.suffix + '.part')
        try:
            with urllib.request.urlopen(url, timeout=120) as source, temporary.open('wb') as target:
                shutil.copyfileobj(source, target)
            if sha(temporary) != checksum:
                raise ValueError('upstream checksum mismatch: ' + cache.name)
            os.replace(temporary, cache)
        finally:
            temporary.unlink(missing_ok=True)
    return cache


def cores(stage):
    cache = ROOT / 'workspace-tools/private/cache/offline-cores'
    release = (ROOT / 'plugins/mihomo/src/files/core-release.sh').read_text()
    version = re.search(r"CORE_VERSION='([^']+)'", release)[1]
    digest = re.search(r"CORE_SHA256='([^']+)'", release)[1]
    asset = 'mihomo-linux-arm64-' + version + '.gz'
    compressed = fetch('https://github.com/MetaCubeX/mihomo/releases/download/' + version + '/' + asset, digest, cache / asset)
    put(stage / 'plugins/mihomo/src/files/mihomo', gzip.decompress(compressed.read_bytes()), True)
    aria = load_script(ROOT / 'plugins/downloadcenter/src/files/fetch_core.py', 'suite_aria')
    name, digest = aria.ASSETS['aarch64']
    archive_path = fetch('https://github.com/abcfy2/aria2-static-build/releases/download/1.37.0/' + name, digest, cache / name)
    aria.install(stage / 'plugins/downloadcenter/src/files/aria2c', 'aarch64', archive_path)
    qb = load_script(ROOT / 'plugins/downloadcenter/src/files/fetch_qb.py', 'suite_qb')
    name = 'aarch64-qbittorrent-nox'
    binary = fetch('https://github.com/userdocs/qbittorrent-nox-static/releases/download/' + qb.RELEASE + '/' + name,
                   qb.ASSETS['aarch64'], cache / name)
    qb.install(stage / 'plugins/downloadcenter/src/files/qbittorrent-nox', 'aarch64', binary)
    return {
        'mihomo': dict(version=version, architecture='aarch64', archiveSHA256=sha(compressed),
            binarySHA256=sha(stage/'plugins/mihomo/src/files/mihomo'),
            origin='https://github.com/MetaCubeX/mihomo/releases/tag/'+version,
            source='https://github.com/MetaCubeX/mihomo/tree/'+version,
            notices='plugins/mihomo/THIRD_PARTY_NOTICES.md'),
        'aria2': dict(version='1.37.0', architecture='aarch64', archiveSHA256=sha(archive_path),
            binarySHA256=sha(stage/'plugins/downloadcenter/src/files/aria2c'),
            origin='https://github.com/abcfy2/aria2-static-build/releases/tag/1.37.0',
            source='https://github.com/aria2/aria2/tree/release-1.37.0',
            notices='plugins/downloadcenter/THIRD_PARTY_NOTICES.md'),
        'qbittorrent': dict(version=qb.VERSION, architecture='aarch64', archiveSHA256=sha(binary),
            binarySHA256=sha(stage/'plugins/downloadcenter/src/files/qbittorrent-nox'),
            origin='https://github.com/userdocs/qbittorrent-nox-static/releases/tag/'+qb.RELEASE,
            source='https://github.com/qbittorrent/qBittorrent/tree/release-'+qb.VERSION,
            notices='plugins/downloadcenter/THIRD_PARTY_NOTICES.md')}


def sign(root, manifest, key):
    put(root / 'suite.json', json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n')
    subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(key), '-out', str(root / 'suite.sig'),
                    str(root / 'suite.json')], check=True, capture_output=True)
    subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(MANAGER / 'release-public-key.txt'),
                    '-signature', str(root / 'suite.sig'), str(root / 'suite.json')], check=True, capture_output=True)


def manifest_for(stage, plugins, components, core_versions):
    names = set(components) - ({'privileged'} if not set(plugins) & PRIVILEGED else set())
    selected = {}
    for path in sorted(stage.rglob('*')):
        if not path.is_file() or path.name in ('suite.json', 'suite.sig'):
            continue
        relative = path.relative_to(stage).as_posix()
        parts = Path(relative).parts
        if parts[0] == 'plugins' and parts[1] not in plugins:
            continue
        if parts[0] == 'components' and parts[1] not in names:
            continue
        selected[relative] = dict(size=path.stat().st_size, sha256=sha(path),
                                 executable=executable(path), install=file_contract(relative, executable(path)),
                                 owner=('plugin:' + parts[1]) if parts[0] == 'plugins' else ('component:' + parts[1]) if parts[0] == 'components' else 'suite')
    return dict(format=2, architecture='aarch64', ownership=OWNERSHIP,
                plugins={p: {'version': json.loads((ROOT / 'plugins' / p / 'INFO').read_text())['version']} for p in plugins},
                components={name: components[name] for name in sorted(names)}, cores=core_versions, files=selected)


def build(key=None, release=None):
    key = Path(key or ROOT / 'workspace-tools/private/signing/private.pem').resolve()
    if not key.is_file():
        raise ValueError('需要已有发布签名私钥；不会创建或提交私钥')
    from sync_versions import synchronize
    synchronize()
    for name, call in (('sync-plugin-updater.py', 'synchronize'), ('sync-ui-controls.py', 'synchronize')):
        getattr(load_script(ROOT / 'workspace-tools/sync' / name, name), call)(check=True)
    destination = ROOT / 'dist'
    destination.mkdir(exist_ok=True)
    stage_root = ROOT / 'workspace-tools/private/cache'
    stage_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='suite-', dir=stage_root) as temporary:
        stage = Path(temporary)
        copy(ROOT / 'manage.sh', stage / 'manage.sh')
        copy(MANAGER / 'README.md', stage / 'README.md')
        for plugin in PLUGINS:
            source = ROOT / 'plugins' / plugin
            for name in ('INFO', 'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md'):
                if (source / name).is_file():
                    copy(source / name, stage / 'plugins' / plugin / name)
            for name in ('src', 'scripts', 'etc', 'docs'):
                for path in (source / name).rglob('*'):
                    if not path.is_file() or any(p in IGNORED for p in path.relative_to(source).parts) or path.name in CORE_BINARY:
                        continue
                    copy(path, stage / 'plugins' / plugin / path.relative_to(source))
            if plugin in PRIVILEGED:
                private = source / 'installer/common/modules' / plugin
                for path in private.rglob('*'):
                    if path.is_file() and '__pycache__' not in path.parts:
                        copy(path, stage / 'plugins' / plugin / path.relative_to(source))
            descriptor = dict(format=2, plugin=plugin, privileged=plugin in PRIVILEGED,
                              service=plugin in SERVERS, dependencies=DEPENDENCIES.get(plugin, ()),
                              ports=PORTS.get(plugin, ()))
            expected = json.loads(json.dumps(descriptor))
            actual = json.loads((source / 'installer/plugin.json').read_text())
            if actual != expected:
                raise ValueError('插件安装描述与支持契约不一致：' + plugin)
            copy(source / 'installer/plugin.json', stage / 'plugins' / plugin / 'installer/plugin.json')
        sources = {'manager': MANAGER, 'privileged': ROOT / 'plugins/common/privileged',
                   'recovery': ROOT / 'plugins/common/recovery', 'updater': ROOT / 'plugins/common/updater'}
        versions = {'manager': code_version(MANAGER / 'layout.py'),
                    'privileged': code_version(sources['privileged'] / 'common/privileged_runtime.py'),
                    'recovery': code_version(sources['recovery'] / 'recovery.py'),
                    'updater': code_version(sources['updater'] / 'updater.py')}
        components = {}
        for name, files in COMPONENT_FILES.items():
            for relative in files:
                copy(sources[name] / relative, stage / 'components' / name / relative)
            output = destination / ('minas-' + name + '-v' + versions[name] + '.tar.gz')
            components[name] = dict(version=versions[name], **archive(stage, (stage / 'components' / name).rglob('*'), output))
        core_versions = cores(stage)
        catalog = {}
        for plugin in PLUGINS:
            manifest = manifest_for(stage, (plugin,), components, core_versions)
            sign(stage, manifest, key)
            paths = [stage / name for name in manifest['files'] if not name.startswith('components/')]
            paths += [stage / 'suite.json', stage / 'suite.sig']
            plugin_version = manifest['plugins'][plugin]['version']
            item = archive(stage, paths, destination / (plugin + '-v' + plugin_version + '.tar.gz'))
            catalog[plugin] = dict(version=plugin_version, file='dist/' + item['asset'], sha256=item['sha256'],
                                   size=item['size'], files=len(paths), components=list(manifest['components']))
        manifest = manifest_for(stage, PLUGINS, components, core_versions)
        sign(stage, manifest, key)
        Bundle(stage)
        release = release or 'manager-' + versions['manager']
        if not re.fullmatch(r'[A-Za-z0-9._-]+', release):
            raise ValueError('无效套件发布标识')
        suite = archive(stage, [p for p in stage.rglob('*') if p.is_file()],
                        destination / ('xiaomi-nas-suite-' + release + '-arm64.tar.gz'))
        put(destination / 'suite-manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        for path in (ROOT / 'package-manifest.json',):
            put(path, json.dumps(catalog, ensure_ascii=False, indent=2) + '\n')
        release_files = [destination / Path(v['file']).name for v in catalog.values()]
        release_files += [destination / v['asset'] for v in components.values()] + [destination / suite['asset']]
        put(destination / 'SHA256SUMS', ''.join(sha(p) + '  ' + p.name + '\n' for p in release_files))
        put(destination / 'suite-artifacts.json', json.dumps(dict(suite=suite, components=components), indent=2) + '\n')
        print(json.dumps(dict(suite=suite, plugins=catalog, components=components), ensure_ascii=False, indent=2))
        return catalog


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--signing-key', type=Path)
    parser.add_argument('--release')
    args = parser.parse_args()
    build(args.signing_key, args.release)
