#!/usr/bin/env python3
"""Xiaomi NAS Docker bridge repair, with optional IPv6 and scoped nftables NAT."""
import argparse
import fcntl
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import socket
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import tarfile
import urllib.request

BASE = Path('/data/minas/docker/network')
CONFIG = Path('/etc/config/mi_docker')
RUNTIME = Path('/tmp/dockerd/daemon.json')
FORWARD = Path('/proc/sys/net/ipv4/ip_forward')
TAG = 'minas-bridge-net'
OPTION = 'mi_docker.globals.iptables'
ENV = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/data/docker',
       'LC_ALL': 'C', 'HOME': '/root'}
ID = re.compile(r'[0-9]{8}-[0-9]{6}-[a-z0-9_]{8}')
IPV6_DEFAULT = 'fd6d:696e:6173:1::/64'
IPV6_TABLE = 'minas_docker_ipv6'
IPV6_UNIT = 'minas-docker-ipv6'
UNITS = Path('/etc/systemd/system')
PROC6 = Path('/proc/sys/net/ipv6/conf')
WATCH_RUNTIME = Path('/run/minas-docker-ipv6')
# Debian bookworm ARM64 packages; SHA-256 from its official Packages index.
# Extracted privately, never installed over the NAS libraries or package database.
NFT_PACKAGES = (
    ('g/gmp/libgmp10_6.2.1+dfsg1-1.1_arm64.deb', '9906387c1dd806518c915bd8616d072c741061d7fa26b222e52763456060b31a'),
    ('i/iptables/libxtables12_1.8.9-2_arm64.deb', '8884a42f98b989e5ba2e91d931b5c6e9eb72568e5d3001d97d73d7913890a7b3'),
    ('j/jansson/libjansson4_2.14-2_arm64.deb', '49fda2bcfdf6b78098a5cdc9eaee23ef1f3b331559f02f1e32bc26f1fb6b4aec'),
    ('libb/libbsd/libbsd0_0.11.7-2_arm64.deb', 'ef00f132ddd268ee67d756998723ee18543db27ab34930c0a9f1cff75cf55382'),
    ('libe/libedit/libedit2_3.1-20221030-2_arm64.deb', '6ca1e355586ebfcfe8122b4d317fcaad4a9a10ca455c6a9bbd36f55bf4be85d3'),
    ('libm/libmd/libmd0_1.0.4-2_arm64.deb', '99a8c7dd591fae9fb37d8bf8dfdffa850e207fa405b3198c5b24711a5f972381'),
    ('libm/libmnl/libmnl0_1.0.4-3_arm64.deb', 'd63aafb6f2c07db8fcb135b00ff915baf72ef8a3397e773c9c24d67950c6a46c'),
    ('libn/libnftnl/libnftnl11_1.2.4-2_arm64.deb', 'fd8b6f2ea21d60e25b07dfee8804ece79c2b18309df7773b091a587135565b9f'),
    ('n/ncurses/libtinfo6_6.4-4_arm64.deb', 'baef0f6776f84c7eed4f1146d6e5774689567dad43216894d41da02e6608e4b3'),
    ('n/nftables/libnftables1_1.0.6-2+deb12u2_arm64.deb', '7470db9de55d13b40bf7d4c928adce4b1e58aada8a22d614907b52a9724906b2'),
    ('n/nftables/nftables_1.0.6-2+deb12u2_arm64.deb', 'f5906f44fffdb190f84e3fdd6c07cba399205de5173f67e8742876a7865e2fc9'),
)


def run(args, check=True, timeout=45, input_text=None):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True,
                            timeout=timeout, env=ENV, cwd='/', input=input_text)
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
    bridge = bridge_interface(network)
    result = []
    for item in network.get('IPAM', {}).get('Config') or []:
        subnet = ipaddress.ip_network(item.get('Subnet', ''))
        if subnet.version == 4:
            result.append(['POSTROUTING', '-s', str(subnet), '!', '-o', bridge, '-j', 'MASQUERADE'])
    return result


def bridge_interface(network):
    ident = network['Id']
    if not re.fullmatch(r'[a-f0-9]{64}', ident):
        raise RuntimeError('异常的网络 ID')
    bridge = (network.get('Options') or {}).get('com.docker.network.bridge.name') or (
        'docker0' if network['Name'] == 'bridge' else 'br-' + ident[:12])
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', bridge) or bridge == 'lo':
        raise RuntimeError('异常的桥接网卡名')
    return bridge


def nft_command():
    native = shutil.which('nft', path=ENV['PATH'])
    if native:
        return [native]
    root = BASE / 'nft-runtime'
    binary = root / 'usr/sbin/nft'
    if binary.is_file():
        return ['/lib/ld-linux-aarch64.so.1', '--library-path',
                str(root / 'usr/lib/aarch64-linux-gnu') + ':' + str(root / 'lib/aarch64-linux-gnu'), str(binary)]
    raise RuntimeError('缺少 nftables；首次 --apply --ipv6 会从 Debian 官方下载并校验独立工具')


def nft(*args, **kwargs):
    return run([*nft_command(), *args], **kwargs)


def ensure_nft():
    try:
        nft('--version')
        return
    except (RuntimeError, OSError):
        pass
    if os.uname().machine not in ('aarch64', 'arm64'):
        raise RuntimeError('自动准备 nftables 仅支持 ARM64 NAS，请先安装系统 nft 命令')
    target = BASE / 'nft-runtime'
    if target.exists() or target.is_symlink():
        raise RuntimeError('已有 nft-runtime 无法运行，请先检查，停止覆盖')
    folder = Path(tempfile.mkdtemp(prefix='.nft-', dir=BASE))
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for filename, digest in NFT_PACKAGES:
            print('准备 IPv6 工具：' + filename.rsplit('/', 1)[-1], flush=True)
            with opener.open('https://deb.debian.org/debian/pool/main/' + filename, timeout=30) as response:
                value = response.read(16 * 1024 * 1024 + 1)
            if hashlib.sha256(value).hexdigest() != digest:
                raise RuntimeError('Debian 工具摘要不匹配')
            if value[:8] != b'!<arch>\n':
                raise RuntimeError('无效 Debian 软件包')
            offset, extracted = 8, False
            while offset + 60 <= len(value):
                header = value[offset:offset + 60]
                size = int(header[48:58])
                label = header[:16].decode().strip().rstrip('/')
                payload = value[offset + 60:offset + 60 + size]
                offset += 60 + size + size % 2
                if label.startswith('data.tar'):
                    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:*') as archive:
                        for member in archive.getmembers():
                            # Debian data archives contain relative library symlinks.
                            if member.name.startswith('/') or '..' in Path(member.name).parts or member.isdev():
                                raise RuntimeError('Debian 软件包含不安全路径')
                            if member.issym() and (member.linkname.startswith('/') or '..' in Path(member.linkname).parts):
                                raise RuntimeError('Debian 软件包含不安全链接')
                        archive.extractall(folder, filter='data')
                    extracted = True
            if not extracted:
                raise RuntimeError('Debian 软件包缺少运行文件')
        atomic(folder / 'packages.json', json.dumps(NFT_PACKAGES, indent=2) + '\n')
        os.replace(folder, target)
    finally:
        if folder.exists():
            shutil.rmtree(folder)
    nft('--version')


def ipv6_bridges(networks):
    result = []
    for network in networks:
        if not network.get('EnableIPv6') or network.get('Driver') != 'bridge' or network.get('Internal'):
            continue  # Preserve internal, explicitly routed and IPv4-only networks.
        if str((network.get('Options') or {}).get('com.docker.network.bridge.enable_ip_masquerade', 'true')).lower() == 'false':
            continue
        bridge = bridge_interface(network)
        for item in network.get('IPAM', {}).get('Config') or []:
            subnet = ipaddress.ip_network(item.get('Subnet', ''))
            if subnet.version == 6:
                result.append((bridge, str(subnet), str((network.get('Options') or {}).get(
                    'com.docker.network.bridge.enable_icc', 'true')).lower() != 'false'))
    return sorted(set(result))


def ipv6_rules(networks):
    bridges = ipv6_bridges(networks)
    lines = ['add table ip6 ' + IPV6_TABLE, 'flush table ip6 ' + IPV6_TABLE,
             'add chain ip6 %s forward { type filter hook forward priority 0; policy accept; }' % IPV6_TABLE,
             'add chain ip6 %s postrouting { type nat hook postrouting priority 100; policy accept; }' % IPV6_TABLE]
    # All ingress restrictions precede egress accepts, including cross-bridge traffic.
    for bridge, subnet, icc in bridges:
        prefix = 'add rule ip6 %s forward oifname "%s" ' % (IPV6_TABLE, bridge)
        lines.append(prefix + 'ct state established,related counter accept')
        if icc:
            lines.append(prefix + 'iifname "%s" counter accept' % bridge)
        lines.append(prefix + 'meta l4proto ipv6-icmp counter accept')
        lines.append(prefix + 'counter drop')
    for bridge, subnet, _ in bridges:
        lines.append('add rule ip6 %s forward iifname "%s" ip6 saddr %s counter accept' % (IPV6_TABLE, bridge, subnet))
        lines.append('add rule ip6 %s postrouting ip6 saddr %s oifname != "%s" counter masquerade' % (IPV6_TABLE, subnet, bridge))
    for bridge in sorted({item[0] for item in bridges}):
        lines.append('add rule ip6 %s forward iifname "%s" counter drop' % (IPV6_TABLE, bridge))
    return '\n'.join(lines) + '\n'


def nft_digest(text):
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k not in ('handle', 'packets', 'bytes', 'metainfo')}
        if isinstance(value, list):
            return [clean(x) for x in value if not (isinstance(x, dict) and 'metainfo' in x)]
        return value
    return hashlib.sha256(json.dumps(clean(json.loads(text)), sort_keys=True).encode()).hexdigest()


def ipv6_report(networks, required=False, subnet=None):
    try:
        runtime = json.loads(RUNTIME.read_text())
    except (OSError, ValueError):
        runtime = {}
    enabled = runtime.get('ipv6') is True
    result = {'enabled': enabled, 'subnet': runtime.get('fixed-cidr-v6'),
              'bridges': [list(x[:2]) for x in ipv6_bridges(networks)], 'healthy': False}
    if not (required or enabled):
        return result
    try:
        uci = run(['uci', '-q', 'get', 'mi_docker.globals.ipv6'], check=False)
        uci_subnet = run(['uci', '-q', 'get', 'mi_docker.globals.fixed_cidr_v6'], check=False)
        result['uciEnabled'] = uci.returncode == 0 and uci.stdout.strip() in ('1', 'true', 'yes', 'on')
        result['uciSubnet'] = uci_subnet.stdout.strip() if uci_subnet.returncode == 0 else None
        state = json.loads((BASE / 'ipv6-state.json').read_text())
        actual = nft('-j', 'list', 'table', 'ip6', IPV6_TABLE, check=False)
        expected = hashlib.sha256(ipv6_rules(networks).encode()).hexdigest()
        default = next((x for x in networks if x['Name'] == 'bridge'), {})
        result['rulesHealthy'] = actual.returncode == 0 and state.get('actualDigest') == nft_digest(actual.stdout) and state.get('desiredDigest') == expected
        result['eventServiceEnabled'] = run(['systemctl', 'is-enabled', IPV6_UNIT + '.service'], check=False).stdout.strip() == 'enabled'
        result['eventServiceActive'] = run(['systemctl', 'is-active', IPV6_UNIT + '.service'], check=False).returncode == 0
        result['eventMode'] = '--watch-ipv6' in run(['systemctl','show',IPV6_UNIT+'.service','-p','ExecStart','--value']).stdout
        result['forwarding'] = (PROC6 / 'all/forwarding').read_text().strip() == '1'
        result['healthy'] = enabled and result['uciEnabled'] and result['uciSubnet'] == result['subnet'] and default.get('EnableIPv6') is True and result['rulesHealthy'] and result['eventServiceEnabled'] and result['eventServiceActive'] and result['eventMode'] and result['forwarding'] and (subnet is None or result['subnet'] == subnet)
    except (OSError, ValueError, RuntimeError) as error:
        result['detail'] = str(error)[:250]
    return result


def ipv6_sysctls():
    return {str(p.relative_to(PROC6)): p.read_text().strip() for p in PROC6.glob('*/*')
            if p.name in ('forwarding', 'accept_ra')}


def protect_ipv6_ra():
    # Read the kernel table directly: RA lifetime refreshes also emit route
    # events, so spawning ip would repeatedly load binaries from persistent media.
    routes = Path('/proc/net/ipv6_route').read_text().splitlines()
    interfaces = {fields[-1] for line in routes if len(fields := line.split()) == 10
                  and fields[0] == '0' * 32 and fields[1] == '00'
                  and re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', fields[-1])}
    for interface in interfaces:
        path = PROC6 / interface / 'accept_ra'
        if path.is_file() and path.read_text().strip() == '1':
            path.write_text('2\n')


def sync_route_state():
    # Route/address/link events cannot change Docker's subnet definitions.
    # Docker and nft events still run the full network/rule reconciliation.
    protect_ipv6_ra()
    if (PROC6 / 'all/forwarding').read_text().strip() != '1':
        (PROC6 / 'all/forwarding').write_text('1\n')


def sync_ipv6():
    runtime = json.loads(RUNTIME.read_text())
    if runtime.get('ipv6') is not True:
        raise RuntimeError('Docker 尚未启用 IPv6，停止应用 IPv6 规则')
    networks = inspect_networks()  # Failure must leave the last valid table intact.
    desired = ipv6_rules(networks)
    digest = hashlib.sha256(desired.encode()).hexdigest()
    current = nft('-j', 'list', 'table', 'ip6', IPV6_TABLE, check=False)
    try:
        old = json.loads((BASE / 'ipv6-state.json').read_text())
    except (OSError, ValueError):
        old = {}
    if current.returncode or old.get('desiredDigest') != digest or old.get('actualDigest') != nft_digest(current.stdout):
        nft('-c', '-f', '-', input_text=desired)
        nft('-f', '-', input_text=desired)
        actual = nft('-j', 'list', 'table', 'ip6', IPV6_TABLE).stdout
        atomic(BASE / 'ipv6-state.json', json.dumps({'desiredDigest': digest, 'actualDigest': nft_digest(actual)}) + '\n')
    # Preserve RA routes on upstream interfaces where the kernel manages RAs.
    sync_route_state()


def event_commands():
    executable = '/data/docker/docker' if Path('/data/docker/docker').is_file() else '/usr/bin/docker'
    return {
        'docker': [executable,'--host=unix:///var/run/docker.sock','events','--filter','type=network','--filter','type=daemon','--format','{{json .}}'],
        'firewall': [*nft_command(),'monitor','ruleset'],
    }


def route_events():
    # BusyBox ip on the NAS has no monitor command. Subscribe to the kernel.
    channel = socket.socket(socket.AF_NETLINK,socket.SOCK_RAW,socket.NETLINK_ROUTE)
    try:
        channel.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1024*1024)
        channel.bind((0,0x001|0x100|0x400))  # link, IPv6 address, IPv6 route
        channel.setblocking(False)
        return channel
    except BaseException:
        channel.close();raise


def relevant_route_event(data):
    offset = 0; changed = False
    while offset < len(data):
        if len(data)-offset < 16: raise RuntimeError('路由事件头损坏，重新订阅')
        size,kind,flags,sequence,pid = struct.unpack_from('=IHHII',data,offset)
        if size < 16 or offset+size > len(data): raise RuntimeError('路由事件长度损坏，重新订阅')
        if kind == 4: raise RuntimeError('路由事件丢失，重新订阅')
        if kind == 2:
            if size < 20 or struct.unpack_from('=i',data,offset+16)[0]:
                raise RuntimeError('路由事件监听错误，重新订阅')
        changed = changed or kind in (16,17,20,21,24,25)
        offset += (size+3)&~3
    return changed


def relevant_event(source, line):
    if source == 'firewall':
        return bool(re.search(r'\b'+re.escape(IPV6_TABLE)+r'\b',line))
    return bool(line.strip())


def event_sync():
    secure_dir(BASE)
    fd = os.open(BASE/'lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'r+') as lock:
        # Wait for a real pending event; do not poll while an upgrade owns it.
        fcntl.flock(lock,fcntl.LOCK_EX)
        sync_ipv6()


def sync_events(pending):
    if pending == {'route'}:
        sync_route_state()
    else:
        event_sync()


def watch_ipv6():
    """Block on Docker/netlink events. No periodic timer or idle timeout."""
    secure_dir(WATCH_RUNTIME)
    fd = os.open(WATCH_RUNTIME/'watch.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'r+') as lock, selectors.DefaultSelector() as selector:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        processes = []; route = None
        try:
            for name,command in event_commands().items():
                child = subprocess.Popen(command,stdout=subprocess.PIPE,stderr=None,env=ENV,cwd='/')
                processes.append(child);os.set_blocking(child.stdout.fileno(),False)
                selector.register(child.stdout,selectors.EVENT_READ,dict(name=name,buffer=b''))
            route = route_events()
            selector.register(route,selectors.EVENT_READ,dict(name='route'))
            pending = {'startup'}; deadline = time.monotonic()+1; count = 0
            while True:
                timeout = max(0,deadline-time.monotonic()) if pending else None
                for key,_ in selector.select(timeout):
                    if key.fileobj is route:
                        data,ancillary,flags,address = route.recvmsg(1024*1024)
                        if not data or flags&socket.MSG_TRUNC: raise RuntimeError('路由事件中断或丢失，重新订阅')
                        if relevant_route_event(data):
                            if not pending: deadline = time.monotonic()+1
                            pending.add('route')
                        continue
                    data = os.read(key.fileobj.fileno(),65536)
                    require_event = key.data
                    if not data: raise RuntimeError('事件监听中断：'+require_event['name'])
                    require_event['buffer'] += data
                    if len(require_event['buffer']) > 1024*1024: raise RuntimeError('事件缓冲溢出，重新订阅')
                    while b'\n' in require_event['buffer']:
                        line,require_event['buffer'] = require_event['buffer'].split(b'\n',1)
                        if relevant_event(require_event['name'],line.decode(errors='replace')):
                            if not pending: deadline = time.monotonic()+1
                            pending.add(require_event['name'])
                if pending and time.monotonic() >= deadline:
                    sync_events(pending)
                    count += 1
                    atomic(WATCH_RUNTIME/'status.json',json.dumps(dict(pid=os.getpid(),syncCount=count,lastTrigger=sorted(pending),lastSync=int(time.time())))+'\n')
                    pending.clear()
        finally:
            if route is not None: route.close()
            for child in processes:
                if child.poll() is None: child.terminate()
            for child in processes:
                try: child.wait(timeout=5)
                except subprocess.TimeoutExpired: child.kill();child.wait(timeout=5)
                child.stdout.close()


def ipv6_snapshot():
    files = {}
    for path in (UNITS / (IPV6_UNIT + '.service'), UNITS / (IPV6_UNIT + '.timer'), BASE / 'ipv6-state.json'):
        if path.is_symlink():
            raise RuntimeError('IPv6 状态或服务不允许使用符号链接：' + str(path))
        if path.exists() and (not path.is_file() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022):
            raise RuntimeError('IPv6 服务文件权限不安全')
        files[str(path)] = path.read_text() if path.exists() else None
    table = nft('-s', 'list', 'table', 'ip6', IPV6_TABLE, check=False)
    if table.returncode not in (0, 1):
        raise RuntimeError('无法备份原 IPv6 规则')
    options = {}
    for name in ('ipv6', 'fixed_cidr_v6'):
        value = run(['uci', '-q', 'get', 'mi_docker.globals.' + name], check=False)
        if value.returncode not in (0, 1):
            raise RuntimeError('无法读取 IPv6 UCI 选项')
        options[name] = value.stdout.strip() if value.returncode == 0 else None
    return {'uci': options, 'sysctls': ipv6_sysctls(), 'files': files,
            'table': table.stdout if table.returncode == 0 else None,
            'serviceEnabled': run(['systemctl','is-enabled',IPV6_UNIT+'.service'],check=False).stdout.strip() == 'enabled',
            'serviceActive': run(['systemctl','is-active',IPV6_UNIT+'.service'],check=False).returncode == 0,
            'timerEnabled': run(['systemctl', 'is-enabled', IPV6_UNIT + '.timer'], check=False).stdout.strip() == 'enabled',
            'timerActive': run(['systemctl', 'is-active', IPV6_UNIT + '.timer'], check=False).returncode == 0}


def install_ipv6_service():
    timer = UNITS/(IPV6_UNIT+'.timer')
    if timer.exists() and ('OnUnitActiveSec=30s' not in timer.read_text() or 'Refresh Docker IPv6 rules' not in timer.read_text()):
        raise RuntimeError('IPv6 定时器已被手动修改，拒绝覆盖')
    run(['systemctl','disable','--now',IPV6_UNIT+'.timer'],check=False)
    run(['systemctl','stop',IPV6_UNIT+'.service'],check=False)
    atomic(UNITS / (IPV6_UNIT + '.service'), '''[Unit]
Description=Xiaomi NAS Docker IPv6 event-driven NAT and isolation
Wants=network-online.target
After=network-online.target docker.service
BindsTo=docker.service
PartOf=docker.service
StartLimitIntervalSec=0

[Service]
Type=simple
UMask=0077
RuntimeDirectory=minas-docker-ipv6
RuntimeDirectoryMode=0700
ExecStart=/usr/bin/python3 -B /data/minas/docker/network/repair.py --watch-ipv6
Restart=on-failure
RestartSec=5s
TimeoutStopSec=15s

[Install]
WantedBy=docker.service
''')
    timer.unlink(missing_ok=True)
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', IPV6_UNIT + '.service'])


def restore_ipv6(saved):
    run(['systemctl', 'disable', '--now', IPV6_UNIT + '.service'], check=False)
    run(['systemctl', 'disable', '--now', IPV6_UNIT + '.timer'], check=False)
    run(['systemctl', 'stop', IPV6_UNIT + '.service'], check=False)
    # Only this tool's table is removed/replaced; never flush the ruleset.
    present = nft('list', 'table', 'ip6', IPV6_TABLE, check=False).returncode == 0
    batch = ('delete table ip6 %s\n' % IPV6_TABLE if present else '') + (saved['table'] or '')
    if batch:
        nft('-f', '-', input_text=batch)
    for name, data in saved['files'].items():
        path = remap_data_path(name)
        allowed = {UNITS / (IPV6_UNIT + '.service'), UNITS / (IPV6_UNIT + '.timer'), BASE / 'ipv6-state.json'}
        if path not in allowed:
            raise RuntimeError('异常的 IPv6 备份文件路径')
        if data is None:
            path.unlink(missing_ok=True)
        else:
            atomic(path, rebase_data_text(data))
    for name, value in sorted(saved['sysctls'].items(), key=lambda x: (not x[0].startswith('all/'), x[0])):
        if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}/(forwarding|accept_ra)', name) or value not in ('0', '1', '2'):
            raise RuntimeError('异常的 IPv6 转发备份')
        path = PROC6 / name
        if path.exists():
            path.write_text(value + '\n')
    run(['systemctl', 'daemon-reload'])
    if saved['timerEnabled']:
        run(['systemctl', 'enable', IPV6_UNIT + '.timer'])
    if saved['timerActive']:
        run(['systemctl', 'start', IPV6_UNIT + '.timer'])
    if saved.get('serviceEnabled'):
        run(['systemctl','enable',IPV6_UNIT+'.service'])
    if saved.get('serviceActive'):
        run(['systemctl','start',IPV6_UNIT+'.service'])


def audit(ipv6=False, subnet=None):
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
    report = {'uciIptables': configured, 'runtimeIptables': native, 'ipForward': forwarded,
            'dockerForwardHook': hooks, 'bridgeNetworks': checked, 'missingNat': sorted(set(missing)),
            'missingForward': sorted(set(missing_forward)),
            'legacyTaggedRules': legacy, 'legacyServiceActive': active, 'legacyServiceEnabled': enabled,
            'legacyServiceEnableMode': enable_mode,
            'healthy': healthy, 'needsChange': not healthy or bool(legacy) or active or enabled}
    six = ipv6_report(networks, ipv6, subnet)
    report['ipv6'] = six
    if ipv6 or six['enabled']:
        report['healthy'] = healthy and six['healthy']
        report['needsChange'] = report['needsChange'] or not six['healthy']
    return report

# BEGIN MINAS DATA LAYOUT (generated)
"""Small standalone layout primitives; generated constants select one owner."""
DATA_LAYOUT_VERSION = 1
DATA_LAYOUT_ROOT = '/data/minas'


def _layout_secure(path):
    import os,stat
    from pathlib import Path
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts:raise ValueError('invalid layout path')
    for node in (path,*path.parents):
        s=node.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:
            raise ValueError('unsafe layout directory: '+str(node))
    return path


def _layout_atomic(path,data,mode=0o600):
    import os,tempfile
    from pathlib import Path
    path=Path(path);_layout_secure(path.parent)
    fd,name=tempfile.mkstemp(prefix='.layout-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data if isinstance(data,bytes) else data.encode())
            stream.flush();os.fchmod(stream.fileno(),mode);os.fsync(stream.fileno())
        os.replace(name,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)


def _layout_initialize():
    import json,os,stat
    from pathlib import Path
    if os.geteuid()!=0:raise ValueError('root required for layout')
    root=Path(DATA_LAYOUT_ROOT);_layout_secure(root.parent)
    if not root.exists():
        root.mkdir(mode=0o755);root.chmod(0o755)
    _layout_secure(root)
    if not root.stat().st_mode&0o001:raise ValueError('layout root must allow traversal')
    marker=root/'layout.json'
    if os.path.lexists(marker):
        s=marker.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('unsafe layout marker')
        value=json.loads(marker.read_text())
        if value.get('version')!=DATA_LAYOUT_VERSION or value.get('root')!=DATA_LAYOUT_ROOT:raise ValueError('unsupported layout')
    else:_layout_atomic(marker,json.dumps(dict(version=DATA_LAYOUT_VERSION,root=DATA_LAYOUT_ROOT))+'\n',0o644)


def ensure_data_layout():
    """Initialize only the current layout; never inspect or move former paths."""
    _layout_initialize()


def remap_data_path(value):
    from pathlib import Path
    path=Path(value)
    if not path.is_absolute() or '..' in path.parts:raise ValueError('invalid state path')
    return path


def rebase_data_text(text):
    return text
# END MINAS DATA LAYOUT

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
    if state.get('ipv6'):
        run(['systemctl', 'stop', IPV6_UNIT + '.timer'], check=False)
        run(['systemctl', 'stop', IPV6_UNIT + '.service'], check=False)
        for name, value in state['ipv6']['uci'].items():
            option = 'mi_docker.globals.' + name
            if value is None:
                run(['uci', '-q', 'delete', option], check=False)
            else:
                run(['uci', 'set', option + '=' + value])
    run(['uci', 'commit', 'mi_docker'])
    if state['helperEnabled']:
        flags = ['--runtime'] if state.get('helperEnableMode') == 'enabled-runtime' else []
        run(['systemctl', 'enable', *flags, 'bridge-net.service'])
    restart_and_restore(ids)
    if state.get('ipv6'):
        restore_ipv6(state['ipv6'])
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


def apply(yes=False, ipv6=False, subnet=None):
    report = audit(ipv6, subnet)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if not report['needsChange']:
        print('当前原生 bridge 网络正常，无需进一步网络修复。')
        return
    if report['uciIptables'] not in (None, '0', '1', 'true', 'false', 'yes', 'no', 'on', 'off'):
        raise RuntimeError('不识别的 UCI iptables 值，停止修改')
    if not CONFIG.is_file() or 'mi_docker' not in Path('/usr/sbin/dockerd_prepare.sh').read_text():
        raise RuntimeError('不是受支持的小米 NAS Docker 配置生成方式')
    if ipv6:
        prepare = Path('/usr/sbin/dockerd_prepare.sh').read_text()
        if 'fixed_cidr_v6' not in prepare or '"ipv6"' not in prepare:
            raise RuntimeError('当前固件不支持通过 UCI 持久启用 IPv6')
        desired = ipaddress.ip_network(subnet)
        if desired.version != 6 or desired.prefixlen != 64 or not desired.subnet_of(ipaddress.ip_network('fc00::/7')):
            raise RuntimeError('IPv6 出口子网必须是 ULA /64，例如 ' + IPV6_DEFAULT)
        for network in inspect_networks():
            if network['Name'] != 'bridge':
                for item in network.get('IPAM', {}).get('Config') or []:
                    other = ipaddress.ip_network(item.get('Subnet', ''))
                    if other.version == 6 and other.overlaps(desired):
                        raise RuntimeError('IPv6 子网与现有网络重叠：' + network['Name'])
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
    if ipv6:
        ensure_nft()
        state['ipv6'] = ipv6_snapshot()
        state['requestedSubnet'] = subnet
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
        if ipv6:
            run(['systemctl', 'stop', IPV6_UNIT + '.timer'], check=False)
            run(['systemctl', 'stop', IPV6_UNIT + '.service'], check=False)
            run(['uci', 'set', 'mi_docker.globals.ipv6=1'])
            run(['uci', 'set', 'mi_docker.globals.fixed_cidr_v6=' + subnet])
            protect_ipv6_ra()
        run(['uci', 'commit', 'mi_docker'])
        if ids:
            docker('stop', '-t', '20', *ids, timeout=90)
        if not all(os.path.ismount(path) for path in roots):
            raise RuntimeError('存储池挂载发生变化')
        restart_and_restore(ids)
        if ipv6:
            sync_ipv6()
            install_ipv6_service()
        for table in ('nat', 'filter'):
            for rule in rules(table):
                if owned(rule):
                    ipt(table, '-D', *rule[1:])
        after = audit(ipv6, subnet)
        if not after['healthy'] or after['needsChange'] or not set(ids).issubset(running()):
            raise RuntimeError('原生 NAT、转发规则或原容器状态验证失败')
        print(json.dumps(after, ensure_ascii=False, indent=2), flush=True)
        print('基础检查通过。请验证容器出网、DNS、端口映射及下载器连接。\n'
              '全部正常后在 10 分钟内执行：\n'
              '  sh fix-docker-bridge.sh --confirm %s\n'
              '未确认会自动回退；此次未更改容器定义、入站端口映射或重启策略。' % state['id'], flush=True)
    except BaseException:
        rollback(folder, state)
        raise


def confirm(folder, state):
    if state['phase'] != 'pending':
        raise RuntimeError('只能确认 pending 状态的事务')
    if audit(bool(state.get('ipv6')), state.get('requestedSubnet'))['needsChange'] or not set(state['running']).issubset(running()):
        raise RuntimeError('当前规则或容器状态不正常，不能确认')
    if not all(os.path.ismount(path) for path in state['mounts']):
        raise RuntimeError('存储池挂载发生变化，不能确认')
    state['phase'] = 'confirmed'
    state['confirmedAt'] = int(time.time())
    save(folder, state)
    disarm(state)
    print('已确认原生 Docker 网络，自动回退已取消。配置会在后续 Docker 启动时重新生成。')


def main(argv=None):
    parser = argparse.ArgumentParser(description='小米 NAS Docker bridge 双栈出口修复（默认只读检查；--ipv6 启用 IPv6）')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--check', action='store_true', help='只读检查，不改配置或重启服务')
    group.add_argument('--apply', action='store_true', help='修复并设置 10 分钟自动回退；会重启 Docker')
    group.add_argument('--confirm', metavar='ID', help='用户验证联网后确认事务，取消回退')
    group.add_argument('--rollback', metavar='ID', help='回退尚未确认的事务')
    group.add_argument('--restore', metavar='ID', help='撤销已确认的网络修复；会重启 Docker')
    group.add_argument('--sync-ipv6', action='store_true', help=argparse.SUPPRESS)
    group.add_argument('--watch-ipv6', action='store_true', help=argparse.SUPPRESS)
    group.add_argument('--install-ipv6-events', action='store_true', help='将已启用的 IPv6 维护改为纯事件驱动，不重启 Docker')
    parser.add_argument('--ipv6', action='store_true', help='检查或通过 UCI 启用默认 bridge IPv6、nftables NAT 和开机规则恢复')
    parser.add_argument('--ipv6-subnet', help='默认 bridge 使用的 ULA /64；仅适用于 --ipv6')
    parser.add_argument('--yes', action='store_true', help='明确允许 --apply 中断 Docker，跳过交互确认')
    args = parser.parse_args(argv)
    if args.yes and not (args.apply or args.restore):
        parser.error('--yes 只能与 --apply 或 --restore 同时使用')
    if (args.ipv6 or args.ipv6_subnet) and (args.confirm or args.rollback or args.restore or args.sync_ipv6 or args.watch_ipv6 or args.install_ipv6_events):
        parser.error('--ipv6 仅用于 --check 或 --apply')
    if args.ipv6_subnet and not args.ipv6:
        parser.error('--ipv6-subnet 需要 --ipv6')
    subnet = args.ipv6_subnet
    if args.ipv6 and subnet is None:
        try:
            subnet = json.loads(RUNTIME.read_text()).get('fixed-cidr-v6') or IPV6_DEFAULT
        except (OSError, ValueError):
            subnet = IPV6_DEFAULT
    if args.ipv6:
        try:
            subnet = str(ipaddress.ip_network(subnet))
            if ipaddress.ip_network(subnet).version != 6 or ipaddress.ip_network(subnet).prefixlen != 64 or not ipaddress.ip_network(subnet).subnet_of(ipaddress.ip_network('fc00::/7')):
                raise ValueError('需要 ULA /64')
        except ValueError:
            parser.error('IPv6 子网必须是 ULA /64')
    if os.geteuid() != 0:
        raise RuntimeError('需要 NAS root 命令行；本工具不负责获取 root')
    if args.watch_ipv6:
        watch_ipv6(); return 0
    if not (args.apply or args.confirm or args.rollback or args.restore or args.sync_ipv6 or args.install_ipv6_events):
        report = audit(args.ipv6, subnet)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if not report['needsChange'] else 2
    if str(BASE)=='/data/minas/docker/network':ensure_data_layout()
    if args.apply and not audit(args.ipv6, subnet)['needsChange']:
        print('当前原生 bridge 网络正常，无需修复；未重启 Docker、未写入文件。')
        return 0
    secure_dir(BASE / 'transactions')
    lock_fd = os.open(BASE / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'r+') as lock:
        # A watchdog firing during a slow Docker restart must wait for this
        # transaction, not exit once with EAGAIN and silently lose its rollback.
        flags = fcntl.LOCK_EX if (args.rollback or args.sync_ipv6) else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(lock, flags)
        if args.apply:
            apply(args.yes, args.ipv6, subnet)
        elif args.install_ipv6_events:
            if run(['systemctl','is-active','--quiet','docker.service'],check=False).returncode:
                raise RuntimeError('Docker 必须已运行，维护服务升级不会代为启动 Docker')
            for item in (BASE/'transactions').glob('*/state.json'):
                if json.loads(item.read_text())['phase'] == 'pending': raise RuntimeError('存在待确认网络修复事务')
            sync_ipv6(); saved = ipv6_snapshot()
            worker = BASE/'repair.py'; previous = worker.read_bytes() if worker.exists() else None
            atomic(worker,Path(__file__).read_bytes())
            try: install_ipv6_service()
            except BaseException:
                if previous is not None: atomic(worker,previous)
                restore_ipv6(saved); raise
            print('IPv6 维护已改为纯事件驱动，周期定时器已移除；Docker 未重启。')
        elif args.sync_ipv6:
            sync_ipv6()
        else:
            folder, state = transaction(args.confirm or args.rollback or args.restore)
            if args.restore:
                if state['phase'] not in ('pending','confirmed'):raise ValueError('该事务不能恢复')
                if not args.yes and (not sys.stdin.isatty() or input('会重启 Docker。输入 RESTORE：')!='RESTORE'):raise ValueError('未确认恢复')
                state['phase']='pending';save(folder,state)
                rollback(folder,state)
            elif args.confirm:
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
