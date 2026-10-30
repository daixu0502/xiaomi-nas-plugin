#!/usr/bin/env python3
"""Inspect, clean and uninstall known custom NAS components. No migration."""
import argparse,contextlib,ctypes,fcntl,hashlib,json,os,pwd,re,shutil,stat,subprocess,sys,tempfile
from pathlib import Path

VERSION='1.0.3'
ROOT=Path('/data/minas')
PLUGINS=('dockermanager','mihomo','taskcenter','nascenter','sshrecovery','quickshare','downloadcenter','mediaeditor')
LABELS=dict(zip(PLUGINS,('Docker 管理','Mihomo','计划任务','控制中心','SSH 恢复','文件快传','下载中心','影视编辑')))
OLD={
 '/data/.minas-privileged':'/data/minas/privileged',
 '/data/.minas-plugin-data':'/data/minas/plugins/data',
 '/data/.minas-plugin-recovery':'/data/minas/plugins/recovery',
 '/data/ssh-autostart-fix':'/data/minas/ssh',
 '/data/docker-bridge-repair':'/data/minas/docker/network',
 '/data/.minas-docker-runtime':'/data/minas/docker/runtime',
 '/data/docker-upgrade-backups':'/data/minas/backups/docker',
 '/data/.minas-privileged-backups':'/data/minas/backups/privileged',
 '/data/.sshrecovery-backups':'/data/minas/backups/plugins/sshrecovery',
 '/data/.taskcenter-backups':'/data/minas/backups/plugins/taskcenter',
 '/data/minas-ssh-bootstrap':'/data/minas/backups/ssh-bootstrap',
}
# Plugin removal is delegated to the installed common transaction engine.


def run(args,check=True,**kwargs):
    p=subprocess.run([str(x) for x in args],text=True,capture_output=True,timeout=kwargs.pop('timeout',60),**kwargs)
    if check and p.returncode:raise RuntimeError((p.stderr or p.stdout).strip()[-2000:])
    return p

def load(path):
    path=Path(path)
    if path.is_symlink():raise ValueError('拒绝读取重定向的管理记录：'+str(path))
    return json.loads(path.read_text())

def below(path,base):
    return path==base or path.startswith(base.rstrip('/')+'/')

def identity(path):
    s=Path(path).lstat();return [s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_size,s.st_mtime_ns]

def registries():
    out={}
    for f in Path('/data/plugin').glob('u*.list'):
        if not re.fullmatch(r'u[0-9]+',f.stem):continue
        out[f.stem]=load(f)
    return out

def references():
    """Read metadata and kernel references, never scan user media or cache bodies."""
    refs=[];units=[]
    for base in (Path('/etc/systemd/system'),Path('/run/systemd/system'),Path('/run/systemd/transient'),Path('/etc/cron.d'),Path('/etc/sudoers.d')):
        for p in base.rglob('*') if base.exists() else []:
            if p.is_file() and not p.is_symlink() and p.stat().st_size<200000:
                text=p.read_text(errors='replace')
                refs += [(m.group(0).rstrip("'\";,)"),str(p)) for m in re.finditer(r'/(?:data|home|nas|run|tmp)/[^\s\x00\"\'<>;]+',text)]
                if base.name in ('system','transient') and p.suffix in ('.service','.timer','.socket'):units.append(str(p))
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            for key in ('cwd','exe','root'):
                refs.append((os.readlink(proc/key).removesuffix(' (deleted)'),'进程 '+proc.name))
            for arg in (proc/'cmdline').read_bytes().split(b'\0'):
                if arg.startswith(b'/') and b'\n' not in arg:refs.append((os.fsdecode(arg),'进程 '+proc.name))
            for fd in (proc/'fd').iterdir():
                try:refs.append((os.readlink(fd).removesuffix(' (deleted)'),'文件句柄 '+proc.name))
                except OSError:pass
            for line in (proc/'maps').read_text().splitlines():
                fields=line.split(None,5)
                if len(fields)==6 and fields[5].startswith('/'):refs.append((fields[5].removesuffix(' (deleted)'),'内存映射 '+proc.name))
        except (OSError,ProcessLookupError):pass
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        mount=line.split()[4].replace('\\040',' ')
        refs.append((mount,'挂载点'))
    for user in Path('/home').glob('u*'):
        if not re.fullmatch(r'u[0-9]+',user.name):continue
        for plugin in PLUGINS:
            for name in ('src','tmp','etc','var'):
                p=user/'plugin'/plugin/name
                if p.is_symlink():refs.append((os.path.abspath(os.path.join(p.parent,os.readlink(p))),str(p)))
    return refs,units

def tool_status():
    result={}
    for key,base,marker in (('ssh',ROOT/'ssh',Path('/etc/cron.d/keep-ssh')),('network',ROOT/'docker/network',Path('/etc/systemd/system/minas-docker-ipv6.service')),('docker',ROOT/'docker/runtime',Path('/etc/systemd/system/docker.service.d/80-minas-runtime.conf'))):
        transactions=[]
        for p in (base/'transactions').glob('*/state.json'):
            data=load(p);transactions.append({'id':p.parent.name,'phase':data.get('phase'),'path':str(p),'backup':data.get('backup')})
        active=marker.exists() or any(t['phase'] in ('pending','confirmed','installed','rollback-failed') for t in transactions)
        unit={'ssh':'minas.boot_check.service','network':'minas-docker-ipv6.service','docker':'docker.service'}[key]
        service=run(['systemctl','show',unit,'-p','LoadState','-p','ActiveState','--value'],False).stdout.strip().splitlines()
        result[key]={'name':{'ssh':'SSH 开机恢复','network':'Docker 网络修复','docker':'Docker 升级'}[key],'installed':active,'directory':str(base),'marker':str(marker),'service':{'name':unit,'definitionExists':marker.exists(),'state':service},'transactions':transactions,'residue':base.exists() and not active}
    return result

def scan():
    regs=registries();refs,units=references();tools=tool_status();items={};plugins=[]
    def installed(user,plugin):return bool(regs.get(user,{}).get(plugin,{}).get('install'))
    def add(path,owner,layout,reason='',keep=False,automatic=False):
        p=Path(path)
        if not os.path.lexists(p):return
        path=str(p);used=sorted({origin for target,origin in refs if below(target,path)})
        # Parents used by several installed components are never cleanup targets.
        blocked=keep or bool(used)
        status='使用中' if used or keep else '可清理遗留' if automatic else '未使用／卸载后保留'
        items[path]={'id':hashlib.sha256(path.encode()).hexdigest()[:12],'path':path,'owner':owner,'layout':layout,'status':status,'deletable':not blocked,'automatic':automatic and not blocked,'reason':reason,'references':used[:8],'identity':identity(p),'link':os.readlink(p) if p.is_symlink() else None}
    for old,new in OLD.items():
        docker_backup='docker-upgrade-backups' in old
        owner=next((t['name'] for t in tools.values() if t['directory']==new),'历史公共组件')
        add(old,'Docker 升级备份' if docker_backup else owner,'旧',keep=docker_backup,automatic=not docker_backup,reason='Docker 升级备份始终保留' if docker_backup else '')
    any_plugins=any(installed(u,p) for u in regs for p in PLUGINS)
    for path,name,used in ((ROOT/'privileged','公共权限组件',any_plugins),(ROOT/'plugins/recovery','插件恢复服务',any_plugins)):
        add(path,name,'新',keep=used,reason='仍有已安装插件依赖' if used else '')
    for key,t in tools.items():add(t['directory'],t['name'],'新',keep=t['installed'],reason='卸载恢复所需事务及运行文件' if t['installed'] else '')
    for path in ('/data/minas/backups/docker','/data/docker-upgrade-backups'):
        add(path,'Docker 升级备份','备份',keep=True,reason='用户指定保留')
    for u in sorted(set(regs)|{p.name for p in (ROOT/'plugins/data').glob('u*')}):
        if not re.fullmatch(r'u[0-9]+',u):continue
        for plugin in PLUGINS:
            home=Path('/home')/u/'plugin'/plugin;present=installed(u,plugin)
            if present:
                service=None
                if plugin in ('mihomo','quickshare','downloadcenter'):
                    name='minas-plugin-'+plugin+'-'+u+'.service'
                    report=run(['systemctl','show',name,'-p','LoadState','-p','ActiveState','-p','SubState','-p','MainPID'],False)
                    runtime=dict(x.split('=',1) for x in report.stdout.strip().splitlines() if '=' in x)
                    service={'name':name,'definitionExists':(Path('/etc/systemd/system')/name).is_file(),'runtime':runtime}
                plugins.append({'user':u,'plugin':plugin,'name':LABELS[plugin],'enabled':regs[u][plugin].get('enable'),'home':str(home),'service':service})
            add(ROOT/'plugins/data'/u/plugin,u+'/'+LABELS[plugin],'新',keep=present,reason='现用配置、任务或保护数据' if present else '卸载后的持久数据，手动选择才删除')
            add(Path('/data/.minas-plugin-data')/u/plugin,u+'/'+LABELS[plugin],'旧',automatic=True)
            add(Path('/data/.minas-privileged/modules')/plugin,LABELS[plugin]+'旧权限模块','旧',automatic=True)
            add(ROOT/'privileged/modules'/plugin,LABELS[plugin]+'权限模块','新',keep=any(installed(account,plugin) for account in regs))
            add(home,u+'/'+LABELS[plugin],'安装入口',keep=present)
            for root in Path('/nas').glob('pool[0-9]*'):
                for name in ('pluginsrc','plugintmp'):
                    add(root/u/'plugin'/name/plugin,u+'/'+LABELS[plugin],name,keep=present)
            add(Path('/data/plugin')/('.'+plugin+'-system'),'旧权限模块 '+plugin,'旧',automatic=True)
            add(Path('/data')/('.'+plugin+'-system'),'旧权限模块 '+plugin,'旧',automatic=True)
        reserve=Path('/home')/u/'plugin/.reserve'
        for p in reserve.glob('*/*'):
            if p.parent.name in PLUGINS:add(p,'自定义插件安装卸载备份','备份',automatic=True)
    add(ROOT/'plugins/mediaeditor','影视编辑旧保护数据','旧',automatic=True)
    add(ROOT/'privileged/modules/mediaeditor/var/storage-layout.json','已完成的影视迁移记录','旧',automatic=True)
    for pattern in ('.registry-before-install.*','*.before-local-*'):
        for p in Path('/data/plugin').glob(pattern):add(p,'旧注册信息备份','遗留',automatic=True)
    for path in ('backups/privileged','backups/plugins','backups/ssh-bootstrap','plugins/recovery/deployments','plugins/recovery/operations','docker/runtime/code-backups'):
        base=ROOT/path
        for p in base.iterdir() if base.is_dir() and not base.is_symlink() else []:
            pending=False
            if path=='plugins/recovery/deployments' and (p/'state.json').is_file():
                pending=load(p/'state.json').get('phase') not in ('committed','rolled-back','complete')
            add(p,'历次部署／测试备份','遗留',keep=pending,automatic=True,reason='未完成事务，保留恢复材料' if pending else '')
    migration=ROOT/'migration'
    phases=[load(p).get('phase') for p in migration.glob('transactions/*/state.json')]
    add(migration,'旧迁移工具及事务','旧',keep=any(p not in ('committed','rolled-back','complete') for p in phases),automatic=True)
    for base in ((ROOT/'plugins/recovery/records'),):
        for record in base.glob('u*-*'):
            current=(record/'current').read_text().strip() if (record/'current').is_file() else None
            for generation in record.glob('generation-*'):
                add(generation,'插件恢复副本','恢复',keep=generation.name==current,automatic=generation.name!=current,reason='当前恢复副本必须保留' if generation.name==current else '过期恢复副本')
    for path in ('/run/minas-auto-layout.lock','/run/minas-layout-docker-runtime.lock'):
        add(path,'已退役的目录迁移锁','旧',automatic=True)
    patterns=('minas-ui.*','minas-xunlei.*','minas-cleanup.*','minas-manager.*','minas-docker-upgrade.*','minas-docker-bridge.*','minas-runtime.*','minas-entry.*','minas-spec-*','minas-auto-layout-*','minas-component-*','minas-docker-probe-*','xiaomi-plugin.*','minas-ssh-transport.*')
    for parent in (Path('/run'),Path('/tmp')):
        for pattern in patterns:
            for p in parent.glob(pattern):add(p,'部署／迅雷触发／隔离测试暂存','暂存',automatic=True)
    for unit in units:
        p=Path(unit)
        if re.fullmatch(r'(?:minas-plugin-recovery(?:-watch)?|minas-data-layout-[a-z0-9]+|minas-(?:ui|xunlei|test)[a-z0-9-]*)\.(?:service|timer)',p.name):
            state=run(['systemctl','show',p.name,'-p','ActiveState','--value'],False).stdout.strip()
            add(p,'旧服务定义','服务',keep=state not in ('inactive','failed',''),automatic=True,reason='服务状态：'+state)
    return {'version':VERSION,'plugins':plugins,'tools':tools,'directories':sorted(items.values(),key=lambda x:(x['owner'],x['path']))}

def confirm(message,yes):
    print(message,flush=True)
    if not yes and (not sys.stdin.isatty() or input('输入 YES 执行：').strip()!='YES'):raise ValueError('未确认，未执行')

# FD-relative deletion never follows a symlink or traverses a filesystem boundary.
DELETE_CONTENTS='''import os,stat,sys
def clear(fd,dev):
 for name in os.listdir(fd):
  s=os.stat(name,dir_fd=fd,follow_symlinks=False)
  if stat.S_ISDIR(s.st_mode):
   child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
   try:
    if os.fstat(child).st_dev!=dev:raise ValueError('filesystem boundary')
    clear(child,dev)
   finally:os.close(child)
   os.rmdir(name,dir_fd=fd)
  else:os.unlink(name,dir_fd=fd)
fd=os.open('.',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=int(sys.argv[1]))
try:clear(fd,os.fstat(fd).st_dev)
finally:os.close(fd)
'''

def delete_item(item):
    p=Path(item['path']);expected=item['identity']
    if not p.is_absolute() or '..' in p.parts:raise ValueError('无效清理路径')
    with contextlib.ExitStack() as stack:
        parent=os.open('/',os.O_PATH|os.O_DIRECTORY);stack.callback(os.close,parent)
        for part in p.parts[1:-1]:
            try:parent=os.open(part,os.O_PATH|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            except OSError as e:raise ValueError('父目录存在链接或已改变') from e
            stack.callback(os.close,parent)
        s=os.stat(p.name,dir_fd=parent,follow_symlinks=False)
        if [s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_size,s.st_mtime_ns]!=expected:raise ValueError('目录已变化，请重新扫描：'+str(p))
        if stat.S_ISDIR(s.st_mode):
            fd=os.open(p.name,os.O_PATH|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent);stack.callback(os.close,fd)
            if os.fstat(fd).st_ino!=s.st_ino:raise ValueError('目录已变化')
            demote=None
            if s.st_uid:
                account=pwd.getpwuid(s.st_uid)
                def demote():
                    os.initgroups(account.pw_name,account.pw_gid);os.setgid(account.pw_gid);os.setuid(account.pw_uid)
            # Root opens only O_PATH; cache traversal and deletion run as its owner.
            run([sys.executable,'-B','-c',DELETE_CONTENTS,str(fd)],pass_fds=(fd,),preexec_fn=demote,timeout=300)
            current=os.stat(p.name,dir_fd=parent,follow_symlinks=False)
            if (current.st_dev,current.st_ino)!=(s.st_dev,s.st_ino):raise ValueError('入口身份变化，停止移除')
            os.rmdir(p.name,dir_fd=parent)
        else:os.unlink(p.name,dir_fd=parent)

@contextlib.contextmanager
def operation_lock():
    with contextlib.ExitStack() as stack:
        for path in ('/data/plugin/.local-plugin-installer.lock','/data/minas/privileged/component.lock'):
            if not Path(path).parent.exists():continue
            fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600);stack.callback(os.close,fd)
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield

def clean(ids,yes=False,automatic=False):
    with operation_lock():
        snapshot=scan();table={x['id']:x for x in snapshot['directories']}
        chosen=[x for x in table.values() if x['automatic']] if automatic else [table[i] for i in ids]
        # Selecting both parent and child removes the parent only once.
        chosen=[x for x in chosen if not any(x is not y and below(x['path'],y['path']) for y in chosen)]
        if not chosen:print('没有可清理项目。');return []
        if any(not x['deletable'] for x in chosen):raise ValueError('包含使用中或必须保留的项目，已拒绝整批操作')
        confirm('将永久删除以下目录／文件：\n'+'\n'.join(x['path'] for x in chosen),yes)
        removed=[]
        for item in chosen:
            fresh={x['id']:x for x in scan()['directories']}.get(item['id'])
            if not fresh or not fresh['deletable'] or fresh['identity']!=item['identity']:raise ValueError('状态变化，请重新检查：'+item['path'])
            delete_item(item);removed.append(item['path'])
        if any(x['layout']=='服务' for x in chosen):run(['systemctl','daemon-reload'])
        for relative in ('plugins/recovery/deployments','plugins/recovery/operations','docker/runtime/code-backups','backups/privileged','backups/plugins','backups/ssh-bootstrap'):
            container=ROOT/relative
            try:
                if not container.is_symlink() and container.stat().st_uid==0:container.rmdir()
            except (FileNotFoundError,OSError):pass
        print(json.dumps({'removed':removed},ensure_ascii=False));return removed

def uninstall(plugin,user,yes=False):
    if plugin not in LABELS or not re.fullmatch(r'u[0-9]+',user):
        raise ValueError('invalid plugin/user')
    entry=Path('/data/minas/plugins/manager/cli.py')
    if not entry.is_file():raise ValueError('请先使用完整离线套件完成统一安装器切换')
    command=['/usr/bin/python3','-B',str(entry),'uninstall','--backend','--plugins',plugin,'--users',user]
    if yes:command.append('--yes')
    subprocess.run(command,check=True)


def restore(tool,transaction,yes=False):
    state=tool_status()[tool]
    if tool in ('network','docker'):
        choices=[t for t in state['transactions'] if t['phase'] in ('installed','confirmed','pending','rollback-failed')]
        if transaction:choices=[t for t in choices if t['id']==transaction]
        if len(choices)!=1:raise ValueError('请指定唯一事务编号 --transaction；可先 --scan 查看')
        selected=choices[0];base=Path(state['directory'])
        worker=base/('upgrade.py' if tool=='docker' else 'repair.py')
        if worker.is_symlink() or worker.stat().st_uid!=0 or worker.stat().st_mode&0o022:raise ValueError('恢复程序权限异常')
        confirm(('Docker 升级回退会恢复升级前镜像和容器层，保留当前本地卷数据；' if tool=='docker' else '网络恢复会撤销已确认修复并恢复原 UCI、防火墙及维护服务设置；')+'会短暂停止容器并重启 Docker。事务：'+selected['id'],yes)
        cmd=[sys.executable,'-B',worker,'--rollback' if tool=='docker' else '--restore',selected['id']]
        if tool=='docker':cmd+=['--yes']
        else:cmd+=['--yes']
        print(run(cmd,timeout=1800).stdout);return
    confirm('撤销 SSH 开机恢复 Cron、开机检查挂钩和本工具的 shell 绑定；有原始 root 账户备份时恢复该账户行。保留现用登录公钥、主机密钥和当前 SSH 连接。',yes)
    with operation_lock():
        for name,marker in (('/etc/cron.d/keep-ssh','/data/minas/ssh/restore-ssh.sh'),('/etc/systemd/system/minas.boot_check.service.d/90-keep-ssh.conf','ExecStartPost=/usr/bin/systemctl start dropbear.socket'),('/etc/systemd/system/dropbear@.service.d/90-normal-shell.conf','BindReadOnlyPaths=')):
            p=Path(name)
            if not p.exists():continue
            if p.is_symlink() or p.stat().st_uid!=0 or marker not in p.read_text():raise ValueError('配置不属于本工具，拒绝覆盖：'+name)
            p.unlink()
        backup=ROOT/'ssh/passwd.before-shell-repair'
        if backup.is_file():
            old=[x for x in backup.read_text().splitlines() if x.startswith('root:')]
            if len(old)!=1 or old[0].split(':')[2]!='0':raise ValueError('root 账户备份异常')
            passwd=Path('/etc/passwd');rows=passwd.read_text().splitlines()
            if len([x for x in rows if x.startswith('root:')])!=1:raise ValueError('当前 root 账户异常')
            atomic(passwd,'\n'.join(old[0] if x.startswith('root:') else x for x in rows)+'\n',stat.S_IMODE(passwd.stat().st_mode))
        run(['systemctl','daemon-reload']);run(['systemctl','reload','crond.service'])
        print('已撤销开机恢复；当前 SSH、登录公钥和主机密钥保留。')

def atomic(path,text,mode=0o600):
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:f.write(text);f.flush();os.fchmod(f.fileno(),mode);os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)

def display(data):
    print('\n已安装自定义插件：')
    for i,p in enumerate(data['plugins'],1):
        print(f"  P{i}  {p['user']}  {p['name']}  已安装")
        service=p.get('service')
        if service:
            runtime=service.get('runtime',{});active=runtime.get('ActiveState','unknown')
            label={'active':'运行中','inactive':'已停止','failed':'启动失败'}.get(active,'状态未知：'+active)
            print('      后台服务：'+label+'；'+('服务定义存在' if service['definitionExists'] else '服务定义已删除'))
        else:print('      页面/API 工具，无独立常驻服务')
        print('      原厂登记：'+('已启用' if p['enabled'] else '未启用')+'（独立登记标记）')
    print('\n脚本状态：')
    for key,t in data['tools'].items():print('  '+key+'  '+t['name']+'  '+('已安装' if t['installed'] else '已恢复／未安装')+'  '+', '.join(x['id']+':'+str(x['phase']) for x in t['transactions']))
    print('\n目录／文件（ID 用于选择删除；使用中项目不可删除）：')
    for x in data['directories']:
        print(f"  {x['id']}  [{x['layout']}] {x['status']}  {x['owner']}\n      {x['path']}")
        detail=x['reason'] or '；'.join(x['references'][:2])
        if detail:print('      '+detail)

def main(argv=None):
    parser=argparse.ArgumentParser(description='NAS 历史遗留检查清理、八插件卸载及三个脚本恢复；默认只读检查')
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--scan',action='store_true',help='检查目录、安装登记、服务及实际引用')
    group.add_argument('--delete',nargs='+',metavar='ID',help='删除扫描中的选定 ID；拒绝使用中项目')
    group.add_argument('--clean-residue',action='store_true',help='仅清理已知且确认未使用的部署／测试遗留')
    group.add_argument('--uninstall',choices=PLUGINS,help='卸载当前账号的自定义插件')
    group.add_argument('--restore',choices=('ssh','network','docker'),help='撤销对应脚本的更改')
    parser.add_argument('--user',help='卸载账号 u数字')
    parser.add_argument('--transaction',help='网络修复／Docker 升级事务编号')
    parser.add_argument('--yes',action='store_true',help='明确确认已列明的删除／卸载／恢复')
    parser.add_argument('--json',action='store_true',help='输出检查 JSON')
    args=parser.parse_args(argv)
    if os.geteuid()!=0:raise ValueError('请在 NAS 本机以 root 运行')
    if os.readlink('/proc/self/ns/mnt')!=os.readlink('/proc/1/ns/mnt'):
        fd=os.open('/proc/1/ns/mnt',os.O_RDONLY)
        try:
            if ctypes.CDLL(None,use_errno=True).setns(fd,0):raise OSError('无法进入宿主机挂载命名空间，停止检查或清理')
        finally:os.close(fd)
    if args.delete:return clean(args.delete,args.yes)
    if args.clean_residue:return clean([],args.yes,True)
    if args.uninstall:return uninstall(args.uninstall,args.user,args.yes)
    if args.restore:return restore(args.restore,args.transaction,args.yes)
    data=scan()
    if args.json:print(json.dumps(data,ensure_ascii=False,indent=2));return
    display(data)
    if args.scan or not sys.stdin.isatty():return
    print('\n1 检查刷新  2 选择删除  3 清理已确认遗留  4 卸载插件  5 恢复脚本  0 退出')
    choice=input('请选择：').strip()
    if choice=='1':return main(['--scan'])
    if choice=='2':return clean(input('输入目录 ID（空格分隔）：').split())
    if choice=='3':return clean([],automatic=True)
    if choice=='4':
        index=int(input('输入 P 后的插件序号：'))
        if not 1<=index<=len(data['plugins']):raise ValueError('无效插件序号')
        selected=data['plugins'][index-1]
        return uninstall(selected['plugin'],selected['user'])
    if choice=='5':return restore(input('输入 ssh / network / docker：').strip(),input('事务编号（SSH 留空）：').strip() or None)
    if choice not in ('0',''):raise ValueError('无效选项')

if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,IndexError,RuntimeError,subprocess.SubprocessError) as error:
        print('操作停止：'+str(error),file=sys.stderr);sys.exit(1)
