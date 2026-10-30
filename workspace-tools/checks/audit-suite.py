"""Verify signed suites, dependency boundaries and offline/update equivalence."""
import json
from pathlib import Path
import shutil
import tempfile

import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'plugins/common/manager'))

from layout import PLUGINS, PRIVILEGED, checksum, require
from packages import Bundle, unpack


def audit(root):
    dist = root / 'dist'
    artifacts = json.loads((dist / 'suite-artifacts.json').read_text())
    catalog = json.loads((root / 'package-manifest.json').read_text())
    reports = {}
    with tempfile.TemporaryDirectory(prefix='minas-suite-audit-') as temporary:
        folder = Path(temporary)
        item = artifacts['suite']
        archive = dist / item['asset']
        require(checksum(archive) == item['sha256'] and archive.stat().st_size == item['size'], '完整套件摘要错误')
        suite = Bundle(unpack(archive, folder / 'suite'))
        require(set(suite.plugins) == set(PLUGINS), '完整套件未包含八插件')
        for name, component in artifacts['components'].items():
            file = dist / component['asset']
            require(checksum(file) == component['sha256'], '组件包摘要错误：' + name)
        for plugin, item in catalog.items():
            file = root / item['file']
            require(checksum(file) == item['sha256'], '插件包摘要错误：' + plugin)
            payload = unpack(file, folder / plugin)
            manifest = json.loads((payload / 'suite.json').read_text())
            require(set(manifest['plugins']) == {plugin}, '更新包包含其他插件')
            require(not (payload / 'components').exists(), '更新包重复包含共有组件')
            for name in manifest['components']:
                shutil.copytree(suite.component(name), payload / 'components' / name)
            update = Bundle(payload)
            for relative, info in update.manifest['files'].items():
                require(info == suite.manifest['files'][relative], '在线更新与离线套件内容不一致：' + relative)
            require(('privileged' in manifest['components']) == (plugin in PRIVILEGED), '非提权插件依赖边界错误')
            materialized = update.materialize(plugin, folder / 'installed-view')
            common = materialized / 'installer/common'
            if plugin in PRIVILEGED:
                modules = {p.name for p in (common / 'modules').iterdir()}
                require(modules == {plugin}, '安装视图混入其他私有模块')
                data = json.loads((common / 'manifest.json').read_text())
                require(all(checksum(common / key) == value for key, value in data['files'].items()), '权限组件安装视图摘要错误')
            else:
                require(not common.exists(), '非提权插件包含权限组件')
            reports[plugin] = dict(version=item['version'], dependencies=list(manifest['components']),
                                   signature=True, fileOwnership=True, offlineUpdateEquivalent=True)
    return dict(suite=artifacts['suite'], plugins=reports)


if __name__ == '__main__':
    print(json.dumps(audit(ROOT), ensure_ascii=False, indent=2))
