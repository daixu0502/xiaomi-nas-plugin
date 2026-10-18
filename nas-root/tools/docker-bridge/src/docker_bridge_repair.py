#!/usr/bin/env python3
"""Xiaomi NAS native Docker IPv4 bridge repair; no third-party dependencies."""
import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time

BASE = Path('/data/docker-bridge-repair')
CONFIG = Path('/etc/config/mi_docker')
RUNTIME = Path('/tmp/dockerd/daemon.json')
FORWARD = Path('/proc/sys/net/ipv4/ip_forward')
TAG = 'minas-bridge-net'
OPTION = 'mi_docker.globals.iptables'
ENV = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/data/docker',
       'LC_ALL': 'C', 'HOME': '/root'}
ID = re.compile(r'[0-9]{8}-[0-9]{6}-[a-z0-9_]{8}')


def run(args, check=True, timeout=45):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True,
                            timeout=timeout, env=ENV, cwd='/')
    if check and result.returncode:
        raise RuntimeError('%s 失败：%s' % (args[0], result.stderr.strip()[:500]))
    return result


def docker(*args, **kwargs):
    executable = '/data/docker/docker' if Path('/data/docker/docker').is_file() else '/usr/bin/docker'
    # Do not inherit DOCKER_HOST, contexts, TLS options or user CLI credentials.
    return run([executable, '--host=unix:///var/run/docker.sock', *args], **kwargs)


def ipt(table, *args, **kwargs):
    return run(['/usr/sbin/iptables', '-w', '5', '-t', table, *args], **kwargs)


def rules(table):
    return [shlex.split(line) for line in ipt(table, '-S').stdout.splitlines()]


def owned(rule):
    if not rule or rule[0] != '-A' or '--comment' not in rule:
        return False
    index = rule.index('--comment') + 1
    return index < len(rule) and rule[index] == TAG


def running():
    values = docker('ps', '-q', '--no-trunc').stdout.split()
    if not all(re.fullmatch(r'[a-f0-9]{64}', value) for value in values):
        raise RuntimeError('Docker 返回了异常的容器 ID')
    return values


def inspect_networks():
    ids = docker('network', 'ls', '--filter', 'driver=bridge', '-q', '--no-trunc').stdout.split()
    if not ids:
        return []
    if not all(re.fullmatch(r'[a-f0-9]{64}', value) for value in ids):
        raise RuntimeError('Docker 返回了异常的网络 ID')
    return json.loads(docker('network', 'inspect', *ids).stdout)


def requirements(network):
    """Rules expected from the NAS Docker 20.10 native iptables backend."""
    if network.get('Driver') != 'bridge' or network.get('Internal'):
        return []
    options = network.get('Options') or {}
    if str(options.get('com.docker.network.bridge.enable_ip_masquerade', 'true')).lower() == 'false':
        return []  # Explicitly routed/no-NAT networks must not be changed.
    ident = network['Id']
    if not re.fullmatch(r'[a-f0-9]{64}', ident):
        raise RuntimeError('异常的网络 ID')
    bridge = options.get('com.docker.network.bridge.name') or (
        'docker0' if network['Name'] == 'bridge' else 'br-' + ident[:12])
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', bridge) or bridge == 'lo':
        raise RuntimeError('异常的桥接网卡名')
    result = []
    for item in network.get('IPAM', {}).get('Config') or []:
        subnet = ipaddress.ip_network(item.get('Subnet', ''))
        if subnet.version == 4:
            result.append(['POSTROUTING', '-s', str(subnet), '!', '-o', bridge, '-j', 'MASQUERADE'])
    return result


def audit():
    value = run(['uci', '-q', 'get', OPTION], check=False)
    if value.returncode not in (0, 1):
        raise RuntimeError('无法读取 UCI 配置')
    configured = value.stdout.strip() if value.returncode == 0 else None
    try:
        native = json.loads(RUNTIME.read_text()).get('iptables')
    except (OSError, ValueError):
        native = None
    networks = inspect_networks()
    missing = []
    missing_forward = []
    checked = []
    for network in networks:
        expected = requirements(network)
        if expected:
            checked.append(network['Name'])
            bridge = expected[0][5]
            outbound = ipt('filter', '-C', 'FORWARD', '-i', bridge, '!', '-o', bridge, '-j', 'ACCEPT', check=False).returncode == 0
            established = ipt('filter', '-C', 'FORWARD', '-o', bridge, '-m', 'conntrack', '--ctstate', 'RELATED,ESTABLISHED', '-j', 'ACCEPT', check=False).returncode == 0
            # Newer Docker releases can move per-bridge rules into their own chains.
            if not outbound:
                outbound = ipt('filter', '-C', 'DOCKER-FORWARD', '-i', bridge, '-j', 'ACCEPT', check=False).returncode == 0
            if not established:
                established = ipt('filter', '-C', 'DOCKER-CT', '-o', bridge, '-m', 'conntrack', '--ctstate', 'RELATED,ESTABLISHED', '-j', 'ACCEPT', check=False).returncode == 0
            if not (outbound and established):
                missing_forward.append(network['Name'])
        for rule in expected:
            if ipt('nat', '-C', *rule, check=False).returncode:
                missing.append(network['Name'])
    tables = {table: rules(table) for table in ('nat', 'filter')}
    hooks = any(rule[:2] == ['-A', 'FORWARD'] and '-j' in rule
                and rule[rule.index('-j') + 1] in ('DOCKER', 'DOCKER-FORWARD') for rule in tables['filter'])
    forwarded = FORWARD.read_text().strip() == '1'
    active = run(['systemctl', 'is-active', 'bridge-net.service'], check=False).returncode == 0
    enable_mode = run(['systemctl', 'is-enabled', 'bridge-net.service'], check=False).stdout.strip()
    enabled = enable_mode in ('enabled', 'enabled-runtime')
    legacy = sum(owned(rule) for values in tables.values() for rule in values)
    healthy = configured in (None, '1', 'true', 'yes', 'on') and native is True and forwarded and hooks and not missing and not missing_forward
    return {'uciIptables': configured, 'runtimeIptables': native, 'ipForward': forwarded,
            'dockerForwardHook': hooks, 'bridgeNetworks': checked, 'missingNat': sorted(set(missing)),
            'missingForward': sorted(set(missing_forward)),
            'legacyTaggedRules': legacy, 'legacyServiceActive': active, 'legacyServiceEnabled': enabled,
            'legacyServiceEnableMode': enable_mode,
            'healthy': healthy, 'needsChange': not healthy or bool(legacy) or active or enabled}


def secure_dir(path):
    """Reject user-writable or symlinked state directories before running as root."""
    for parent in reversed((path, *path.parents)):
        if not parent.exists() and not parent.is_symlink():
            parent.mkdir(mode=0o700)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('状态目录不安全：' + str(parent))


def atomic(path, data):
    if isinstance(data, str):
        data = data.encode()
    fd, temporary = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save(folder, state):
    atomic(folder / 'state.json', json.dumps(state, ensure_ascii=False, indent=2) + '\n')


def transaction(ident):
    if not ID.fullmatch(ident):
        raise RuntimeError('无效的事务编号')
    folder = BASE / 'transactions' / ident
    secure_dir(folder.parent)
    if not folder.is_dir() or folder.is_symlink():
        raise RuntimeError('事务不存在')
    secure_dir(folder)
    state = json.loads((folder / 'state.json').read_text())
    if state.get('id') != ident:
        raise RuntimeError('事务编号不匹配')
    return folder, state


def unit(state):
    return 'minas-docker-bridge-rollback-' + state['id']


def disarm(state):
    run(['systemctl', 'stop', unit(state) + '.timer'], check=False)


def restart_and_restore(ids):
    run(['systemctl', 'restart', 'docker.service'], timeout=120)
    for attempt in range(15):
        if docker('info', check=False).returncode == 0:
            break
        time.sleep(2)
    else:
        raise RuntimeError('Docker 未能在有限时间内就绪')
    if ids:
        docker('start', *ids, timeout=120)


def rollback(folder, state):
    if state['phase'] != 'pending':
        print('事务状态为 %s，不执行回退。' % state['phase'])
        return
    if not all(os.path.ismount(path) for path in state['mounts']):
        raise RuntimeError('原存储池未挂载，停止回退以免容器写入错误磁盘；挂载后再手动回退')
    # Keep the watchdog available if a manual rollback fails partway through.
    current = docker('ps', '-q', '--no-trunc', check=False).stdout.split()
    ids = sorted(set(state['running'] + [x for x in current if re.fullmatch(r'[a-f0-9]{64}', x)]))
    if state['uci'] is None:
        run(['uci', '-q', 'delete', OPTION], check=False)
    else:
        run(['uci', 'set', OPTION + '=' + state['uci']])
    run(['uci', 'commit', 'mi_docker'])
    if state['helperEnabled']:
        flags = ['--runtime'] if state.get('helperEnableMode') == 'enabled-runtime' else []
        run(['systemctl', 'enable', *flags, 'bridge-net.service'])
    restart_and_restore(ids)
    # Restore only the two original table policies and exactly tagged helper rules.
    for table, values in state['rules'].items():
        for rule in values:
            if rule and rule[0] == '-P':
                ipt(table, *rule)
            elif owned(rule) and ipt(table, '-C', *rule[1:], check=False).returncode:
                ipt(table, '-A', *rule[1:])
    if state['helperActive']:
        run(['systemctl', 'start', 'bridge-net.service'])
    FORWARD.write_text(state['ipForward'] + '\n')
    state['phase'] = 'rolled-back'
    save(folder, state)
    disarm(state)
    print('已回退原 UCI、转发开关、策略及辅助规则，并恢复原运行容器。')


def apply(yes=False):
    report = audit()
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if not report['needsChange']:
        print('当前原生 bridge 网络正常，无需修复；未重启 Docker、未写入文件。')
        return
    if report['uciIptables'] not in (None, '0', '1', 'true', 'false', 'yes', 'no', 'on', 'off'):
        raise RuntimeError('不识别的 UCI iptables 值，停止修改')
    if not CONFIG.is_file() or 'mi_docker' not in Path('/usr/sbin/dockerd_prepare.sh').read_text():
        raise RuntimeError('不是受支持的小米 NAS Docker 配置生成方式')
    if run(['uci', 'changes', 'mi_docker']).stdout.strip():
        raise RuntimeError('mi_docker 有尚未提交的 UCI 改动，请先处理')
    for item in (BASE / 'transactions').glob('*/state.json'):
        if json.loads(item.read_text())['phase'] == 'pending':
            raise RuntimeError('存在待确认事务：' + item.parent.name)
    if not yes:
        if not sys.stdin.isatty() or input('将短暂停止容器并重启 Docker。输入 APPLY 继续：') != 'APPLY':
            raise RuntimeError('未确认；非交互执行需要显式 --yes')
    ids = running()
    # Docker's storage root may be a symlink into the pool. Never start containers
    # while their original pool mount has disappeared.
    roots = [Path('/nas/pool0')]
    for ident in ids:
        detail = json.loads(docker('inspect', ident).stdout)[0]
        for mount in detail.get('Mounts', []):
            source = mount.get('Source', '')
            match = re.match(r'^(/nas/pool[0-9]+)(?:/|$)', source)
            if match:
                roots.append(Path(match.group(1)))
    roots = sorted(set(roots))
    if not all(os.path.ismount(path) for path in roots):
        raise RuntimeError('真实存储池尚未挂载，停止修改')
    folder = Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%d-%H%M%S-'), dir=BASE / 'transactions'))
    state = {'id': folder.name, 'phase': 'pending', 'uci': report['uciIptables'], 'running': ids,
             'helperEnabled': report['legacyServiceEnabled'], 'helperActive': report['legacyServiceActive'],
             'helperEnableMode': report['legacyServiceEnableMode'],
             'ipForward': FORWARD.read_text().strip(), 'rules': {t: rules(t) for t in ('nat', 'filter')},
             'createdAt': int(time.time()), 'mounts': [str(path) for path in roots]}
    atomic(folder / 'mi_docker.original', CONFIG.read_bytes())
    save(folder, state)
    atomic(BASE / 'repair.py', Path(__file__).read_bytes())
    try:
        # The persistent worker, not the temporary wrapper, performs timed rollback.
        run(['systemd-run', '--unit=' + unit(state), '--on-active=10m', '--timer-property=AccuracySec=1s',
             '/usr/bin/python3', '-B', str(BASE / 'repair.py'), '--rollback', state['id']])
    except BaseException:
        state['phase'] = 'not-started'
        save(folder, state)
        raise
    print('事务编号：%s；10 分钟回退已启用。' % state['id'], flush=True)
    try:
        if report['legacyServiceActive'] or report['legacyServiceEnabled']:
            flags = ['--runtime'] if report['legacyServiceEnableMode'] == 'enabled-runtime' else []
            run(['systemctl', 'disable', '--now', *flags, 'bridge-net.service'])
        run(['uci', 'set', OPTION + '=1'])
        run(['uci', 'commit', 'mi_docker'])
        if ids:
            docker('stop', '-t', '20', *ids, timeout=90)
        if not all(os.path.ismount(path) for path in roots):
            raise RuntimeError('存储池挂载发生变化')
        restart_and_restore(ids)
        for table in ('nat', 'filter'):
            for rule in rules(table):
                if owned(rule):
                    ipt(table, '-D', *rule[1:])
        after = audit()
        if not after['healthy'] or after['needsChange'] or not set(ids).issubset(running()):
            raise RuntimeError('原生 NAT、转发规则或原容器状态验证失败')
        print(json.dumps(after, ensure_ascii=False, indent=2), flush=True)
        print('基础检查通过。请验证容器出网、DNS、端口映射及下载器连接。\n'
              '全部正常后在 10 分钟内执行：\n'
              '  sh fix-docker-bridge.sh --confirm %s\n'
              '未确认会自动回退；此次未更改入站端口、IPv6 或重启策略。' % state['id'], flush=True)
    except BaseException:
        rollback(folder, state)
        raise


def confirm(folder, state):
    if state['phase'] != 'pending':
        raise RuntimeError('只能确认 pending 状态的事务')
    if audit()['needsChange'] or not set(state['running']).issubset(running()):
        raise RuntimeError('当前规则或容器状态不正常，不能确认')
    if not all(os.path.ismount(path) for path in state['mounts']):
        raise RuntimeError('存储池挂载发生变化，不能确认')
    state['phase'] = 'confirmed'
    state['confirmedAt'] = int(time.time())
    save(folder, state)
    disarm(state)
    print('已确认原生 Docker 网络，自动回退已取消。配置会在后续 Docker 启动时重新生成。')


def main(argv=None):
    parser = argparse.ArgumentParser(description='小米 NAS Docker IPv4 bridge 原生出口修复（默认只读检查）')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--check', action='store_true', help='只读检查，不改配置或重启服务')
    group.add_argument('--apply', action='store_true', help='修复并设置 10 分钟自动回退；会重启 Docker')
    group.add_argument('--confirm', metavar='ID', help='用户验证联网后确认事务，取消回退')
    group.add_argument('--rollback', metavar='ID', help='回退尚未确认的事务')
    parser.add_argument('--yes', action='store_true', help='明确允许 --apply 中断 Docker，跳过交互确认')
    args = parser.parse_args(argv)
    if args.yes and not args.apply:
        parser.error('--yes 只能与 --apply 同时使用')
    if os.geteuid() != 0:
        raise RuntimeError('需要 NAS root 命令行；本工具不负责获取 root')
    if not (args.apply or args.confirm or args.rollback):
        report = audit()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if not report['needsChange'] else 2
    if args.apply and not audit()['needsChange']:
        print('当前原生 bridge 网络正常，无需修复；未重启 Docker、未写入文件。')
        return 0
    secure_dir(BASE / 'transactions')
    lock_fd = os.open(BASE / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'r+') as lock:
        # A watchdog firing during a slow Docker restart must wait for this
        # transaction, not exit once with EAGAIN and silently lose its rollback.
        flags = fcntl.LOCK_EX if args.rollback else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(lock, flags)
        if args.apply:
            apply(args.yes)
        else:
            folder, state = transaction(args.confirm or args.rollback)
            if args.confirm:
                confirm(folder, state)
            else:
                rollback(folder, state)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
        print('错误：' + str(error), file=sys.stderr)
        sys.exit(1)
