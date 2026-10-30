#!/usr/bin/python3
"""Install/remove shared component registrations. Called only by root installers."""
import argparse
import contextlib
import fcntl
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import privileged_runtime as runtime

BASE = runtime.BASE
HOME = Path('/home')
PLUGIN_DATA = Path('/data/plugin')
BACKUPS = Path('/data/minas/backups/privileged')
SUDOERS = Path('/etc/sudoers.d')
CRON = Path('/etc/cron.d')
CORE_FILES = ('minas-helper', 'component_admin.py', 'common/privileged_runtime.py', 'common/component_admin.py', 'common/user_state_io.py', 'common/telemetry_cache.py', 'common/event_wait.py', 'common/maintenance_events.py')
PRIVATE_FILES = {
 'dockermanager': ('docker-manager-helper',),
 'taskcenter': ('task-center-helper','task_schedule.py','task_maintenance.py'),
 'sshrecovery': ('ssh-recovery-helper','restore-ssh.sh'),
 'mihomo': ('api.py','mihomo-docker-proxy','docker_proxy_status.py'),
 'nascenter': ('api.py','nas-center-helper','process_metrics.py'),
 'mediaeditor': ('api.py','store.py','sqlite_native.py','provider.py'),
}



def atomic(path, data, mode=0o600):
    if isinstance(data, dict):
        data = json.dumps(data, ensure_ascii=False, indent=2).encode()
    if isinstance(data, str): data = data.encode()
    if path.is_symlink(): raise ValueError('拒绝覆盖符号链接：%s' % path)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data); os.fchmod(f.fileno(), mode); os.fchown(f.fileno(), 0, 0)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def version(value):
    if not re.fullmatch(r'\d+\.\d+\.\d+', value): raise ValueError('无效组件版本')
    return tuple(map(int, value.split('.')))


def validate_tree(directory):
    for path in [directory] + list(directory.rglob('*')):
        s = path.lstat()
        if s.st_uid != 0 or s.st_mode & 0o022 or not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode)):
            raise ValueError('调度数据包含链接、不安全权限或非 root 文件：%s' % path)


def check_idle():
    # The component exclusive lock already excludes current shared workers.
    for directory in [BASE]:
        users = directory / 'users'
        if not users.is_dir(): continue
        for file in users.glob('u*/state.json'):
            state = json.loads(file.read_text())
            for run in state.get('runs', []):
                if run.get('state') == 'running':
                    pid = run.get('pid')
                    if pid and Path('/proc/%s' % int(pid)).exists():
                        raise ValueError('存在执行中的定时任务，稍后再更新')
                    # Do not silently classify a just-reserved worker as dead.
                    import time
                    if time.time() - run.get('started', 0) < 90:
                        raise ValueError('存在刚提交的定时任务，稍后再更新')
    for file in [Path('/run/dockermanager-upgrade-status.json'), Path('/run/dockermanager-bulk-status.json')]:
        if not file.is_file(): continue
        data = json.loads(file.read_text())
        if data.get('state') == 'running' and data.get('pid') and Path('/proc/%s' % int(data['pid'])).exists():
            raise ValueError('Docker 正在升级或批量操作，稍后再安装')





def install_sudo(user, plugin):
    rule = '%s ALL=(root) NOPASSWD: %s/minas-helper %s\n' % (user, BASE, plugin)
    fd, temp = tempfile.mkstemp(prefix='.minas-sudo-', dir=str(SUDOERS))
    os.close(fd); temp = Path(temp)
    try:
        atomic(temp, rule, 0o440)
        subprocess.run(['/usr/sbin/visudo', '-cf', str(temp)], check=True, stdout=subprocess.DEVNULL)
        os.replace(str(temp), str(SUDOERS / (plugin + '-' + user)))
    finally:
        if temp.exists(): temp.unlink()





def update_cron(data):
    plugins = {p for grants in data['users'].values() for p in grants}
    file = CRON / 'minas-privileged-v2'
    if plugins:
        text = 'SHELL=/bin/sh\nPATH=/usr/sbin:/usr/bin:/sbin:/bin:/data/docker\nMAILTO=""\n'
        if 'dockermanager' in plugins:
            text += '@reboot root %s/minas-helper boot >/dev/null 2>&1\n' % BASE
        atomic(file, text, 0o644)
    elif file.exists(): file.unlink()
    install_event_service(plugins)



def install_event_service(plugins):
    if str(BASE) != '/data/minas/privileged': return  # isolated package tests
    unit='minas-maintenance-events.service'
    path=Path('/etc/systemd/system')/unit
    if not plugins.intersection({'taskcenter','sshrecovery'}):
        subprocess.run(['systemctl','disable','--now',unit],check=False,capture_output=True)
        path.unlink(missing_ok=True)
    else:
        content='[Unit]\nDescription=NAS task deadlines and SSH material expiry\nAfter=local-fs.target\n[Service]\nType=simple\nExecStart=/usr/bin/python3 -I -B '+str(BASE)+'/common/maintenance_events.py\nRestart=on-failure\nRestartSec=15\nUMask=0077\n[Install]\nWantedBy=multi-user.target\n'
        # -I excludes script directory: launch with a fixed, root-owned bootstrap.
        content=content.replace('-I -B','-B')
        atomic(path,content,0o644)
        subprocess.run(['systemctl','daemon-reload'],check=True)
        subprocess.run(['systemctl','enable',unit],check=True,capture_output=True)
        subprocess.run(['systemctl','restart',unit],check=True,capture_output=True)


def bundle(payload, plugin):
    import hashlib
    source = payload / 'installer/common'
    manifest = json.loads((source/'manifest.json').read_bytes())
    if manifest['version'] != runtime.VERSION or manifest['apiVersion'] != runtime.API_VERSION or manifest['plugin'] != plugin:
        raise ValueError('安装包公共组件版本或插件身份不一致')
    expected = set(CORE_FILES) | {'modules/'+plugin+'/'+name for name in PRIVATE_FILES.get(plugin, ())}
    if set(manifest['files']) != expected:
        raise ValueError('安装包必须只包含共有代码和当前插件私有代码')
    result = {}
    for name, checksum in manifest['files'].items():
        path = source/name
        if path.is_symlink() or not path.is_file() or path.resolve() != path:
            raise ValueError('安装包组件路径异常')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != checksum:
            raise ValueError('安装包组件摘要不匹配')
        result[name] = data
    return result


def install_code(relative, value):
    target = BASE/relative
    for directory in reversed(tuple(target.parent.relative_to(BASE).parents)):
        if str(directory) != '.': runtime.secure_directory(BASE/directory, create=True)
    runtime.secure_directory(target.parent, create=True)
    executable = target.name in ('minas-helper','component_admin.py','docker-manager-helper','task-center-helper','ssh-recovery-helper','nas-center-helper','mihomo-docker-proxy') or target.suffix == '.sh'
    atomic(target, value, 0o755 if executable else 0o644)





def ssh_state():
    return BASE/'modules/sshrecovery/var'


@contextlib.contextmanager
def ssh_state_lock(plugin):
    if plugin != 'sshrecovery':
        yield
        return
    directory = ssh_state()
    directories = [directory] if directory.exists() else []
    descriptors = []
    try:
        for node in directories:
            runtime.secure_directory(node)
            fd = os.open(str(node/'operations.lock'), os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
            descriptors.append(fd)
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o077:
                raise ValueError('SSH 恢复操作锁权限异常')
            if not any(os.fstat(other).st_ino==metadata.st_ino and os.fstat(other).st_dev==metadata.st_dev for other in descriptors[:-1]):
                fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:
        for fd in descriptors: os.close(fd)





def restore_tree(current, saved):
    """Restore a root-owned snapshot in place, never unlinking live locks."""
    if current.exists():
        validate_tree(current)
        for path in sorted(current.rglob('*'),key=lambda p:len(p.parts),reverse=True):
            previous=saved/path.relative_to(current)
            if path.is_file() and not previous.exists() and not path.name.endswith('.lock') and path.name!='lock':
                path.unlink()
            elif path.is_dir() and not previous.exists():
                try:path.rmdir()
                except OSError:pass
    if saved.exists():
        for path in [saved]+sorted(saved.rglob('*'),key=lambda p:len(p.parts)):
            target=current/path.relative_to(saved)
            if path.is_dir():
                target.mkdir(parents=True,exist_ok=True);target.chmod(stat.S_IMODE(path.stat().st_mode))
            elif path.is_file():atomic(target,path.read_bytes(),stat.S_IMODE(path.stat().st_mode))
    elif current.exists():
        try:current.rmdir()
        except OSError:pass








def install(payload, plugin, user, plugin_version, backup):
    values = bundle(payload, plugin)
    old = runtime.registry() if (BASE/'registry.json').exists() else {'apiVersion':1,'version':'0.0.0','users':{},'modules':{}}
    if old.get('apiVersion') != runtime.API_VERSION:
        raise ValueError('公共权限组件 API 不兼容，拒绝升级')
    if version(old.get('modules',{}).get(plugin,'0.0.0')) > version(plugin_version):
        raise ValueError('已安装更新的插件权限模块，拒绝降级')
    data = json.loads(json.dumps(old))
    for retired in ('quickshare','downloadcenter'):
        data.get('packages',{}).pop(retired,None)
    if version(old['version']) < version(runtime.VERSION):
        for name in CORE_FILES: install_code(name, values[name])
        data.update(version=runtime.VERSION, layoutVersion=runtime.LAYOUT_VERSION)
        # These are shared files, not any other plugin's private implementation.
        for name in ('privileged_runtime.py','user_state_io.py'):
            file = BASE/name
            if file.is_file() and not file.is_symlink(): file.unlink()
    elif version(old['version']) == version(runtime.VERSION):
        import hashlib
        for name in CORE_FILES:
            path = BASE/name
            if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(values[name]).digest():
                raise ValueError('同版本共有组件内容不一致，拒绝静默覆盖')
    for name in PRIVATE_FILES.get(plugin, ()):
        install_code('modules/'+plugin+'/'+name, values['modules/'+plugin+'/'+name])
    if plugin == 'sshrecovery':
        for relative in ('var','var/keys','var/host','var/tmp'):
            runtime.secure_directory(BASE/'modules/sshrecovery'/relative,create=True,mode=0o700)
    # Distributors without privileged operations receive no sudo grant/module.
    data.setdefault('packages',{})[plugin] = plugin_version
    if plugin in runtime.MODULES:
        data['users'].setdefault(user, [])
        if plugin not in data['users'][user]: data['users'][user].append(plugin)
        data.setdefault('modules',{})[plugin] = plugin_version
    if plugin == 'mihomo':
        text = (HOME/user/'plugin/mihomo/etc/ports.env').read_text()
        match = re.search(r'^MIXED_PORT=([0-9]+)$',text,re.M)
        if not match or not 1024 <= int(match[1]) <= 65535: raise ValueError('Mihomo 端口未配置')
        data.setdefault('settings',{}).setdefault(user,{})['mihomo'] = {'proxyPort':int(match[1])}
    atomic(BASE/'registry.json', data)
    with contextlib.nullcontext():
        if plugin == 'taskcenter':
            sys.path.insert(0,str(BASE/'modules/taskcenter'))
            loader=importlib.machinery.SourceFileLoader('install_tasks',str(BASE/'modules/taskcenter/task-center-helper'))
            spec=importlib.util.spec_from_loader(loader.name,loader);module=importlib.util.module_from_spec(spec);loader.exec_module(module)
            if not (BASE/'users'/user/'state.json').is_file() or 'taskcenter' not in old.get('users',{}).get(user,[]):
                module.initialize(user)
            state=json.loads((BASE/'users'/user/'state.json').read_text())
            for task in state['tasks']: module.validate_task(task)
        if plugin in runtime.MODULES: install_sudo(user,plugin)
    obsolete = CRON/('dockermanager-'+user)
    if plugin == 'dockermanager' and obsolete.exists(): obsolete.unlink()
    update_cron(data)
    return data


def remove(plugin, user, backup):
    data = runtime.registry()
    if plugin in ('taskcenter','nascenter'):
        from user_state_io import release_memory_cache
        release_memory_cache(user,plugin)
    if plugin == 'sshrecovery':
        key = ssh_state() / 'keys' / (user + '.pub')
        if key.exists(): key.unlink()
    grants = data['users'].get(user, [])
    if plugin == 'taskcenter':
        target = BASE / 'users' / user
        if target.exists():
            validate_tree(target)
            shutil.copytree(str(target), str(backup / 'removed-state'))
            # Exact validated single-user state, never the user's files or containers.
            shutil.rmtree(str(target))
    if plugin in grants: grants.remove(plugin)
    if not grants: data['users'].pop(user, None)
    if plugin == 'sshrecovery' and not any('sshrecovery' in g for g in data['users'].values()):
        state = ssh_state()
        if state.exists():
            validate_tree(state)
            shutil.rmtree(str(state))
    if plugin == 'mihomo':
        data.get('settings',{}).get(user,{}).pop('mihomo',None)
    if not any(plugin in grants for grants in data['users'].values()):
        data.get('modules',{}).pop(plugin,None)
        data.get('packages',{}).pop(plugin,None)
        module = BASE/'modules'/plugin
        if module.is_dir():
            validate_tree(module);shutil.rmtree(module)
    atomic(BASE / 'registry.json', data)
    rule = SUDOERS / (plugin + '-' + user)
    if rule.exists(): rule.unlink()
    update_cron(data)
    # Keep stable shared entry points for other distributors and future installs.
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install', 'remove', 'check'])
    parser.add_argument('--plugin', choices=sorted(runtime.DISTRIBUTORS), required=True)
    parser.add_argument('--user', required=True)
    parser.add_argument('--payload', type=Path)
    parser.add_argument('--plugin-version', default='0.0.0')
    args = parser.parse_args()
    if os.geteuid() != 0 or os.environ.get('SUDO_USER') not in (None, 'root'):
        raise ValueError('组件管理仅允许 root 安装器调用')
    runtime.account(args.user)
    if str(BASE)=='/data/minas/privileged':runtime.ensure_data_layout()
    runtime.secure_directory(BASE, create=True)

    with runtime.lock('component.lock', exclusive=True, wait=15), ssh_state_lock(args.plugin):
        check_idle()
        if args.action == 'check':
            print(json.dumps({'ok':True,'helperVersion':runtime.VERSION}))
            return
        if args.action == 'remove':
            runtime.secure_directory(BACKUPS,create=True,mode=0o700)
            backup=Path(tempfile.mkdtemp(prefix='removed-'+args.plugin+'-',dir=str(BACKUPS)))
        else: backup = Path(tempfile.mkdtemp(prefix='minas-component-'))
        saved = backup / 'component'
        shutil.copytree(str(BASE), str(saved), ignore=shutil.ignore_patterns('*.lock','backups'))
        rule = SUDOERS / (args.plugin + '-' + args.user)
        if rule.exists(): shutil.copy2(str(rule), str(backup / 'sudoers'))
        for file in CRON.glob('*'):
            if file.is_file() and file.name in {'minas-privileged', 'minas-privileged-v2', 'taskcenter', 'dockermanager-' + args.user}:
                shutil.copy2(str(file), str(backup / ('cron-' + file.name)))
        try:
            data = install(args.payload, args.plugin, args.user, args.plugin_version, backup) if args.action == 'install' else remove(args.plugin, args.user, backup)
        except BaseException:
            # Restore grouped code and protected state, preserving live lock inodes.
            for name in ('common','modules','users'):
                restore_tree(BASE/name,saved/name)
            for file in BASE.iterdir():
                if file.is_file() and file.name not in {'component.lock','docker.lock'} and not (saved/file.name).exists(): file.unlink()
            for file in saved.iterdir():
                if file.is_file():atomic(BASE/file.name,file.read_bytes(),file.stat().st_mode&0o777)
            if (backup / 'sudoers').exists(): atomic(rule, (backup / 'sudoers').read_bytes(), 0o440)
            elif rule.exists(): rule.unlink()
            for name in ['minas-privileged', 'minas-privileged-v2', 'taskcenter', 'dockermanager-' + args.user]:
                saved_cron = backup / ('cron-' + name)
                if saved_cron.exists(): atomic(CRON / name, saved_cron.read_bytes(), 0o644)
                elif (CRON / name).exists(): (CRON / name).unlink()
            install_event_service({p for g in runtime.registry().get('users',{}).values() for p in g})
            raise
        finally:
            if args.action == 'install': shutil.rmtree(backup)
    subprocess.run(['/usr/bin/systemctl', 'reload', 'crond.service'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(json.dumps({'ok': True, 'helperVersion': data['version'], 'backup': str(backup) if args.action == 'remove' else None, 'registeredUsers': len(data['users'])}, ensure_ascii=False))


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print('公共权限组件安装/卸载失败：%s' % error, file=sys.stderr)
        sys.exit(1)
