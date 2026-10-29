"""Build signed, per-plugin update metadata and optionally publish a GitHub Release."""
import argparse
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
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'daixu0502/xiaomi-nas-plugin'


def openssl(args):
    executable = shutil.which('openssl')
    if executable:
        command = [executable, *map(str, args)]
    elif os.name == 'nt':
        def linux_path(value):
            value = str(value)
            return '/mnt/' + value[0].lower() + value[2:].replace('\\', '/') if re.match(r'^[A-Za-z]:', value) else value
        command = ['wsl.exe', '-e', 'openssl', *map(linux_path, args)]
    else:
        raise ValueError('需要 OpenSSL 来签名发布清单')
    subprocess.run(command, check=True, capture_output=True, timeout=30)


def prepare(tag, key, target):
    if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{0,95}', tag):
        raise ValueError('发布标签无效')
    if not key.is_file():
        raise ValueError('签名私钥不存在；私钥必须保存在源码和安装包之外')
    try:
        key.relative_to(ROOT / 'local')
    except ValueError:
        if key.is_relative_to(ROOT):
            raise ValueError('签名私钥不能位于可提交的源码目录')
    subprocess.run([sys.executable, '-B', str(ROOT / 'workspace-tools/build_plugin_packages.py')], check=True, capture_output=True)
    catalog = json.loads((ROOT / 'package-manifest.json').read_text())
    match = re.search(r"^VERSION = '([0-9]+\.[0-9]+\.[0-9]+)'$", (ROOT / 'workspace-tools/plugin-updater/updater.py').read_text(encoding='utf-8'), re.M)
    if not match:
        raise ValueError('在线更新组件版本缺失')
    # This is the schema-1 protocol floor, not the version being distributed:
    # compatible future updater versions must themselves remain upgradable.
    value = dict(schema=1, repository=REPOSITORY, tag=tag, minUpdater='1.0.0', updaterVersion=match[1], sourceCommit=target, plugins={})
    assets = []
    for plugin, item in catalog.items():
        archive = ROOT / item['file']
        info = json.loads((ROOT / plugin / 'INFO').read_text())
        value['plugins'][plugin] = dict(version=info['version'], asset=archive.name, size=archive.stat().st_size, sha256=item['sha256'], architecture='aarch64', notes=info['changelog'])
        if hashlib.sha256(archive.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError('安装包内容与构建清单不一致')
        assets.append(archive)
    manifest, signature = ROOT / 'dist/plugin-updates.json', ROOT / 'dist/plugin-updates.sig'
    manifest.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
    openssl(['dgst', '-sha256', '-sign', key, '-out', signature, manifest])
    openssl(['dgst', '-sha256', '-verify', ROOT / 'workspace-tools/plugin-updater/release-public-key.txt', '-signature', signature, manifest])
    return assets + [manifest, signature, ROOT / 'dist/SHA256SUMS']


def credential():
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        result = subprocess.run(['git', 'credential', 'fill'], input='protocol=https\nhost=github.com\n\n', text=True, capture_output=True, env=dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never'), timeout=20)
        fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        token = fields.get('password')
    if not token:
        raise ValueError('缺少 GitHub 发布授权；可使用现有 Git 凭据或 GH_TOKEN，不要把令牌写进脚本')
    return token


def publish(tag, target, assets):
    if not re.fullmatch('[0-9a-f]{40}', target):
        raise ValueError('发布必须指定完整源码提交 SHA')
    listing = subprocess.run(['git', '-c', 'safe.directory=' + ROOT.as_posix(), '-C', str(ROOT), 'ls-tree', '-rz', '--full-tree', target], check=True, capture_output=True).stdout
    committed = {entry.split(b'\t', 1)[1].decode(): entry.split(b'\t', 1)[0].split()[2].decode() for entry in listing.split(b'\0') if entry}
    for path in assets:
        if path.name.endswith('.tar.gz'):
            with tarfile.open(path) as package:
                for item in package:
                    data = package.extractfile(item).read()
                    oid = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
                    if committed.get(item.name) != oid:
                        raise ValueError('安装包与指定源码提交不一致：' + item.name)
    token = credential()
    def request(url, method='GET', body=None, mime='application/json'):
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        req = urllib.request.Request(url, data=data, method=method, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'Content-Type': mime, 'User-Agent': 'Xiaomi-NAS-Release-Publisher'})
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise ValueError('GitHub 发布请求失败：HTTP ' + str(error.code)) from None
    base = 'https://api.github.com/repos/' + REPOSITORY
    releases = request(base + '/releases?per_page=100')
    existing = next((r for r in releases if r['tag_name'] == tag), None)
    if existing and not existing['draft']:
        raise ValueError('该标签已经正式发布，拒绝替换公开安装包；请使用新标签')
    release = existing or request(base + '/releases', 'POST', dict(tag_name=tag, target_commitish=target, name='小米 NAS 插件 ' + tag, draft=True, prerelease=False, body='八个插件独立版本的正式安装包，附签名更新清单与 SHA-256 校验。更新会保留已有配置、任务及 SSH 恢复材料。'))
    remote_assets = request(release['assets_url'])
    if remote_assets:
        raise ValueError('草稿已有附件，请检查后换用新标签，发布工具不自动删除附件')
    upload = release['upload_url'].split('{', 1)[0]
    for path in assets:
        data = request(upload + '?name=' + urllib.parse.quote(path.name), 'POST', path.read_bytes(), 'application/octet-stream')
        if data['size'] != path.stat().st_size:
            raise ValueError('GitHub 附件大小不匹配')
        digest = data.get('digest')
        if digest and digest != 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError('GitHub 附件摘要不匹配')
    data = request(release['url'], 'PATCH', dict(draft=False, make_latest='true'))
    print(json.dumps(dict(published=True, url=data['html_url'], tag=data['tag_name'], assets=len(assets)), ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--target', required=True, help='exact source commit')
    parser.add_argument('--signing-key', type=Path, required=True)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    paths = prepare(args.tag, args.signing_key.resolve(), args.target)
    if args.publish:
        publish(args.tag, args.target, paths)
    else:
        print(json.dumps(dict(prepared=True, tag=args.tag, assets=[p.name for p in paths]), ensure_ascii=False))
