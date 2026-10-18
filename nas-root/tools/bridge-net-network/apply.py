"""Restore IPv4 outbound networking for the Docker network named bridge-net."""
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

NETWORK = 'bridge-net'
COMMENT = 'minas-bridge-net'
DOCKER = '/data/docker/docker'
IPTABLES = '/usr/sbin/iptables'


def run(args, **kwargs):
    return subprocess.run(args, text=True, timeout=20, **kwargs)


def main():
    if os.geteuid() != 0: raise RuntimeError('Administrator required')
    names = run([DOCKER,'network','ls','--format','{{.Name}}'],capture_output=True,check=True).stdout.splitlines()
    if NETWORK not in names:
        print('bridge-net does not exist; no network or rule created.')
        return
    network = json.loads(run([DOCKER,'network','inspect',NETWORK],capture_output=True,check=True).stdout)[0]
    if network['Name'] != NETWORK or network['Driver'] != 'bridge' or network.get('Internal'):
        raise RuntimeError('bridge-net must be a non-internal bridge network')
    ident = network['Id']
    if not re.fullmatch(r'[a-f0-9]{64}',ident): raise RuntimeError('Unexpected network ID')
    bridge = network['Options'].get('com.docker.network.bridge.name') or 'br-' + ident[:12]
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}',bridge) or bridge in ('lo','docker0'):
        raise RuntimeError('Unexpected bridge interface')
    private = [ipaddress.ip_network(c) for c in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')]
    subnets = []
    for item in network['IPAM']['Config']:
        subnet = ipaddress.ip_network(item['Subnet'])
        if subnet.version == 6: continue
        if not any(subnet.subnet_of(block) for block in private):
            raise RuntimeError('Only RFC1918 IPv4 subnets are supported')
        subnets.append(str(subnet))
    if not subnets: raise RuntimeError('No IPv4 subnet')
    if Path('/proc/sys/net/ipv4/ip_forward').read_text().strip() != '1':
        run(['/usr/sbin/sysctl','-w','net.ipv4.ip_forward=1'],check=True)
    desired = {'nat':[], 'filter':[]}
    for subnet in subnets:
        desired['nat'].append(['POSTROUTING','-s',subnet,'!','-o',bridge,'-m','comment','--comment',COMMENT,'-j','MASQUERADE'])
        desired['filter'].extend([
            ['FORWARD','-i',bridge,'-s',subnet,'!','-o',bridge,'-m','comment','--comment',COMMENT,'-j','ACCEPT'],
            ['FORWARD','-o',bridge,'-d',subnet,'-m','conntrack','--ctstate','RELATED,ESTABLISHED','-m','comment','--comment',COMMENT,'-j','ACCEPT'],
        ])
    for table,rules in desired.items():
        base=[IPTABLES,'-w','5','-t',table]
        for rule in rules:
            found=run(base+['-C']+rule,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            if found.returncode: run(base+['-A']+rule,check=True)
        # Retire only rules owned by this helper after a network recreation.
        # Keep unrelated system/Docker/other-plugin rules untouched.
        for line in run(base+['-S'],capture_output=True,check=True).stdout.splitlines():
            rule=shlex.split(line)
            if len(rule)<3 or rule[0]!='-A' or '--comment' not in rule: continue
            if rule[rule.index('--comment')+1]!=COMMENT: continue
            if bridge not in rule or not any(s in rule for s in subnets):
                run(base+['-D']+rule[1:],check=True)
    print('bridge-net outbound NAT ready: '+', '.join(subnets)+' via '+bridge)


if __name__=='__main__': main()
