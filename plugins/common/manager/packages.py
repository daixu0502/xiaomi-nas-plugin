"""Signed, complete offline bundles. No network or package-supplied hooks."""
import ast
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile

from layout import OWNERSHIP, PLUGINS, PRIVILEGED, checksum, file_contract, plain, require, version

FORMAT = 2
CORES = {'mihomo': ('mihomo',), 'downloadcenter': ('aria2c', 'qbittorrent-nox')}
MAX_TREE = 2 * 1024 * 1024 * 1024
COMPONENT_FILES = {
    'privileged': ('minas-helper', 'component_admin.py', 'common/privileged_runtime.py',
                   'common/component_admin.py', 'common/user_state_io.py', 'common/telemetry_cache.py',
                   'common/event_wait.py', 'common/maintenance_events.py'),
    'recovery': ('recovery.py',),
    'updater': ('updater.py', 'release-public-key.txt'),
    'manager': ('manager.py', 'layout.py', 'packages.py', 'operations.py', 'adapters.py', 'restoration.py', 'cli.py', 'bootstrap.py', 'release-public-key.txt'),
}


def code_version(path):
    values = [ast.literal_eval(node.value) for node in ast.parse(Path(path).read_text()).body
              if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in node.targets)]
    require(len(values) == 1, '组件 VERSION 不唯一')
    version(values[0])
    return values[0]


def unpack(archive, destination):
    destination = plain(destination)
    destination.mkdir(mode=0o700)
    total, seen = 0, set()
    with tarfile.open(archive, 'r:gz') as stream:
        for member in stream:
            path = PurePosixPath(member.name)
            require(not path.is_absolute() and '..' not in path.parts and path.parts
                    and str(path) == member.name and member.name not in seen, '安装包路径非法或重复')
            require(member.isfile() or member.isdir(), '安装包不允许链接或特殊文件')
            require(not member.mode & 0o7000, '安装包权限非法')
            seen.add(member.name)
            total += member.size
            require(len(seen) <= 20000 and 0 <= member.size <= MAX_TREE and total <= MAX_TREE, '安装包超过大小限制')
            require(shutil.disk_usage(destination).free >= member.size + 64 * 1024 * 1024, '解包空间不足，保留至少 64 MiB 空闲空间')
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(mode=0o700, exist_ok=True)
            else:
                with stream.extractfile(member) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    return destination


class Bundle:
    def __init__(self, root, public_key=None):
        self.root = plain(Path(root).absolute())
        manifest = self.root / 'suite.json'
        signature = self.root / 'suite.sig'
        key = public_key or Path(__file__).with_name('release-public-key.txt')
        require(manifest.is_file() and signature.is_file(), '缺少签名套件清单；请先构建完整离线套件')
        subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(key), '-signature',
                        str(signature), str(manifest)], check=True, capture_output=True, timeout=15)
        self.manifest = json.loads(manifest.read_bytes())
        require(self.manifest.get('format') == FORMAT and self.manifest.get('architecture') == 'aarch64',
                '不支持的套件格式或架构')
        require(self.manifest.get('ownership') == OWNERSHIP, '文件安装位置约定不匹配')
        files = self.manifest['files']
        require(isinstance(files, dict) and 0 < len(files) <= 20000, '清单文件数量异常')
        actual = set()
        total = 0
        for target in self.root.rglob('*'):
            relative = target.relative_to(self.root).as_posix()
            require(not target.is_symlink(), '套件不允许符号链接：' + relative)
            if target.is_dir():
                continue
            require(target.is_file(), '套件包含特殊文件')
            if relative in ('suite.json', 'suite.sig'):
                continue
            actual.add(relative)
            require(relative in files, '套件包含未声明文件：' + relative)
            item = files[relative]
            parts = PurePosixPath(relative).parts
            if parts[0] == 'plugins':
                require(len(parts) >= 3 and parts[1] in self.manifest['plugins'], '文件插件归属错误')
                expected_owner = 'plugin:' + parts[1]
                if parts[2] == 'installer':
                    require(parts[3:] == ('plugin.json',) or
                            len(parts) >= 7 and parts[3:6] == ('common', 'modules', parts[1]),
                            '插件包含非所属安装组件')
            elif parts[0] == 'components':
                require(len(parts) >= 3 and parts[1] in self.manifest['components'], '文件组件归属错误')
                expected_owner = 'component:' + parts[1]
            else:
                require(relative in ('manage.sh', 'README.md'), '套件包含未知根文件')
                expected_owner = 'suite'
            require(item.get('owner') == expected_owner, '文件归属清单不一致：' + relative)
            require(item.get('install') == file_contract(relative, item.get('executable') is True),
                    '文件目标路径或权限不符合约定：' + relative)
            require(not any(p in ('tests', '__tests__', '__pycache__', 'local', 'tmp', 'var', '.cache') for p in parts),
                    '安装包混入测试或运行数据')
            require(checksum(target) == item['sha256'] and target.stat().st_size == item['size'],
                    '套件摘要不匹配：' + relative)
            total += target.stat().st_size
        require(actual == set(files) and total <= MAX_TREE, '套件文件缺失或大小超限')
        self.plugins = self.manifest['plugins']
        require(set(self.plugins) <= set(PLUGINS) and self.plugins, '套件插件列表异常')
        policy = self.manifest.get('corePolicy', 'bundled')
        require(policy in ('bundled', 'preserve-installed'), '内核分发策略无效')
        for plugin in self.plugins:
            for binary in CORES.get(plugin, ()):
                present = (self.payload(plugin) / 'src/files' / binary).is_file()
                require(present == (policy == 'bundled'), '内核与套件分发策略不一致：' + binary)
        required_components = {'manager', 'recovery', 'updater'} | ({'privileged'} if set(self.plugins) & PRIVILEGED else set())
        require(set(self.manifest['components']) == required_components, '共有组件依赖边界错误')
        for plugin, info in self.plugins.items():
            payload = self.payload(plugin)
            metadata = json.loads((payload / 'INFO').read_bytes())
            require(metadata['plugin'] == plugin and metadata['version'] == info['version']
                    and metadata['type'] == 'standard' and metadata['system'] is False, '插件 INFO 不匹配')
            version(metadata['version'])
            for relative in ('src/ui/config', 'src/ui/icon.png', 'src/ui/' + plugin + '.cgi', 'scripts/control'):
                require((payload / relative).is_file(), '插件缺少文件：' + relative)
            descriptor = json.loads((payload / 'installer/plugin.json').read_bytes())
            require(descriptor['plugin'] == plugin and descriptor['privileged'] == (plugin in PRIVILEGED)
                    and descriptor['format'] == FORMAT, '插件安装描述不匹配')
            require(not (payload / 'manage.sh').exists(), '新套件不分发插件独立安装器')
        for name, item in self.manifest['components'].items():
            require(name in COMPONENT_FILES, '未知公共组件')
            version(item['version'])
            for relative in COMPONENT_FILES[name]:
                require((self.component(name) / relative).is_file(), '公共组件文件缺失：' + name + '/' + relative)
            entry = {'manager':'layout.py','privileged':'common/privileged_runtime.py',
                     'recovery':'recovery.py','updater':'updater.py'}[name]
            require(code_version(self.component(name) / entry) == item['version'], '组件 VERSION 与清单不一致：' + name)

    def payload(self, plugin):
        require(plugin in self.plugins, '套件不包含插件：' + plugin)
        return self.root / 'plugins' / plugin

    def component(self, name):
        return self.root / 'components' / name

    def materialize(self, plugin, destination):
        """A protected transaction view for existing narrow component APIs."""
        target = Path(destination) / plugin
        shutil.copytree(self.payload(plugin), target)
        # Windows/DrvFS and ZIP extractors cannot reliably retain POSIX modes.
        # The signed executable flag, not the source filesystem, is authoritative.
        for path in target.rglob('*'):
            if path.is_file():
                item = self.manifest['files']['plugins/' + plugin + '/' + path.relative_to(target).as_posix()]
                path.chmod(0o755 if item['executable'] else 0o644)
        for name in ('recovery', 'updater'):
            shutil.copytree(self.component(name), target / 'installer' / name)
        if plugin in PRIVILEGED:
            common = target / 'installer/common'
            common.mkdir(parents=True, exist_ok=True)
            for relative in COMPONENT_FILES['privileged']:
                out = common / relative
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.component('privileged') / relative, out)
            files = {p.relative_to(common).as_posix(): checksum(p) for p in common.rglob('*')
                     if p.is_file() and p.name != 'manifest.json'}
            (common / 'manifest.json').write_text(json.dumps(dict(apiVersion=1, layoutVersion=3,
                version=self.manifest['components']['privileged']['version'], plugin=plugin, files=files)))
        return target
