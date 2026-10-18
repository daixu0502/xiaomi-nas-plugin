#!/usr/bin/env python3
import argparse
import base64
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
import socket
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from quickshare_lib import ShareError, Store
from folder_archive import open_folder, inventory, write_zip

MAX_UPLOAD = 20 * 1024 * 1024 * 1024
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")
FAILURES = {}
FAIL_LOCK = threading.Lock()

def format_size(value):
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB": return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024

def page(title, body):
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#073f35"><title>{html.escape(title)}</title><style>
*{{box-sizing:border-box;-webkit-tap-highlight-color:transparent}}body{{min-height:100vh;margin:0;padding:max(24px,env(safe-area-inset-top)) max(18px,env(safe-area-inset-right)) max(30px,env(safe-area-inset-bottom)) max(18px,env(safe-area-inset-left));color:#15312d;background:radial-gradient(circle at 100% 0,#baf5df,transparent 35rem),#edf8f4;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}}main{{width:min(100%,520px);margin:8vh auto 0}}.brand{{margin-bottom:18px;color:#08745f;font-size:11px;font-weight:800;letter-spacing:.16em}}.card{{padding:24px;border:1px solid #fff;border-radius:24px;background:rgba(255,255,255,.94);box-shadow:0 18px 55px rgba(21,76,65,.13)}}.icon{{display:grid;place-items:center;width:58px;height:58px;margin-bottom:18px;border-radius:18px;color:#fff;background:linear-gradient(145deg,#34d399,#047857);font-size:28px}}h1{{margin:0 0 7px;font-size:24px}}p{{margin:0;color:#637b76;font-size:14px;line-height:1.65}}.meta{{margin:18px 0;padding:13px 14px;border-radius:14px;background:#f0f8f5;font-size:13px;overflow-wrap:anywhere}}label{{display:block;margin:17px 0 7px;font-size:12px;font-weight:800}}input[type=password],input[type=file]{{width:100%;min-height:46px;padding:11px;border:1px solid #cfe1dc;border-radius:12px;background:#fff;font-size:16px}}button,.button{{display:flex;align-items:center;justify-content:center;width:100%;min-height:46px;margin-top:13px;border:0;border-radius:13px;color:#fff;background:#07816b;font-size:14px;font-weight:800;text-decoration:none}}.secondary{{color:#08745f;background:#dcf6ed}}.error{{margin-bottom:12px;padding:11px;border-radius:11px;color:#b42318;background:#fff0ee}}.progress{{height:8px;margin-top:14px;overflow:hidden;border-radius:9px;background:#e5efec}}.bar{{width:0;height:100%;background:#12a184;transition:.2s}}small{{display:block;margin-top:14px;color:#7b8d89;line-height:1.55}}footer{{padding:20px;text-align:center;color:#83938f;font-size:11px}}
</style></head><body><main><div class="brand">XIAOMI SMART STORAGE · QUICK SHARE</div>{body}<footer>文件快传 · 临时分享服务</footer></main></body></html>'''

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    def __init__(self, address, handler, store, secret):
        super().__init__(address, handler); self.store=store; self.secret=secret
        self.archive_slots = threading.BoundedSemaphore(2)

class Handler(BaseHTTPRequestHandler):
    server_version = "QuickShare/1.0"
    sys_version = ""

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def security_headers(self, content_type="text/html; charset=utf-8", length=None, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        if length is not None: self.send_header("Content-Length", str(length))
        for key, value in (extra or {}).items(): self.send_header(key, value)
        self.end_headers()

    def send_html(self, title, body, status=200, extra=None):
        data = page(title, body).encode("utf-8")
        self.security_headers(length=len(data), status=status, extra=extra); self.wfile.write(data)

    def send_json(self, payload, status=200):
        data=json.dumps(payload,ensure_ascii=False,separators=(",", ":")).encode()
        self.security_headers("application/json; charset=utf-8",len(data),status); self.wfile.write(data)

    def path_parts(self):
        return [unquote(part) for part in urlsplit(self.path).path.split("/") if part]

    def record(self, token):
        if not TOKEN_RE.fullmatch(token): return None
        return self.server.store.find_token(token)

    def cookie_name(self, token): return "qs_" + token[:10]

    def make_cookie(self, token, expires_at):
        expiry=min(int(expires_at),int(time.time())+3600)
        message=f"{token}:{expiry}".encode(); signature=hmac.new(self.server.secret,message,hashlib.sha256).hexdigest()
        value=base64.urlsafe_b64encode(f"{expiry}:{signature}".encode()).decode().rstrip("=")
        return f"{self.cookie_name(token)}={value}; Path=/; Max-Age={max(0,expiry-int(time.time()))}; HttpOnly; SameSite=Strict"

    def authorized(self, record):
        if not record.get("password_hash"): return True
        cookies={}
        for part in self.headers.get("Cookie","").split(";"):
            if "=" in part:
                key,value=part.strip().split("=",1); cookies[key]=value
        value=cookies.get(self.cookie_name(record["token"]),"")
        try:
            raw=base64.urlsafe_b64decode(value+"="*(-len(value)%4)).decode(); expiry_text,signature=raw.split(":",1); expiry=int(expiry_text)
            if expiry < time.time(): return False
            expected=hmac.new(self.server.secret,f"{record['token']}:{expiry}".encode(),hashlib.sha256).hexdigest()
            return hmac.compare_digest(signature,expected)
        except Exception: return False

    def valid_record(self, token, kind=None):
        record=self.record(token)
        if not record: return None,"not_found"
        status=Store.status(record)
        if status != "active": return record,status
        if kind and record.get("kind") != kind: return record,"wrong_type"
        return record,"active"

    def unavailable(self, reason):
        messages={"not_found":"链接不存在","revoked":"分享已失效","expired":"分享已过期","exhausted":"使用次数已用完","wrong_type":"链接类型错误"}
        body=f'<div class="card"><div class="icon">×</div><h1>{messages.get(reason,"暂时无法访问")}</h1><p>请联系分享者重新生成链接。</p></div>'
        self.send_html("链接不可用",body,HTTPStatus.NOT_FOUND)

    def do_GET(self):
        parts=self.path_parts()
        if parts==["health"]: return self.send_json({"ok":True})
        if len(parts)==2 and parts[0]=="s": return self.share_page(parts[1])
        if len(parts)==2 and parts[0]=="d": return self.download(parts[1])
        self.unavailable("not_found")

    def share_page(self, token, error=""):
        record,status=self.valid_record(token)
        if status!="active": return self.unavailable(status)
        if not self.authorized(record):
            message=f'<div class="error">{html.escape(error)}</div>' if error else ''
            body=f'''<div class="card"><div class="icon">🔒</div><h1>需要访问密码</h1><p>此临时链接已受到密码保护。</p>{message}<form method="post" action="/auth/{token}"><label for="password">访问密码</label><input id="password" name="password" type="password" minlength="4" maxlength="128" autocomplete="current-password" required><button type="submit">验证并继续</button></form></div>'''
            return self.send_html("输入访问密码",body)
        remaining="不限次数" if not record.get("max_uses") else f'剩余 {max(0,int(record["max_uses"])-int(record.get("uses",0)))} 次'
        if record["kind"]=="download":
            try: target,_=self.server.store.resolve_path(record["path"],"file"); size=format_size(target.stat().st_size)
            except (ShareError,OSError): return self.unavailable("not_found")
            body=f'''<div class="card"><div class="icon">↓</div><h1>{html.escape(record["name"])}</h1><p>分享者允许你下载这个文件。</p><div class="meta">文件大小：{size}<br>下载限制：{remaining}</div><a class="button" href="/d/{token}">下载文件</a><small>链接达到有效期或次数限制后会自动失效。</small></div>'''
        elif record["kind"]=="folder":
            try:
                with open_folder(self.server.store, record["path"]): pass
            except (ShareError,OSError): return self.unavailable("not_found")
            body=f'''<div class="card"><div class="icon">▰</div><h1>{html.escape(record["name"])}</h1><p>下载整个文件夹，保留文件和子目录结构。</p><div class="meta">下载格式：ZIP（不压缩，减少设备负载）<br>下载限制：{remaining}</div><a class="button" href="/d/{token}">下载文件夹 ZIP</a><small>包含下载时的文件夹内容，不含隐藏文件、符号链接或特殊文件。最多 10000 个文件和子目录、64 层目录。开始传输计一次，浏览页面不计次；中断重试会再次计次，不支持断点续传。扫描较大文件夹需要一些时间，下载时请勿修改源文件。</small></div>'''
        else:
            body=f'''<div class="card"><div class="icon">↑</div><h1>上传文件</h1><p>文件会直接保存到分享者指定的文件夹。</p><div class="meta">上传限制：{remaining}<br>单个文件最大：20 GB</div><input id="file" type="file"><button id="upload" type="button">开始上传</button><div class="progress"><div id="bar" class="bar"></div></div><small id="message">上传过程中请保持页面打开。</small></div><script>
(function(){{var b=document.getElementById('upload'),f=document.getElementById('file'),bar=document.getElementById('bar'),msg=document.getElementById('message');b.onclick=function(){{if(!f.files.length){{msg.textContent='请先选择文件';return}}var file=f.files[0],x=new XMLHttpRequest();b.disabled=true;msg.textContent='正在上传 '+file.name;x.open('POST','/upload/{token}?name='+encodeURIComponent(file.name));x.setRequestHeader('Content-Type','application/octet-stream');x.upload.onprogress=function(e){{if(e.lengthComputable)bar.style.width=Math.round(e.loaded/e.total*100)+'%'}};x.onload=function(){{b.disabled=false;try{{var r=JSON.parse(x.responseText);msg.textContent=r.ok?'上传完成：'+r.name:(r.error||'上传失败')}}catch(e){{msg.textContent='上传失败'}}}};x.onerror=function(){{b.disabled=false;msg.textContent='网络中断，上传失败'}};x.send(file)}}}})();
</script>'''
        self.send_html(record["name"],body)

    def download(self, token):
        record,status=self.valid_record(token)
        if status!="active": return self.unavailable(status)
        if record.get("kind") not in ("download", "folder"): return self.unavailable("wrong_type")
        if not self.authorized(record): return self.share_page(token)
        if record["kind"] == "folder": return self.download_folder(record)
        try: target,_=self.server.store.resolve_path(record["path"],"file"); size=target.stat().st_size
        except (ShareError,OSError): return self.unavailable("not_found")
        try: self.server.store.consume(token,"download",self.client_address[0])
        except ShareError as exc: return self.unavailable(str(exc))
        mime=mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        disposition="attachment; filename*=UTF-8''"+quote(target.name,safe="")
        self.security_headers(mime,size,200,{"Content-Disposition":disposition,"Accept-Ranges":"none"})
        try:
            with target.open("rb") as stream:
                while True:
                    chunk=stream.read(1024*1024)
                    if not chunk: break
                    self.wfile.write(chunk)
        except (BrokenPipeError,ConnectionResetError): pass

    def download_folder(self, record):
        if not self.server.archive_slots.acquire(blocking=False):
            return self.send_html("请稍后重试", '<div class="card"><h1>文件夹下载繁忙</h1><p>最多同时下载两个文件夹，请稍后重试；本次不计下载次数。</p></div>', 503, {"Retry-After":"10"})
        started = False
        try:
            with open_folder(self.server.store, record["path"]) as fd:
                entries = inventory(fd)
                # Recheck expiry/revocation/count after scanning, atomically reserve one use.
                self.server.store.consume(record["token"], "folder", self.client_address[0])
                disposition="attachment; filename*=UTF-8''"+quote(record["name"]+".zip",safe="")
                started = True
                self.close_connection = True
                self.connection.settimeout(60)
                self.security_headers("application/zip", extra={"Content-Disposition":disposition,"Accept-Ranges":"none","Connection":"close"})
                write_zip(self.wfile, fd, record["name"], entries)
        except (ShareError, OSError, ValueError) as exc:
            if started:
                self.close_connection = True
                self.log_message("folder download interrupted (%s)", type(exc).__name__)
            elif isinstance(exc, ShareError) and str(exc) in ("expired", "revoked", "exhausted", "not_found"):
                self.unavailable(str(exc))
            else:
                message=str(exc) if isinstance(exc, ShareError) else "文件夹不存在、读取权限不足或内容发生变化，请联系分享者检查。"
                self.send_html("无法下载文件夹", '<div class="card"><h1>无法下载文件夹</h1><p>'+html.escape(message)+'</p><small>本次未计下载次数。</small></div>', 409)
        finally:
            self.server.archive_slots.release()

    def do_POST(self):
        parts=self.path_parts()
        if len(parts)==2 and parts[0]=="auth": return self.auth(parts[1])
        if len(parts)==2 and parts[0]=="upload": return self.upload(parts[1])
        self.unavailable("not_found")

    def too_many_failures(self, ip, add=False):
        now=time.time()
        with FAIL_LOCK:
            recent=[stamp for stamp in FAILURES.get(ip,[]) if now-stamp<600]
            if add: recent.append(now)
            FAILURES[ip]=recent
            return len(recent)>=5

    def auth(self, token):
        record,status=self.valid_record(token)
        if status!="active": return self.unavailable(status)
        ip=self.client_address[0]
        if self.too_many_failures(ip): return self.send_html("请求过多",'<div class="card"><h1>尝试次数过多</h1><p>请十分钟后再试。</p></div>',429)
        try: length=int(self.headers.get("Content-Length","0"))
        except ValueError: length=0
        if length<1 or length>8192: return self.share_page(token,"请求无效")
        values=parse_qs(self.rfile.read(length).decode("utf-8","replace")); password=values.get("password",[""])[0]
        if not self.server.store.verify_password(record,password):
            self.too_many_failures(ip,True); return self.share_page(token,"密码错误")
        with FAIL_LOCK: FAILURES.pop(ip,None)
        self.send_response(303); self.send_header("Location",f"/s/{token}"); self.send_header("Set-Cookie",self.make_cookie(token,record["expires_at"])); self.send_header("Cache-Control","no-store"); self.end_headers()

    def safe_upload_name(self, value):
        name=os.path.basename(value).strip().replace("\x00","")
        if not name or name in (".","..") or len(name.encode("utf-8"))>240: raise ShareError("文件名无效")
        return name

    def upload(self, token):
        record,status=self.valid_record(token,"upload")
        if status!="active": return self.send_json({"ok":False,"error":"链接不可用"},410)
        if not self.authorized(record): return self.send_json({"ok":False,"error":"请先验证访问密码"},401)
        try: length=int(self.headers.get("Content-Length","-1"))
        except ValueError: length=-1
        if length<0 or length>MAX_UPLOAD: return self.send_json({"ok":False,"error":"文件过大或长度无效"},413)
        try:
            directory,_=self.server.store.resolve_path(record["path"],"dir")
            query=parse_qs(urlsplit(self.path).query); name=self.safe_upload_name(query.get("name",[""])[0])
        except ShareError as exc: return self.send_json({"ok":False,"error":str(exc)},400)
        target=directory/name; stem,suffix=target.stem,target.suffix; counter=1
        while target.exists(): target=directory/f"{stem} ({counter}){suffix}"; counter+=1
        temp=directory/(".quickshare-upload-"+token[:8]+"-"+str(threading.get_ident()))
        remaining=length
        try: self.server.store.consume(token,"upload",self.client_address[0])
        except ShareError as exc: return self.send_json({"ok":False,"error":"链接不可用："+str(exc)},410)
        try:
            with temp.open("xb") as output:
                while remaining:
                    chunk=self.rfile.read(min(1024*1024,remaining))
                    if not chunk: raise ConnectionError("上传中断")
                    output.write(chunk); remaining-=len(chunk)
                output.flush(); os.fsync(output.fileno())
            os.replace(temp,target)
            return self.send_json({"ok":True,"name":target.name,"size":length})
        except (OSError,ConnectionError) as exc:
            try: temp.unlink()
            except OSError: pass
            try: self.server.store.rollback_use(token,"upload")
            except ShareError: pass
            return self.send_json({"ok":False,"error":"上传失败："+str(exc)},500)

def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--state",required=True); parser.add_argument("--port",type=int,default=19092); args=parser.parse_args()
    state=Path(args.state); secret_file=state/"server.secret"
    if not secret_file.is_file(): raise SystemExit("server.secret missing")
    user=state.parts[2] if len(state.parts)>2 and state.parts[1]=="home" else os.environ.get("USER","")
    if not re.fullmatch(r"u[0-9]+",user): raise SystemExit("invalid plugin user")
    server=Server(("0.0.0.0",args.port),Handler,Store(state,user),secret_file.read_bytes())
    print(f"QuickShare listening on 0.0.0.0:{args.port}",flush=True)
    try: server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt: pass
    finally: server.server_close()
if __name__=="__main__": main()
