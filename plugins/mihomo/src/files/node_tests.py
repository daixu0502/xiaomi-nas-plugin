"""Bounded user-owned node probes; speed tests use a separate disposable core."""
import base64
import concurrent.futures
import contextlib
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError, URLError
from plugin_paths import runtime_directory

DELAY_URL = 'https://cp.cloudflare.com/generate_204'
DOWNLOAD_HOST = 'speed.cloudflare.com'
MAX_BYTES = 20 * 1024 * 1024
MAX_SECONDS = 8
BUILTINS = {'direct', 'reject', 'rejectdrop', 'pass', 'passrule', 'compatible', 'dns'}


def child_setup():
    # Linux: the isolated core must not outlive a killed CGI worker.
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, 15, 0, 0, 0) != 0 or os.getppid() == 1: os._exit(1)


def atomic(path, data):
    fd, name = tempfile.mkstemp(prefix='.node-test-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(data, out, ensure_ascii=False); out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


class Tests:
    def __init__(self, home):
        if os.geteuid() == 0: raise RuntimeError('节点测速必须以插件所属用户运行')
        self.home = Path(home)
        if str(self.home).startswith('/home/'):
            match = re.fullmatch(r'/home/(u[0-9]+)/plugin/mihomo', str(self.home))
            if not match or pwd.getpwnam(match[1]).pw_uid != os.geteuid():
                raise RuntimeError('插件用户不匹配')
        self.runtime = runtime_directory(self.home)
        self.path = self.runtime / 'node-test.json'
        self.results = self.runtime / 'node-test-results.json'
        self.cancel_path = self.runtime / 'node-test-cancel.json'

    @contextlib.contextmanager
    def lock(self, name, shared=False):
        fd = os.open(self.runtime / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            yield fd
        except BlockingIOError:
            raise RuntimeError('已有测速、更新或配置任务正在执行，请稍后重试') from None
        finally: os.close(fd)

    def controller(self, method, path, body=None, port=None, secret=None, timeout=8):
        if port is None:
            text = (self.home / 'etc/ports.env').read_text()
            found = re.search(r'^CONTROLLER_PORT=([0-9]+)$', text, re.M)
            if not found: raise RuntimeError('内核控制端口未配置')
            port = int(found[1]); secret = (self.home / 'etc/api.secret').read_text().strip()
        if not 1 <= port <= 65535: raise RuntimeError('内核控制端口无效')
        req = Request('http://127.0.0.1:%d%s' % (port, path), method=method,
                      headers={'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/json'},
                      data=None if body is None else json.dumps(body).encode())
        with build_opener(ProxyHandler({})).open(req, timeout=timeout) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024: raise RuntimeError('节点数据过大')
            return json.loads(raw) if raw else {}

    def source(self, node):
        provider = node.get('_provider') or node.get('provider-name', '')
        relative = {'APP-SUBSCRIPTION': 'providers/app-subscription.yaml',
                    'APP-MANUAL': 'providers/app-manual.yaml',
                    'default': 'config.yaml', '': 'config.yaml'}.get(provider)
        if node.get('dialer-proxy') or relative is None: return None
        path = self.home / 'etc' / relative
        base = (self.home / 'etc').resolve(strict=True)
        # etc is the installed persistent link; never follow links below it.
        current = base
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink(): return None
        return path if current.is_file() else None

    def nodes(self):
        proxies = self.controller('GET', '/proxies').get('proxies', {})
        providers = self.controller('GET', '/providers/proxies').get('providers', {})
        result = {}
        def add(name, node, provider=None):
            if not isinstance(name, str) or not isinstance(node, dict) or 'all' in node or str(node.get('type', '')).lower() in BUILTINS: return
            key = hashlib.sha256(json.dumps([provider, name], ensure_ascii=False).encode()).hexdigest()
            result[key] = dict(node, _name=name, _provider=provider)
        for name, node in proxies.items(): add(name, node)
        for provider, value in providers.items():
            if value.get('vehicleType') not in ('File', 'HTTP'): continue
            for node in value.get('proxies', []): add(node.get('name'), node, provider)
        return result

    @staticmethod
    def read(path, default):
        try:
            if path.is_symlink() or path.stat().st_size > 1024 * 1024: return default
            return json.loads(path.read_text())
        except (OSError, ValueError): return default

    def status(self):
        job = self.read(self.path, None)
        if job and job['status'] in ('queued', 'running'):
            try:
                with self.lock('node-test.lock'):
                    job = self.read(self.path, None)
                    if job and job['status'] in ('queued', 'running'):
                        job.update(status='failed', message='测速进程已结束，可重新测试')
                        atomic(self.path, job)
            except RuntimeError: pass
        return {'ok': True, 'job': job, 'results': self.read(self.results, {})}

    def listing(self):
        nodes = self.nodes(); results = self.read(self.results, {})
        # Retire stale names from the disposable result cache on manual refresh.
        results = {name: row for name, row in results.items() if name in nodes}
        return {'ok': True, 'nodes': [{'key': name, 'name': node.get('_name', name), 'provider': node.get('_provider') or '配置内节点', 'type': node.get('type', ''),
            'speedSupported': self.source(node) is not None,
            'speedHint': '独立下载测速支持插件导入的订阅、手动节点及配置内节点；暂不支持链式代理或自定义提供者'}
            for name, node in list(nodes.items())[:200]], 'partial': len(nodes) > 200,
            'total': len(nodes), 'results': results}

    def start(self, data):
        kind = data.get('kind'); names = data.get('names')
        if kind not in ('delay', 'speed') or not isinstance(names, list) or not 1 <= len(names) <= (200 if kind == 'delay' else 1):
            raise RuntimeError('延迟最多测试 200 个节点；下载测速每次选择一个节点')
        if any(not isinstance(n, str) or not n or len(n) > 256 for n in names) or len(set(names)) != len(names):
            raise RuntimeError('节点名称无效或重复')
        with self.lock('node-test.lock') as job_fd, self.lock('update-operation.lock', True) as update_fd:
            nodes = self.nodes()
            if any(n not in nodes for n in names): raise RuntimeError('节点已改变，请刷新节点列表')
            if kind == 'speed' and self.source(nodes[names[0]]) is None:
                raise RuntimeError('该节点暂无独立测速来源，请使用插件导入的订阅或手动节点；链式代理暂不支持')
            job = {'id': secrets.token_hex(16), 'kind': kind, 'names': names, 'status': 'queued',
                   'done': 0, 'total': len(names), 'message': '正在准备测试', 'created': time.time()}
            atomic(self.path, job)
            env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'LC_ALL')}
            env.update(PLUG_HOME_DIR=str(self.home), PYTHONDONTWRITEBYTECODE='1')
            try:
                subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), 'worker', job['id']],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, close_fds=True, pass_fds=(job_fd, update_fd), env=env)
            except OSError:
                job.update(status='failed', message='无法启动测速进程'); atomic(self.path, job)
                raise RuntimeError('无法启动测速进程') from None
        return {'ok': True, 'job': job}

    def cancelled(self, ident):
        return self.read(self.cancel_path, {}).get('id') == ident

    def cancel(self, data):
        job = self.read(self.path, None)
        if not job or data.get('id') != job['id']: raise RuntimeError('测速任务已改变，请刷新')
        if job['status'] in ('queued', 'running'): atomic(self.cancel_path, {'id': job['id']})
        return {'ok': True}

    def delay(self, name, node=None):
        node = node or self.nodes().get(name)
        if node is None: raise RuntimeError('节点已改变，请刷新')
        actual = node.get('_name', name); provider = node.get('_provider')
        query = urlencode({'url': DELAY_URL, 'timeout': 5000, 'expected': '204'})
        path = ('/providers/proxies/' + quote(provider, safe='') + '/' + quote(actual, safe='') + '/healthcheck') if provider else ('/proxies/' + quote(actual, safe='') + '/delay')
        value = self.controller('GET', path + '?' + query)
        delay = value.get('delay')
        if type(delay) is not int or not 0 < delay <= 65535: raise RuntimeError('延迟测试未返回有效结果')
        return {'delayMs': delay, 'testedAt': time.time()}

    def speed(self, name, ident, progress):
        node = self.nodes().get(name)
        source = self.source(node) if node else None
        if source is None: raise RuntimeError('节点来源已改变，请刷新后重试')
        name = node.get('_name', name)
        binary = self.home / 'var/core/mihomo'
        if not binary.is_file(): binary = self.home / 'src/files/mihomo'
        with tempfile.TemporaryDirectory(prefix='node-speed-', dir=self.runtime) as folder:
            work = Path(folder); secret = secrets.token_hex(24)
            # Reserve both ephemeral ports together, then authenticate the child
            # before sending requests. No main-core selectors/config are changed.
            sockets = [socket.socket(), socket.socket()]
            try:
                for s in sockets: s.bind(('127.0.0.1', 0))
                mixed, control = [s.getsockname()[1] for s in sockets]
            finally:
                for s in sockets: s.close()
            group = '__MINAS_TEST_' + ident
            pattern = '^' + re.sub(r'([\\.+*?()|\[\]{}^$])', r'\\\1', name) + '$'
            config = {'mixed-port': mixed, 'bind-address': '127.0.0.1', 'allow-lan': False,
                'external-controller': '127.0.0.1:%d' % control, 'secret': secret,
                'authentication': ['test:' + secret], 'mode': 'rule', 'log-level': 'silent',
                'ipv6': True, 'dns': {'enable': False}, 'profile': {'store-selected': False},
                'proxy-providers': {'test-source': {'type': 'file', 'path': str(source.resolve()),
                    'filter': pattern, 'health-check': {'enable': False}}},
                'proxy-groups': [{'name': group, 'type': 'select', 'use': ['test-source']}],
                'rules': ['MATCH,' + group]}
            # This disposable config has no node passwords; the child only reads
            # the existing persistent provider through its normal Mihomo parser.
            config_path = work / 'config.json'; atomic(config_path, config)
            env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': str(work),
                   'SAFE_PATHS': str((self.home / 'etc').resolve()), 'GOMAXPROCS': '2', 'GOMEMLIMIT': '128MiB'}
            child = subprocess.Popen([str(binary), '-d', str(work), '-f', str(config_path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                preexec_fn=child_setup)
            try:
                deadline = time.monotonic() + 6
                while True:
                    if self.cancelled(ident): raise RuntimeError('已取消')
                    if child.poll() is not None: raise RuntimeError('独立测速内核启动失败，该节点配置可能依赖其他节点或文件')
                    try:
                        value = self.controller('GET', '/proxies/' + quote(group, safe=''), port=control, secret=secret, timeout=.5)
                        if value.get('all') != [name]: raise RuntimeError('独立内核未能精确匹配节点，已停止测速')
                        break
                    except (URLError, OSError, HTTPError):
                        if time.monotonic() >= deadline: raise RuntimeError('独立测速内核启动超时') from None
                        time.sleep(.1)
                progress(0)
                auth = base64.b64encode(('test:' + secret).encode()).decode()
                conn = http.client.HTTPSConnection('127.0.0.1', mixed, timeout=3, context=ssl.create_default_context())
                try:
                    conn.set_tunnel(DOWNLOAD_HOST, 443, {'Proxy-Authorization': 'Basic ' + auth})
                    conn.request('GET', '/__down?bytes=%d' % MAX_BYTES,
                                 headers={'Host': DOWNLOAD_HOST, 'Accept-Encoding': 'identity', 'Cache-Control': 'no-cache'})
                    response = conn.getresponse()
                    if response.status != 200 or response.getheader('Content-Length') != str(MAX_BYTES):
                        raise RuntimeError('测速服务返回异常，未计为有效速度')
                    start = last = time.monotonic(); received = 0
                    while received < MAX_BYTES and time.monotonic() - start < MAX_SECONDS:
                        if self.cancelled(ident): raise RuntimeError('已取消')
                        block = response.read1(min(65536, MAX_BYTES - received))
                        if not block: raise RuntimeError('测速数据提前中断，请重试')
                        received += len(block)
                        if time.monotonic() - last >= .5:
                            progress(received); last = time.monotonic()
                    elapsed = time.monotonic() - start
                    if received < 64 * 1024 or elapsed <= 0: raise RuntimeError('有效数据不足，无法计算速度')
                    return {'bytes': received, 'seconds': round(elapsed, 3),
                            'bytesPerSecond': round(received / elapsed), 'mbps': round(received * 8 / elapsed / 1e6, 2),
                            'limited': received == MAX_BYTES, 'testedAt': time.time()}
                finally: conn.close()
            finally:
                if child.poll() is None:
                    child.terminate()
                    try: child.wait(timeout=3)
                    except subprocess.TimeoutExpired: child.kill(); child.wait()

    def worker(self, ident):
        job = self.read(self.path, None)
        if not job or job['id'] != ident: return
        job.update(status='running', message='正在测试'); atomic(self.path, job)
        results = self.read(self.results, {})
        def save(name, value):
            results.setdefault(name, {})[job['kind']] = value
            atomic(self.results, results)
            job.update(done=job['done'] + 1, message='已测试 %d / %d 个节点' % (job['done'] + 1, job['total']))
            atomic(self.path, job)
        try:
            known = self.nodes()
            results = {k: v for k, v in results.items() if k in known}
            if job['kind'] == 'delay':
                # Schedule only four probes at a time, so cancellation cannot
                # leave a large queue generating traffic after the user stops.
                names = iter(job['names'])
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    pending = {}
                    def schedule():
                        name = next(names, None)
                        if name is not None: pending[pool.submit(self.delay, name, known.get(name))] = name
                    for _ in range(4): schedule()
                    while pending:
                        done, _ = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
                        for future in done:
                            name = pending.pop(future)
                            try: value = future.result()
                            except Exception: value = {'error': '连接失败或超时', 'testedAt': time.time()}
                            save(name, value)
                            if not self.cancelled(ident): schedule()
            else:
                def progress(count):
                    job.update(message='正在下载测试数据', bytes=count); atomic(self.path, job)
                try: value = self.speed(job['names'][0], ident, progress)
                except Exception as error:
                    value = {'error': str(error) if isinstance(error, RuntimeError) else '下载测速失败，请检查节点、证书或测速服务', 'testedAt': time.time()}
                if not self.cancelled(ident): save(job['names'][0], value)
            job.update(status='cancelled' if self.cancelled(ident) else 'done', message='测试已取消' if self.cancelled(ident) else '测试完成')
        except Exception:
            job.update(status='failed', message='测速进程异常结束，请检查核心并重试')
        finally: atomic(self.path, job)


if __name__ == '__main__':
    os.umask(0o077)
    try:
        tests = Tests(os.environ['PLUG_HOME_DIR']); action = sys.argv[1]
        if action == 'worker': tests.worker(sys.argv[2]); sys.exit(0)
        data = json.load(sys.stdin)
        if not isinstance(data, dict): raise RuntimeError('请求格式无效')
        if action == 'list': result = tests.listing()
        elif action == 'start': result = tests.start(data)
        elif action == 'status': result = tests.status()
        elif action == 'cancel': result = tests.cancel(data)
        else: raise RuntimeError('不支持的测速操作')
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error) if isinstance(error, RuntimeError) else '测速接口不可用，请确认 Mihomo 已启动'}, ensure_ascii=False))
