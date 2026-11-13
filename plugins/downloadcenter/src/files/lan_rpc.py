"""Opt-in HTTP JSON-RPC LAN listener; the aria2 core stays on loopback."""
import http.client
import ipaddress
import json
import socket
import threading
import copy
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from openlist_gateway import lan_config, client_allowed, map_path, safe_mkdir
from download_lib import DownloadError


def map_payload(m, payload, depth=0):
    """Map authenticated aria2 path options, including JSON-RPC multicalls."""
    if depth > 8: raise DownloadError('RPC 批量调用层级过多')
    if isinstance(payload, list):
        if len(payload) > 1000: raise DownloadError('RPC 批量调用过大')
        return [map_payload(m, call, depth + 1) for call in payload]
    if not isinstance(payload, dict): raise DownloadError('RPC 调用格式无效')
    call = copy.deepcopy(payload)
    method = call.get('method', call.get('methodName', ''))
    params = call.get('params', [])
    if not isinstance(params, list): raise DownloadError('RPC 参数格式无效')
    if method == 'system.multicall':
        if params: params[0] = map_payload(m, params[0], depth + 1)
        return call
    # Authenticate before inspecting or creating a requested directory.
    token = params[0] if params and isinstance(params[0], str) else ''
    secret = (m.var / 'rpc.secret').read_text().strip()
    if not hmac.compare_digest(token, 'token:' + secret): return call
    index = {'aria2.addUri':2, 'aria2.addTorrent':3, 'aria2.addMetalink':2,
             'aria2.changeOption':2, 'aria2.changeGlobalOption':1}.get(method)
    if index is None or len(params) <= index: return call
    options = params[index]
    if not isinstance(options, dict): raise DownloadError('下载选项格式无效')
    if 'dir' in options:
        if not isinstance(options['dir'], str): raise DownloadError('下载目录格式无效')
        target = map_path(m, options['dir'])
        safe_mkdir(m, target)
        options['dir'] = str(target)
    if 'out' in options:
        name = options['out']
        if not isinstance(name, str) or not name or name in ('.','..') or '/' in name or '\\' in name or any(ord(c)<32 for c in name):
            raise DownloadError('下载文件名无效')
    return call


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def allowed(self):
        peer = ipaddress.ip_address(self.client_address[0])
        return (client_allowed(peer, self.server.networks)
                and self.headers.get('Host', '').split(':')[0] == self.server.server_address[0]
                and self.path == '/jsonrpc')

    def respond(self, status, body=b''):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        # AriaNg uses explicit RPC tokens, never browser cookies.
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        if not self.allowed(): return self.respond(403)
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self): self.respond(405)

    def do_POST(self):
        upstream = None
        try:
            self.connection.settimeout(15)
            if not self.allowed() or self.headers.get('Transfer-Encoding'):
                return self.respond(403)
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8 * 1024 * 1024: return self.respond(413)
            body = self.rfile.read(length)
            payload = json.loads(body)
            if not isinstance(payload, (dict, list)): return self.respond(400)
            payload = map_payload(self.server.manager, payload)
            body = json.dumps(payload).encode()
            # Fixed upstream only. Core verifies the RPC token for protected methods.
            upstream = http.client.HTTPConnection('127.0.0.1', self.server.rpc_port, timeout=15)
            upstream.request('POST', '/jsonrpc', body, {'Content-Type': 'application/json'})
            result = upstream.getresponse()
            raw = result.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024: return self.respond(502)
            self.respond(result.status, raw)
        except DownloadError:
            self.respond(400, json.dumps({'error': {'code': -32602, 'message': '下载路径或参数无效，请检查 NAS 下载目录'}}).encode())
        except (ValueError, OSError, http.client.HTTPException):
            self.respond(502)
        finally:
            if upstream: upstream.close()


class Listener:
    def __init__(self, m):
        self.stop = threading.Event()
        self.server = None
        self.worker = threading.Thread(target=self.start, args=(m,), daemon=True)
        self.worker.start()

    def start(self, m):
        try:
            cfg = lan_config(m, 'aria2-lan.json')
            if not cfg: return
            address, networks = cfg
            port = int((m.var / 'rpc.port').read_text())
        except (ValueError, KeyError, TypeError, OSError):
            print('aria2 LAN configuration invalid; loopback remains available', flush=True)
            return
        while not self.stop.is_set():
            try: server = ThreadingHTTPServer((address, port), Handler)
            except OSError:
                self.stop.wait(5)
                continue
            server.daemon_threads = True
            server.rpc_port, server.networks = port, networks
            server.manager = m
            if self.stop.is_set():
                server.server_close()
                return
            self.server = server
            threading.Thread(target=server.serve_forever, daemon=True).start()
            return

    def shutdown(self):
        self.stop.set()
        self.worker.join()
        if self.server: self.server.shutdown()

    def server_close(self):
        if self.server: self.server.server_close()


def port_listening(address, port):
    try:
        encoded = socket.inet_aton(address)[::-1].hex().upper() + ':%04X' % port
        return any(line.split()[1] == encoded and line.split()[3] == '0A'
                   for line in Path('/proc/net/tcp').read_text().splitlines()[1:])
    except (OSError, ValueError, IndexError): return False


def status(m):
    rows = {}
    for name, config, portfile, path in [('aria2', 'aria2-lan.json', 'rpc.port', '/jsonrpc'),
                                        ('qbittorrent', 'openlist-lan.json', 'openlist.port', '/')]:
        row = {'configured': False, 'listening': False, 'address': '', 'port': None, 'url': ''}
        try:
            row['port'] = int((m.var / portfile).read_text())
            cfg = lan_config(m, config)
            if cfg:
                row.update(configured=True, address=cfg[0], listening=port_listening(cfg[0], row['port']))
                row['url'] = 'http://%s:%d%s' % (cfg[0], row['port'], path)
        except (OSError, ValueError, KeyError, TypeError): row['invalid'] = True
        rows[name] = row
    return rows
