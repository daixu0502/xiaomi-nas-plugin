"""Small fixed plugin differences; no shared component initialization."""
import json
import importlib
import ctypes
import ctypes.util
from pathlib import Path
import re
import secrets
import subprocess
from layout import COMPONENT, DATA, ENV, PRIVILEGED, require

PORTS = {
    'mihomo': [('etc/ports.env', 'MIXED_PORT', 7890, 7989), ('etc/ports.env', 'CONTROLLER_PORT', 9090, 9189)],
    'quickshare': [('var/server.port', '', 19092, 19191)],
    'downloadcenter': [('var/rpc.port', '', 19300, 19399), ('var/peer.port', '', 19400, 19499),
                       ('var/qb.port', '', 19500, 19599), ('var/aria-peer.port', '', 19600, 19699),
                       ('var/openlist.port', '', 19700, 19799)],
}
DEPENDENCIES = {'dockermanager': ('/data/docker/docker',), 'taskcenter': ('/sbin/shutdown',),
                'nascenter': ('smartctl', 'lsblk'), 'mihomo': ('curl','logger','sed','head','tr','wc')}


def preflight_dependencies(plugin):
    for name in ('ssl', 'sqlite3') if plugin in ('downloadcenter','mediaeditor') else ('ssl',):
        try:
            importlib.import_module(name)
        except ImportError as error:
            raise ValueError('NAS Python 缺少模块：' + name) from error
    if plugin == 'mediaeditor':
        require(Path('/usr/lib/libmitrigram.so').is_file(), '缺少原厂影视分词扩展：/usr/lib/libmitrigram.so')
        try:
            ctypes.CDLL(ctypes.util.find_library('sqlite3') or 'libsqlite3.so.0')
        except OSError as error:
            raise ValueError('NAS 缺少 SQLite 运行库') from error
INITIAL_FILES = {
    'mihomo': ('etc/config.yaml', 'etc/api.secret', 'etc/ports.env'),
    'quickshare': ('var/server.port', 'var/server.secret', 'var/shares.json'),
    'downloadcenter': ('var/rpc.port', 'var/peer.port', 'var/qb.port', 'var/aria-peer.port',
                       'var/openlist.port', 'var/rpc.secret', 'var/enabled'),
}
USER_INIT = r'''
import json,os,sys,tempfile
from pathlib import Path
request=json.load(sys.stdin);base=Path(request['base'])
for name,value in request['files'].items():
 p=base/name
 if any(n.is_symlink() for n in (p,*p.parents)):raise ValueError('redirected user state')
 p.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
 if p.exists():
  if not p.is_file():raise ValueError('state is not a file')
  continue
 fd,t=tempfile.mkstemp(prefix='.'+p.name+'-',dir=p.parent)
 try:
  with os.fdopen(fd,'wb') as f:
   f.write(value.encode());os.fchmod(f.fileno(),0o600);f.flush();os.fsync(f.fileno())
  os.replace(t,p)
  directory=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
  try:os.fsync(directory)
  finally:os.close(directory)
 finally:
  if os.path.exists(t):os.unlink(t)
'''


def run_user(user, code, data=None):
    return subprocess.run(['runuser', '-u', user, '--', '/usr/bin/python3', '-I', '-B', '-c', code],
                          input=json.dumps(data).encode() if data is not None else None,
                          env=ENV, check=True, capture_output=True, timeout=30).stdout


def state_text(user, path):
    code = ('from pathlib import Path;p=Path(' + repr(str(path)) + ');'
            'assert not any(x.is_symlink() for x in (p,*p.parents));'
            'print(p.read_text() if p.exists() else "",end="")')
    return run_user(user, code).decode()


def source_digest(user, path):
    code = '''import hashlib,os,stat,sys
from pathlib import Path
base=Path(sys.argv[1]);files=[]
def failed(error):raise error
for parent,dirs,names in os.walk(base,followlinks=False,onerror=failed):
 for name in names:
  p=Path(parent)/name
  if stat.S_ISREG(p.lstat().st_mode):files.append(p)
result=hashlib.sha256()
for p in sorted(files,key=lambda p:os.fsencode(str(p))):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 result.update(h.hexdigest().encode()+bytes([10]))
print(result.hexdigest())
'''
    return subprocess.run(['runuser','-u',user,'--','/usr/bin/python3','-I','-B','-c',code,str(path)],
                          env=ENV,check=True,capture_output=True,timeout=120).stdout.decode().strip()


def ports(user, plugin, used):
    values, files = {}, {}
    for name, key, low, high in PORTS.get(plugin, ()):
        text = state_text(user, DATA / user / plugin / name)
        match = re.search(r'^' + re.escape(key) + r'=([0-9]+)$', text, re.M) if key else None
        number = int(match[1]) if match else int(text.strip()) if not key and text.strip().isdigit() else None
        if text:
            require(number is not None and 1024 <= number <= 65535, '已保存端口配置无效：' + name)
            require(number not in used, '已有端口被占用：' + str(number))
        else:
            number = next((p for p in range(low, high + 1) if p not in used), None)
        require(number is not None, '没有可用端口：' + str(low) + '-' + str(high))
        used.add(number)
        values[key or name] = number
        files[name] = files.get(name, '') + ((key + '=') if key else '') + str(number) + '\n'
    return values, files


def reserved_ports(user, plugin):
    used = set()
    for account in DATA.glob('u[0-9]*'):
        if not re.fullmatch('u[0-9]+', account.name):
            continue
        for other, definitions in PORTS.items():
            if (account.name, other) == (user, plugin):
                continue
            for relative, key, low, high in definitions:
                path = account / other / relative
                if not path.exists():
                    continue
                text = state_text(account.name, path)
                match = re.search(r'^' + re.escape(key) + r'=([0-9]+)$', text, re.M) if key else None
                value = match[1] if match else text.strip() if not key else ''
                require(value.isdigit() and 1024 <= int(value) <= 65535, '其他账号端口配置无效：' + str(path))
                used.add(int(value))
    return used


def initialize(user, plugin, payload, used):
    values, files = ports(user, plugin, used | reserved_ports(user, plugin))
    if plugin == 'mihomo':
        config = (payload / 'etc/config.yaml').read_text()
        config = re.sub(r'^mixed-port:.*$', 'mixed-port: ' + str(values['MIXED_PORT']), config, flags=re.M)
        files.update({'etc/config.yaml': config, 'etc/api.secret': secrets.token_hex(32) + '\n'})
    elif plugin == 'quickshare':
        files.update({'var/server.secret': secrets.token_hex(32),
                      'var/shares.json': json.dumps({'version': 1, 'settings': {'external_base': ''}, 'shares': []})})
    elif plugin == 'downloadcenter':
        files['var/rpc.secret'] = secrets.token_hex(32)
    run_user(user, USER_INIT, {'base': str(DATA / user / plugin), 'files': files})
    return str(values.get('CONTROLLER_PORT', values.get('var/rpc.port', values.get('var/server.port', ''))))


def health(user, plugin, home):
    if plugin in ('dockermanager', 'taskcenter', 'nascenter'):
        action = {'dockermanager': 'system_summary', 'taskcenter': 'list', 'nascenter': 'overview'}[plugin]
        result = subprocess.run(['runuser', '-u', user, '--', 'sudo', '-n',
                                 str(COMPONENT / 'minas-helper'), plugin],
                                input=json.dumps({'action': action, 'pluginUser': user}).encode(),
                                env=ENV, check=True, capture_output=True, timeout=60)
        require(json.loads(result.stdout).get('ok') is True, plugin + ' 权限接口自检失败')
    binaries = {'mihomo': [('mihomo', '-v')],
                'downloadcenter': [('aria2c', '--version'), ('qbittorrent-nox', '--version')]}
    for name, flag in binaries.get(plugin, ()):
        subprocess.run(['runuser', '-u', user, '--', str(home / 'src/files' / name), flag],
                       env=ENV, check=True, capture_output=True, timeout=20)
