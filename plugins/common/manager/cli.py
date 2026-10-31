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
from packages import Bundle, unpack

LABELS = dict(dockermanager='Docker 管理', mihomo='Mihomo', taskcenter='定时任务', nascenter='控制中心',
              sshrecovery='SSH 恢复', quickshare='文件快传', downloadcenter='下载中心', mediaeditor='影视编辑')


def show_plan(plan):
    if plan.get('requiresRecovery'):
        print('有未完成事务，将先恢复：' + ', '.join(plan['transactions']))
    print('操作：' + plan.get('action', 'repair') + '；' +
          ('彻底删除插件数据' if plan.get('dataPolicy') == 'delete' or plan.get('action') == 'uninstall' else '保留配置和启停状态'))
    for row in plan.get('targets', []):
        if isinstance(row, (list, tuple)):
            user, plugin = row
        else:
            user, plugin = row['user'], row['plugin']
        print('  ' + user + ' / ' + LABELS.get(plugin, plugin) + ' (' + plugin + ')')
        if isinstance(row, dict):
            for note in row.get('notes', []):
                print('    ' + note)
            if row.get('incomplete'):
                print('    文件不完整：停止所属服务后清理，不依赖损坏的插件脚本。')


def show_result(args, result):
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif args.action == 'status':
        for row in result['plugins']:
            if not row['installed']:
                state = '未安装'
            else:
                state = str(row['version']) + '；入口' + ('启用' if row['enabled'] else '停用')
                if row['plugin'] in L.SERVERS or row['plugin'] == 'taskcenter':
                    state += '；服务' + ('运行' if row['running'] else '停止')
            print(row['user'] + ' / ' + LABELS[row['plugin']] + '：' + state)
        if result['transactions']:
            print('存在未完成事务，请运行 sh manage.sh recover：' + ', '.join(t['id'] for t in result['transactions']))
    elif args.action == 'recover':
        pending = result.get('transactions', [])
        print('待恢复事务：' + str(len(pending)) if args.dry_run else
              '尚有存储池待挂载清理：' + ', '.join(sorted({p for t in pending for p in t.get('pendingPools', [])})) if pending else '事务恢复完成。')
    else:
        show_plan(result)
        if args.dry_run:
            print('预检未修改设备配置。' if result.get('requiresRecovery') else '预检通过，未修改设备配置。')
        else:
            print('已卸载可访问部分；待存储池挂载后继续清理：' + ', '.join(result['pendingPools'])
                  if result.get('pendingPools') else '操作成功。')


def parser():
    result = argparse.ArgumentParser(description='小米 NAS 统一插件安装器')
    result.add_argument('action', nargs='?', choices=('install', 'reinstall', 'update', 'uninstall', 'repair', 'status', 'recover'))
    result.add_argument('--plugins', help='逗号分隔的插件 ID')
    result.add_argument('--users', help='逗号分隔的 NAS 账号')
    result.add_argument('--all-plugins', action='store_true')
    result.add_argument('--all-users', action='store_true')
    result.add_argument('--ip', help='NAS IPv4 地址或 SSH 主机名')
    result.add_argument('--pool', help='新安装存在多个存储池时明确选择')
    result.add_argument('--bundle', type=Path, help='签名离线套件 .tar.gz 或已解压目录；源码入口默认读取 dist 最新套件')
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
        print('  %d) %s' % (index, (LABELS[value] + ' (' + value + ')') if value in LABELS else value))
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
    labels = dict(backup='保存回退材料', stop='暂停服务', component='安装公共组件',
                  remove='移除原安装', layout='准备目录', source='替换源码', grant='配置账号授权',
                  registration='更新原厂注册', verification='原厂校验', snapshot='保存恢复快照',
                  start='启动服务', health='业务检查', rollback='恢复原安装', cleanup='清理事务')
    manager.PROGRESS = lambda step, row: print('[' + labels.get(step, step) + ']' +
        (' ' + row['user'] + '/' + row['plugin'] if row else ''), file=sys.stderr, flush=True)
    if args.action == 'recover':
        if args.dry_run:
            pending = [{'id': p.parent.name, 'phase': L.read(p).get('phase')}
                       for p in (L.BASE / 'transactions').glob('*/journal.json')]
            return {'ok': True, 'dryRun': True, 'transactions': pending}
        manager.recover_pending()
        pending = [{'id': p.parent.name, 'phase': L.read(p).get('phase'), 'pendingPools': L.read(p).get('pendingPools', [])}
                   for p in (L.BASE / 'transactions').glob('*/journal.json')]
        return {'ok': True, 'transactions': pending}
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
    L.require(pairs, '所选账号没有可执行此操作的插件')
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
    print('正在预检账号、源码、依赖和可用空间…', file=sys.stderr, flush=True)
    plan = manager.apply(args.action, pairs, bundle, dry_run=True, pool=args.pool)
    if args.dry_run:
        return plan
    if args.action in ('uninstall', 'reinstall') and not args.yes:
        show_plan(plan)
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
    if args.action in ('install', 'reinstall', 'update'):
        L.require(args.bundle, '请用 --bundle 指定完整离线套件目录')
        print('正在校验离线套件…', file=sys.stderr, flush=True)
        bundle = Bundle(args.bundle)
        root = bundle.root
    else:
        root = None
    # The local bundle is verified before any remote code executes. All paths
    # in the tar stream are created here, not accepted from a web/API caller.
    remote_dir = subprocess.check_output(['ssh', *options, host,
                                         'umask 077; mktemp -d /tmp/minas-suite.XXXXXXXX'], text=True).strip()
    L.require(re.fullmatch(r'/tmp/minas-suite\.[A-Za-z0-9]+', remote_dir), '远程暂存路径无效')
    try:
        with tempfile.TemporaryFile() as temporary:
            print('正在准备并传输' + ('离线套件' if root else '管理程序') + '…', file=sys.stderr, flush=True)
            with tarfile.open(fileobj=temporary, mode='w:gz', compresslevel=1) as archive:
                if root:
                    for path in sorted(root.rglob('*')):
                        if path.is_file():
                            relative = path.relative_to(root).as_posix()
                            member = archive.gettarinfo(str(path), arcname=relative)
                            item = bundle.manifest['files'].get(relative, {})
                            member.mode = 0o755 if item.get('executable') else 0o644
                            member.uid = member.gid = 0
                            member.uname = member.gname = ''
                            with path.open('rb') as stream:
                                archive.addfile(member, stream)
                else:
                    for path in Path(__file__).parent.iterdir():
                        if path.suffix == '.py' or path.name == 'release-public-key.txt':
                            archive.add(path, arcname='components/manager/' + path.name, recursive=False)
            temporary.seek(0)
            subprocess.run(['ssh', *options, host, 'tar -xzf - -C ' + shlex.quote(remote_dir)],
                           stdin=temporary, check=True)
        print('已连接 NAS，开始执行 ' + args.action + '…', file=sys.stderr, flush=True)
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


def resolve_bundle(args, temporary):
    if args.action not in ('install', 'reinstall', 'update'):
        return
    if args.bundle is None:
        candidate = Path(__file__).resolve().parents[2]
        if (candidate / 'suite.json').is_file():
            args.bundle = candidate
        else:
            dist = Path(__file__).resolve().parents[3] / 'dist'
            catalog = dist / 'suite-artifacts.json'
            if catalog.is_file():
                item = L.read(catalog)['suite']
                name = item['asset']
                L.require(re.fullmatch(r'xiaomi-nas-suite-[A-Za-z0-9._-]+-arm64\.tar\.gz', name), '离线套件文件名无效')
                archive = L.plain(dist / name)
                L.require(archive.is_file() and archive.stat().st_size == item['size']
                          and L.checksum(archive) == item['sha256'], 'dist 离线套件缺失或摘要不匹配，请重新构建或下载完整套件')
                args.bundle = archive
                print('使用本地套件：' + name, file=sys.stderr, flush=True)
    L.require(args.bundle, '未找到完整离线套件。请下载后解压并运行其中的 manage.sh，或使用 --bundle 指定 .tar.gz/目录')
    if args.bundle.is_file():
        print('正在解包并校验离线套件…', file=sys.stderr, flush=True)
        args.bundle = unpack(args.bundle, Path(temporary) / 'bundle')
    L.require(args.bundle.is_dir(), '离线套件目录不存在：' + str(args.bundle))


def execute(args):
    if not args.backend and not Path('/etc/config/plugin').is_file():
        if not args.ip:
            L.require(sys.stdin.isatty(), '远程操作请提供 --ip')
            args.ip = input('NAS 地址：').strip()
        remote(args)
        return
    L.require(not args.ip, 'NAS 本机操作不接受 --ip')
    result = backend(args)
    show_result(args, result)


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
    if args.action in ('install', 'reinstall', 'update'):
        with tempfile.TemporaryDirectory(prefix='minas-bundle-') as temporary:
            resolve_bundle(args, temporary)
            execute(args)
    else:
        execute(args)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('操作失败：' + manager.failure_summary(error), file=sys.stderr)
        raise SystemExit(1)
