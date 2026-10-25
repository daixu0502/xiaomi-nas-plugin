"""Small standalone layout primitives; generated constants select one owner."""
DATA_LAYOUT_VERSION = 1
DATA_LAYOUT_ROOT = '/data/minas'
DATA_LAYOUT_SCOPE = 'plugin-mediaeditor'
DATA_LAYOUT_PATHS = {}


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
    if not root.exists():root.mkdir(mode=0o755)
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
    # Shared runtimes initialize their own parents; migration is installer-owned.
    # No plugin, SSH or Docker migration archive is embedded here.
    _layout_initialize()


def remap_data_path(value):
    import os
    from pathlib import Path
    path=Path(value)
    if not path.is_absolute() or '..' in path.parts:raise ValueError('invalid historical path')
    for old,new in DATA_LAYOUT_PATHS.items():
        if path==Path(old) or path.is_relative_to(old):
            target=Path(new)/path.relative_to(old)
            if os.path.lexists(target):
                _layout_secure(DATA_LAYOUT_ROOT)
                if os.path.lexists(path):raise ValueError('conflicting historical file: '+str(path))
                return target
    return path


def rebase_data_text(text):
    for old,new in sorted(DATA_LAYOUT_PATHS.items(),key=lambda pair:len(pair[0]),reverse=True):text=text.replace(old,new)
    return text


"""Finalize only the current user's plugin after new source/recovery verification."""
DATA_LAYOUT_PLUGIN = 'mediaeditor'
DATA_LAYOUT_PRIVATE_FILES = ['api.py', 'store.py', 'sqlite_native.py', 'provider.py']
DATA_LAYOUT_FLAT_FILES = []

def _layout_empty_parents(path, stop):
    from pathlib import Path
    path, stop = (Path(path), Path(stop))
    while path != stop and path.is_relative_to(stop):
        try:
            path.rmdir()
        except OSError:
            break
        path = path.parent

def _layout_remove_record(path):
    import shutil, stat
    from pathlib import Path
    path = Path(path)
    if not path.exists():
        return
    _layout_secure(path)
    for node in path.rglob('*'):
        s = node.lstat()
        if not stat.S_ISLNK(s.st_mode) and s.st_uid != 0:
            raise ValueError('unsafe legacy recovery record')
    shutil.rmtree(path)

def _layout_retire_public(base, known, refs):
    """Only known public files are disposable after every private owner left."""
    import os, stat
    from pathlib import Path
    base = Path(base)
    if refs or not base.exists():
        return False
    _layout_secure(base)
    for proc in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            args = proc.read_bytes().split(b'\x00')
        except OSError:
            continue
        if any((arg.startswith(os.fsencode(base) + b'/') for arg in args)):
            return False
    remaining = []
    for parent, dirs, files in os.walk(base, followlinks=False):
        for name in files:
            p = Path(parent) / name
            rel = p.relative_to(base).as_posix()
            s = p.lstat()
            if rel not in known or not stat.S_ISREG(s.st_mode) or s.st_uid != 0:
                remaining.append(rel)
        for name in dirs:
            if (Path(parent) / name).is_symlink():
                remaining.append(str(Path(parent) / name))
    if remaining:
        return False
    for rel in known:
        p = base / rel
        if p.is_file():
            p.unlink()
    for p in sorted(base.rglob('*'), key=lambda p: len(p.parts), reverse=True):
        if p.is_dir():
            try:
                p.rmdir()
            except OSError:
                pass
    try:
        base.rmdir()
        return True
    except OSError:
        return False

def finish_plugin_layout(user):
    import fcntl, hashlib, json, os, re, stat, time
    from pathlib import Path
    plugin = DATA_LAYOUT_PLUGIN
    if not re.fullmatch('u[0-9]+', user):
        raise ValueError('invalid migration user')
    root = Path(DATA_LAYOUT_ROOT)
    receipts = {}
    receipt_dir = root / 'privileged/migration' / plugin
    for receipt in receipt_dir.glob('u*.json'):
        _layout_secure(receipt.parent)
        receipts.update(json.loads(receipt.read_text()).get('files', {}))
    record = root / 'plugins/recovery/records' / (user + '-' + plugin)
    _layout_secure(record)
    current = (record / 'current').read_text().strip()
    if not re.fullmatch('generation-[0-9]+', current):
        raise ValueError('invalid new recovery generation')
    saved = record / current
    value = json.loads((saved / 'record.json').read_text())
    if value.get('user') != user or value.get('plugin') != plugin:
        raise ValueError('new recovery identity mismatch')
    home = Path('/home') / user / 'plugin' / plugin

    def digest(src):
        out = hashlib.sha256()
        files = []
        for parent, dirs, names in os.walk(src, followlinks=False):
            files.extend((Path(parent) / name for name in names if stat.S_ISREG((Path(parent) / name).lstat().st_mode)))
        for p in sorted(files, key=lambda p: os.fsencode(str(p))):
            out.update((hashlib.sha256(p.read_bytes()).hexdigest() + '\n').encode())
        return out.hexdigest()
    info = json.loads((home / 'INFO').read_text())
    if digest(home / 'src') != info['abstract'] or digest(saved / 'src') != info['abstract']:
        raise ValueError('new source/recovery not verified; legacy files retained')
    _layout_remove_record(Path('/data/.minas-plugin-recovery/records') / (user + '-' + plugin))
    old = Path('/data/.minas-privileged')
    if old.exists():
        _layout_secure(old)
        lock = old / 'component.lock'
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 384)
        try:
            s = os.fstat(fd)
            if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_mode & 18:
                raise ValueError('unsafe old component lock')
            deadline = time.monotonic() + 15
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ValueError('旧组件忙，当前插件已安装；保留旧文件以便下次清理')
                    time.sleep(0.05)
            registry = old / 'registry.json'
            data = json.loads(registry.read_text()) if registry.is_file() else dict(users={}, modules={}, packages={})
            grants = data.get('users', {}).get(user, [])
            data.setdefault('users', {})[user] = [p for p in grants if p != plugin]
            if not data['users'][user]:
                data['users'].pop(user, None)
            data.get('settings', {}).get(user, {}).pop(plugin, None)
            remaining = any((plugin in grants for grants in data.get('users', {}).values()))
            if not remaining:
                data.get('modules', {}).pop(plugin, None)
                data.get('packages', {}).pop(plugin, None)
            if registry.exists():
                _layout_atomic(registry, json.dumps(data, ensure_ascii=False, indent=2) + '\n')
            if not remaining:
                for name in DATA_LAYOUT_PRIVATE_FILES:
                    p = old / 'modules' / plugin / name
                    if p.is_file() and (not p.is_symlink()):
                        p.unlink()
                for name in DATA_LAYOUT_FLAT_FILES:
                    p = old / name
                    if p.is_file() and (not p.is_symlink()):
                        p.unlink()
                _layout_empty_parents(old / 'modules' / plugin, old)
                cron = Path('/etc/cron.d/minas-privileged')
                if cron.is_file():
                    previous = cron.read_text()
                    lines = []
                    for line in previous.splitlines(keepends=True):
                        own = str(old / 'minas-helper') + ' ' + plugin + ' ' in line
                        own = own or any((str(old / name) in line for name in DATA_LAYOUT_FLAT_FILES))
                        if not own:
                            lines.append(line)
                    if ''.join(lines) != previous:
                        _layout_atomic(cron, ''.join(lines), 420)
        finally:
            os.close(fd)
        public = {'minas-helper', 'component_admin.py', 'privileged_runtime.py', 'user_state_io.py', 'common/privileged_runtime.py', 'common/component_admin.py', 'common/user_state_io.py', 'common/telemetry_cache.py', 'registry.json', 'component.lock', 'docker.lock'}
        refs = bool(data.get('users') or data.get('modules') or data.get('packages'))
        _layout_retire_public(old, public, refs)
    legacy_data = Path('/data/.minas-plugin-data')
    _layout_empty_parents(legacy_data / user / plugin, legacy_data)
    try:
        legacy_data.rmdir()
    except OSError:
        pass
    recovery = Path('/data/.minas-plugin-recovery')
    if recovery.exists():
        _layout_remove_record(recovery / 'records' / (user + '-' + plugin))
        _layout_empty_parents(recovery / 'records', recovery)
        if not (recovery / 'records').exists():
            import subprocess
            for name in ('minas-plugin-recovery-watch.service', 'minas-plugin-recovery.service', 'minas-plugin-recovery.timer'):
                unit = Path('/etc/systemd/system') / name
                if unit.is_file() and str(recovery) in unit.read_text():
                    subprocess.run(['systemctl', 'disable', '--now', name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    unit.unlink()
            cron = Path('/etc/cron.d/minas-plugin-recovery')
            if cron.is_file() and str(recovery) in cron.read_text():
                cron.unlink()
            _layout_retire_public(recovery, {'recovery.py', 'lock', 'recovery.lock', 'watch.lock'}, False)
    receipt = receipt_dir / (user + '.json')
    if receipt.is_file():
        value = json.loads(receipt.read_text())
        value['activated'] = True
        _layout_atomic(receipt, json.dumps(value, indent=2) + '\n')
    print('已完成当前插件迁移；其他使用者和无法确认用途的旧文件已保留：' + user + '/' + plugin)

def abort_plugin_layout(user):
    return

if __name__=='__main__':
 import argparse
 p=argparse.ArgumentParser();p.add_argument('--finish',action='store_true');p.add_argument('--abort',action='store_true');p.add_argument('--user');a=p.parse_args()
 ensure_data_layout()
 if a.finish:finish_plugin_layout(a.user)
 if a.abort:abort_plugin_layout(a.user)
