#!/usr/bin/env python3
"""Unified interactive/automation frontend; remote workers run the same engine."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import layout as L
import manager
from packages import Bundle


def parser():
    result = argparse.ArgumentParser(description='小米 NAS 统一插件安装器')
    result.add_argument('action', nargs='?', choices=('install', 'reinstall', 'update', 'uninstall', 'repair', 'status', 'recover'))
    result.add_argument('--plugins', help='逗号分隔的插件 ID')
    result.add_argument('--users', help='逗号分隔的 NAS 账号')
    result.add_argument('--all-plugins', action='store_true')
    result.add_argument('--all-users', action='store_true')
    result.add_argument('--ip', help='NAS IPv4 地址或 SSH 主机名')
    result.add_argument('--pool', help='新安装存在多个存储池时明确选择')
    result.add_argument('--bundle', type=Path, help='已解压的签名离线套件目录')
    result.add_argument('--dry-run', action='store_true')
    result.add_argument('--yes', '-y', action='store_true')
    result.add_argument('--json', action='store_true')
    result.add_argument('--backend', action='store_true', help=argparse.SUPPRESS)
    return result


def choose(label, values, explicit, all_values):
    L.require(not (explicit and all_values), label + '列表和全选不能同时指定')
    if all_values:
        return list(values)
    if explicit:
        selected = explicit.split(',')
        L.require(selected and len(set(selected)) == len(selected) and set(selected) <= set(values),
                  label + '选择无效，可选：' + ','.join(values))
        return selected
    if len(values) == 1:
        return list(values)
    L.require(sys.stdin.isatty(), '非交互操作请明确指定' + label)
    print(label + '：')
    for index, value in enumerate(values, 1):
        print('  %d) %s' % (index, value))
    raw = input('序号多选（逗号分隔），或 all：').strip()
    if raw == 'all':
        return list(values)
    tokens = raw.split(',')
    L.require(all(t.isdigit() and 1 <= int(t) <= len(values) for t in tokens), '无效序号')
    return list(dict.fromkeys(values[int(t) - 1] for t in tokens))


def selected(args, rows):
    installed = {(r['user'], r['plugin']) for r in rows if r['installed']}
    if args.action == 'reinstall' and not args.users and not args.plugins and not args.all_users and not args.all_plugins:
        return sorted(installed)
    users = choose('账号', sorted({r['user'] for r in rows}), args.users, args.all_users)
    plugins = choose('插件', list(L.PLUGINS), args.plugins, args.all_plugins)
    pairs = [(u, p) for u in users for p in plugins]
    if args.action in ('uninstall', 'update', 'reinstall', 'repair'):
        pairs = [pair for pair in pairs if pair in installed]
    return pairs


def backend(args):
    L.require(os.geteuid() == 0 and os.environ.get('SUDO_USER') in (None, 'root'), '需要 NAS root 终端')
    if args.action == 'recover':
        manager.recover_pending()
        return {'ok': True}
    rows = manager.inventory()
    if args.action == 'status':
        if args.users:
            users = choose('账号', sorted({r['user'] for r in rows}), args.users, args.all_users)
            rows = [row for row in rows if row['user'] in users]
        if args.plugins:
            plugins = choose('插件', list(L.PLUGINS), args.plugins, args.all_plugins)
            rows = [row for row in rows if row['plugin'] in plugins]
        pending = [{'id': p.parent.name, 'phase': L.read(p).get('phase')} for p in (L.BASE / 'transactions').glob('*/journal.json')]
        return {'ok': True, 'plugins': rows, 'transactions': pending}
    pairs = selected(args, rows)
    if args.action == 'repair':
        L.require(L.read(L.BASE / 'installed.json', {}).get('format') == 2,
                  '旧版恢复登记须先通过 reinstall 整机切换，不能直接接管')
        if args.dry_run:
            for user, plugin in pairs:
                manager.target(user, plugin)
            return {'ok': True, 'targets': pairs, 'dryRun': True}
        with L.lock(L.INSTALL_LOCK):
            manager.recover_pending(already_locked=True)
            r = manager.recovery()
            for user, plugin in pairs:
                directory = L.RECOVERY / 'records' / (user + '-' + plugin)
                if not args.dry_run:
                    with r.record_lock(directory):
                        manager.repair_record(directory)
        return {'ok': True, 'targets': pairs, 'dryRun': args.dry_run}
    bundle = Bundle(args.bundle) if args.action != 'uninstall' else None
    plan = manager.apply(args.action, pairs, bundle, dry_run=True, pool=args.pool)
    if args.dry_run:
        return plan
    if args.action in ('uninstall', 'reinstall') and not args.yes:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        L.require(sys.stdin.isatty(), '非交互卸载或重装需要 --yes')
        note = '彻底删除插件配置、任务和恢复材料' if args.action == 'uninstall' else '保留数据和启停状态，停服后重新安装'
        L.require(input(note + '。输入 YES 继续：') == 'YES', '操作已取消')
    return manager.apply(args.action, pairs, bundle, pool=args.pool)


def remote(args):
    L.require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,252}', args.ip or ''), 'NAS 地址无效')
    for command in ('ssh', 'tar'):
        L.require(shutil.which(command), '本机缺少命令：' + command)
    options = ['-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=accept-new']
    if not sys.stdin.isatty():
        options += ['-o', 'BatchMode=yes']
    host = 'root@' + args.ip
    if args.bundle:
        bundle = Bundle(args.bundle)
        root = bundle.root
    else:
        L.require(args.action in ('status', 'uninstall', 'repair'), '请用 --bundle 指定完整离线套件目录')
        root = None
    # The local bundle is verified before any remote code executes. All paths
    # in the tar stream are created here, not accepted from a web/API caller.
    remote_dir = subprocess.check_output(['ssh', *options, host,
                                         'umask 077; mktemp -d /tmp/minas-suite.XXXXXXXX'], text=True).strip()
    L.require(re.fullmatch(r'/tmp/minas-suite\.[A-Za-z0-9]+', remote_dir), '远程暂存路径无效')
    try:
        with tempfile.TemporaryFile() as temporary:
            with tarfile.open(fileobj=temporary, mode='w:gz') as archive:
                if root:
                    for path in sorted(root.rglob('*')):
                        if path.is_file():
                            archive.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
                else:
                    for path in Path(__file__).parent.iterdir():
                        if path.suffix == '.py' or path.name == 'release-public-key.txt':
                            archive.add(path, arcname='components/manager/' + path.name, recursive=False)
            temporary.seek(0)
            subprocess.run(['ssh', *options, host, 'tar -xzf - -C ' + shlex.quote(remote_dir)],
                           stdin=temporary, check=True)
        command = ['/usr/bin/python3', '-B', remote_dir + '/components/manager/cli.py', args.action, '--backend']
        if root:
            command += ['--bundle', remote_dir]
        for name in ('users', 'plugins', 'pool'):
            if getattr(args, name):
                command += ['--' + name, getattr(args, name)]
        for name in ('all_users', 'all_plugins', 'dry_run', 'yes', 'json'):
            if getattr(args, name):
                command.append('--' + name.replace('_', '-'))
        subprocess.run(['ssh', *options, *(['-t'] if sys.stdin.isatty() else []), host, shlex.join(command)], check=True)
    finally:
        subprocess.run(['ssh', *options, '-o', 'BatchMode=yes', host,
                        'rm -rf -- ' + shlex.quote(remote_dir)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    os.umask(0o077)
    args = parser().parse_args()
    if not args.action:
        L.require(sys.stdin.isatty(), '请指定操作')
        actions = ['install', 'reinstall', 'update', 'uninstall', 'repair', 'status']
        labels = ['安装插件', '旧版切换／保留数据重装', '从离线套件更新', '彻底卸载插件及数据', '修复插件', '查看状态']
        print('小米 NAS 统一插件管理')
        for n, label in enumerate(labels, 1):
            print('%d) %s' % (n, label))
        answer = input('选择操作：').strip()
        L.require(answer.isdigit() and 1 <= int(answer) <= len(actions), '无效操作')
        args.action = actions[int(answer) - 1]
    if args.bundle is None:
        candidate = Path(__file__).resolve().parents[2]
        if (candidate / 'suite.json').is_file():
            args.bundle = candidate
    if not args.backend and not Path('/etc/config/plugin').is_file():
        if not args.ip:
            L.require(sys.stdin.isatty(), '远程操作请提供 --ip')
            args.ip = input('NAS 地址：').strip()
        remote(args)
        return
    L.require(not args.ip, 'NAS 本机操作不接受 --ip')
    if args.action in ('install', 'reinstall', 'update'):
        L.require(args.bundle, '请提供已解压的完整离线套件目录 --bundle')
    result = backend(args)
    print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('操作失败：' + str(error), file=sys.stderr)
        raise SystemExit(1)
