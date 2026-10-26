#!/usr/bin/env python3
import json
import os
import pwd
import re
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs

sys.dont_write_bytecode=True
SCRIPT_DIR=Path(__file__).resolve().parent
match=re.search(r"/nas/pool[^/]+/(u[0-9]+)/plugin/pluginsrc/quickshare/ui$",str(SCRIPT_DIR)) or re.search(r"/home/(u[0-9]+)/plugin/quickshare/src/ui$",str(SCRIPT_DIR))
PLUGIN_USER=match.group(1) if match else os.environ.get("USER","")
if not re.fullmatch(r"u[0-9]+",PLUGIN_USER): PLUGIN_USER=pwd.getpwuid(Path(__file__).stat().st_uid).pw_name
PLUGIN_USER=str(PLUGIN_USER)
PLUGIN_HOME=Path("/home")/PLUGIN_USER/"plugin/quickshare"
STATE_DIR=PLUGIN_HOME/"var"
PORT_FILE=STATE_DIR/"server.port"
sys.path.insert(0,str(SCRIPT_DIR.parent/"files"))
from quickshare_lib import ShareError, Store, public_record
from plugin_paths import runtime_directory
from service_process import managed_pids

def service_port():
    try: value=int(PORT_FILE.read_text().strip())
    except (OSError,ValueError): return 19092
    return value if 1024 <= value <= 65535 else 19092

PORT=service_port()

def header(content_type="application/json; charset=utf-8"):
    print(f"Content-Type: {content_type}\r")
    print("Cache-Control: no-store\r")
    print("X-Content-Type-Options: nosniff\r")
    print("\r")

def respond(payload):
    header(); print(json.dumps(payload,ensure_ascii=False,separators=(",", ":")))
    raise SystemExit

def error(message): respond({"ok":False,"error":str(message)})

def serve_static():
    uri=os.environ.get("REQUEST_URI","/index.html").split("?",1)[0]
    if uri.endswith("/app.js"): name,mime="app.js","application/javascript; charset=utf-8"
    elif uri.endswith("/client-bridge.js"): name,mime="client-bridge.js","application/javascript; charset=utf-8"
    elif uri.endswith("/style.css"): name,mime="style.css","text/css; charset=utf-8"
    elif uri.endswith("/palette.css"): name,mime="palette.css","text/css; charset=utf-8"
    elif uri.endswith("/modal.css"): name,mime="modal.css","text/css; charset=utf-8"
    else: name,mime="index.html","text/html; charset=utf-8"
    header(mime)
    with (SCRIPT_DIR/name).open("r",encoding="utf-8") as stream: sys.stdout.write(stream.read())
    raise SystemExit

def body():
    try: length=int(os.environ.get("CONTENT_LENGTH","0"))
    except ValueError: raise ShareError("请求长度无效")
    if length<0 or length>131072: raise ShareError("请求内容过大")
    raw=sys.stdin.buffer.read(length) if length else b"{}"
    try: data=json.loads(raw)
    except (UnicodeDecodeError,json.JSONDecodeError) as exc: raise ShareError("请求不是有效 JSON") from exc
    if not isinstance(data,dict): raise ShareError("请求格式无效")
    return data

def action_name():
    values=parse_qs(os.environ.get("QUERY_STRING",""),keep_blank_values=True)
    return values.get("action",[""])[0]

def lan_ip():
    host=os.environ.get("HTTP_HOST","").split(":",1)[0]
    if re.fullmatch(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}",host) and host!="127.0.0.1": return host
    try:
        sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); sock.connect(("8.8.8.8",80)); value=sock.getsockname()[0]; sock.close(); return value
    except OSError: return "127.0.0.1"

def base_url():
    external=STORE.get_settings().get("external_base","").strip()
    if external: return external.rstrip("/")
    return f"http://{lan_ip()}:{PORT}"

def running():
    pid=next(managed_pids(PLUGIN_HOME),0)
    if pid:
        (runtime_directory(PLUGIN_HOME)/"server.pid").write_text(str(pid)+'\n')
    return bool(pid),pid

def control(command):
    script=PLUGIN_HOME/"scripts/control"
    env=os.environ.copy(); env.update({"PLUG_USER":PLUGIN_USER,"PLUG_HOME_DIR":str(PLUGIN_HOME),"PLUG_SRC_DIR":str(SCRIPT_DIR.parent)})
    result=subprocess.run([str(script),command],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=30)
    if result.returncode: raise ShareError("服务操作失败，请查看运行日志")

action=action_name()
if not action: serve_static()
if os.environ.get("REQUEST_METHOD","GET")!="POST": error("API 仅接受 POST 请求")
STORE=Store(STATE_DIR,PLUGIN_USER)
try:
    data=body(); base=base_url()
    if action=="status":
        is_running,pid=running(); records=STORE.list(); active=sum(1 for item in records if Store.status(item)=="active")
        try: version=json.loads((PLUGIN_HOME/"INFO").read_text()).get("version","1.1.50")
        except Exception: version="1.1.50"
        respond({"ok":True,"running":is_running,"pid":pid,"port":PORT,"lanUrl":f"http://{lan_ip()}:{PORT}","baseUrl":base,"activeShares":active,"totalShares":len(records),"pluginVersion":version})
    if action=="list": respond({"ok":True,"shares":[public_record(item,base) for item in STORE.list()]})
    if action=="browse": respond({"ok":True,**STORE.browse(data.get('path',''),data.get('offset',0),data.get('limit',200),data.get('dirsOnly',False))})
    if action=="create":
        record=STORE.create(data.get("kind"),data.get("path"),data.get("password",""),data.get("expiresIn"),data.get("maxUses"))
        respond({"ok":True,"share":public_record(record,base)})
    if action=="revoke": STORE.revoke(data.get("id","")); respond({"ok":True})
    if action=="extend":
        record=STORE.extend(data.get("id"),data.get("seconds"),data.get("expectedExpiresAt"))
        respond({"ok":True,"share":public_record(record,base)})
    if action=="delete": STORE.delete(data.get("id","")); respond({"ok":True})
    if action=="clear": respond({"ok":True,"deleted":STORE.clear()})
    if action=="settings_get":
        settings=STORE.get_settings(); respond({"ok":True,"externalBase":settings.get("external_base",""),"lanUrl":f"http://{lan_ip()}:{PORT}","port":PORT})
    if action=="settings_save":
        settings=STORE.save_settings(data.get("externalBase","")); respond({"ok":True,"externalBase":settings["external_base"],"baseUrl":base_url()})
    if action=="server_start": control("start"); respond({"ok":True})
    if action=="server_stop": control("stop"); respond({"ok":True})
    if action=="server_restart": control("restart"); respond({"ok":True})
    error("未知操作")
except ShareError as exc: error(exc)
except subprocess.TimeoutExpired: error("服务操作超时")
except Exception as exc: error("操作失败："+str(exc))
