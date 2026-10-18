#!/usr/bin/env python3
import fcntl
import hashlib
import hmac
import heapq
import json
import os
import secrets
import tempfile
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

ALLOWED_ROOTS = ("data", "video", "myshare", "share2me", "bak")
MAX_EXPIRES = 30 * 24 * 3600
MAX_USES = 1000

class ShareError(Exception):
    pass

class Store:
    def __init__(self, state_dir, plugin_user):
        self.state_dir = Path(state_dir)
        self.plugin_user = plugin_user
        self.pool_root = Path("/nas/pool0") / plugin_user
        self.state_file = self.state_dir / "shares.json"
        self.lock_file = self.state_dir / "shares.lock"
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _default(self):
        return {"version": 1, "settings": {"external_base": ""}, "shares": []}

    def _load_unlocked(self):
        try:
            with self.state_file.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
            if not isinstance(data, dict) or not isinstance(data.get("shares"), list):
                raise ValueError("invalid state")
            data.setdefault("settings", {"external_base": ""})
            return data
        except FileNotFoundError:
            return self._default()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ShareError("分享记录损坏，请检查 shares.json") from exc

    def _save_unlocked(self, data):
        fd, tmp_name = tempfile.mkstemp(prefix="shares.", suffix=".tmp", dir=str(self.state_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush(); os.fsync(stream.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.state_file)
        finally:
            try: os.unlink(tmp_name)
            except FileNotFoundError: pass

    def read(self):
        with self.lock_file.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            return self._load_unlocked()

    def mutate(self, callback):
        with self.lock_file.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            data = self._load_unlocked()
            result = callback(data)
            self._save_unlocked(data)
            return result

    def roots(self):
        result = []
        for name in ALLOWED_ROOTS:
            path = self.pool_root / name
            if path.is_dir(): result.append({"name": name, "path": name, "type": "dir"})
        return result

    def resolve_path(self, relative, expected=None):
        text = str(relative or "").strip().strip("/")
        if not text or "\\" in text or "\x00" in text:
            raise ShareError("请选择文件或文件夹")
        pure = PurePosixPath(text)
        if pure.is_absolute() or ".." in pure.parts or not pure.parts or pure.parts[0] not in ALLOWED_ROOTS:
            raise ShareError("路径不在允许的用户目录内")
        allowed = (self.pool_root / pure.parts[0]).resolve(strict=True)
        target = (self.pool_root / pure).resolve(strict=True)
        try: target.relative_to(allowed)
        except ValueError as exc: raise ShareError("路径越过了允许目录") from exc
        if expected == "file" and not target.is_file(): raise ShareError("请选择一个普通文件")
        if expected == "dir" and not target.is_dir(): raise ShareError("请选择一个文件夹")
        return target, pure.as_posix()

    def browse(self, relative="", offset=0, limit=200, dirs_only=False):
        if type(offset) is not int or not 0 <= offset <= 1000000 or type(limit) is not int or not 1 <= limit <= 1000 or type(dirs_only) is not bool:
            raise ShareError('分页参数无效')
        if not relative:
            return {"path": "", "entries": self.roots(), "hasMore":False, "nextOffset":0}
        directory, normalized = self.resolve_path(relative, "dir")
        try:
            with os.scandir(directory) as children:
                def candidates():
                    for item in children:
                        if item.name.startswith('.') or item.is_symlink(): continue
                        try:
                            kind = 'dir' if item.is_dir(follow_symlinks=False) else 'file' if item.is_file(follow_symlinks=False) else None
                            if kind and (not dirs_only or kind == 'dir'):
                                yield {'name':item.name,'path':normalized+'/'+item.name,'type':kind}
                        except OSError: continue
                # Only sort a bounded prefix; stat file sizes on the visible page.
                selected=heapq.nsmallest(offset+limit+1,candidates(),key=lambda row:(row['type']!='dir',row['name'].casefold(),row['name']))
        except PermissionError as exc:
            raise ShareError("没有权限读取此目录") from exc
        entries=selected[offset:offset+limit]
        for item in entries:
            try:
                item['size']=0 if item['type']=='dir' else (directory/item['name']).stat(follow_symlinks=False).st_size
            except OSError:item['size']=0
        return {"path":normalized,"entries":entries,"hasMore":len(selected)>offset+limit,"nextOffset":offset+len(entries)}

    @staticmethod
    def _password(password):
        if not password: return "", ""
        if len(password) < 4 or len(password) > 128: raise ShareError("密码长度应为 4–128 个字符")
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 210000).hex()
        return salt, digest

    def create(self, kind, relative, password, expires_in, max_uses):
        if kind not in ("download", "folder", "upload"): raise ShareError("分享类型无效")
        expected = "file" if kind == "download" else "dir"
        target, normalized = self.resolve_path(relative, expected)
        if kind == "folder":
            from folder_archive import open_folder
            try:
                with open_folder(self, normalized): pass
            except OSError as exc:
                raise ShareError("无法分享此文件夹：请检查读取权限，且路径不能包含符号链接") from exc
        try: expires_in = int(expires_in); max_uses = int(max_uses)
        except (TypeError, ValueError) as exc: raise ShareError("有效期或次数无效") from exc
        if expires_in < 300 or expires_in > MAX_EXPIRES: raise ShareError("有效期必须在 5 分钟到 30 天之间")
        if max_uses < 0 or max_uses > MAX_USES: raise ShareError("使用次数必须为 0–1000，0 表示不限")
        salt, digest = self._password(str(password or ""))
        now = int(time.time()); token = secrets.token_urlsafe(24)
        record = {
            "id": secrets.token_hex(8), "token": token, "kind": kind,
            "name": target.name, "path": normalized, "created_at": now,
            "expires_at": now + expires_in, "max_uses": max_uses, "uses": 0,
            "active": True, "password_salt": salt, "password_hash": digest,
            "last_used_at": 0, "last_ip": ""
        }
        self.mutate(lambda data: data["shares"].insert(0, record))
        return dict(record)

    @staticmethod
    def status(record, now=None):
        now = now or int(time.time())
        if not record.get("active", False): return "revoked"
        if int(record.get("expires_at", 0)) <= now: return "expired"
        maximum = int(record.get("max_uses", 0)); used = int(record.get("uses", 0))
        if maximum and used >= maximum: return "exhausted"
        return "active"

    def list(self): return self.read().get("shares", [])

    def find_token(self, token):
        with self.lock_file.open('a+') as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_SH)
            try:
                metadata=self.state_file.stat()
                signature=(metadata.st_ino,metadata.st_size,metadata.st_mtime_ns)
            except FileNotFoundError:signature=None
            if not hasattr(self,'_token_index') or signature!=getattr(self,'_token_signature',None):
                self._token_index={str(r.get('token','')):r for r in self._load_unlocked()['shares']}
                self._token_signature=signature
            record=self._token_index.get(str(token))
            return dict(record) if record and hmac.compare_digest(str(record.get('token','')),str(token)) else None

    def verify_password(self, record, password):
        expected = str(record.get("password_hash", "")); salt = str(record.get("password_salt", ""))
        if not expected: return True
        try: actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 210000).hex()
        except (ValueError, AttributeError): return False
        return hmac.compare_digest(expected, actual)

    def consume(self, token, kind, remote_ip):
        def update(data):
            for record in data["shares"]:
                if hmac.compare_digest(str(record.get("token", "")), str(token)):
                    if record.get("kind") != kind: raise ShareError("分享类型不匹配")
                    state = self.status(record)
                    if state != "active": raise ShareError(state)
                    record["uses"] = int(record.get("uses", 0)) + 1
                    record["last_used_at"] = int(time.time())
                    record["last_ip"] = str(remote_ip or "")[:64]
                    return dict(record)
            raise ShareError("not_found")
        return self.mutate(update)

    def rollback_use(self, token, kind):
        def update(data):
            for record in data["shares"]:
                if hmac.compare_digest(str(record.get("token", "")), str(token)) and record.get("kind") == kind:
                    record["uses"] = max(0, int(record.get("uses", 0)) - 1)
                    return True
            return False
        return self.mutate(update)

    def revoke(self, record_id):
        def update(data):
            for record in data["shares"]:
                if hmac.compare_digest(str(record.get("id", "")), str(record_id)):
                    record["active"] = False; return True
            raise ShareError("分享记录不存在")
        return self.mutate(update)

    def extend(self, record_id, seconds, expected_expires_at):
        """Extend one share under the same lock as consumption and revocation.

        Keep its token, password, permissions and usage count. The expected
        deadline prevents a repeated request from extending the record twice.
        """
        if not isinstance(record_id, str) or not record_id:
            raise ShareError("请选择要延期的分享记录")
        if type(seconds) is not int or not 300 <= seconds <= MAX_EXPIRES:
            raise ShareError("每次延期必须在 5 分钟到 30 天之间")
        if type(expected_expires_at) is not int or expected_expires_at <= 0:
            raise ShareError("原到期时间无效，请刷新分享记录后重试")
        def update(data):
            for record in data["shares"]:
                if not hmac.compare_digest(str(record.get("id", "")), record_id):
                    continue
                if not record.get("active", False):
                    raise ShareError("此分享已手动失效，不能通过延期恢复")
                maximum = int(record.get("max_uses", 0))
                if maximum and int(record.get("uses", 0)) >= maximum:
                    raise ShareError("此分享次数已用完，延期不会恢复次数，请重新创建分享")
                current = int(record.get("expires_at", 0))
                if current != expected_expires_at:
                    raise ShareError("分享的到期时间已改变，请刷新记录；本次未重复延期")
                record["expires_at"] = max(current, int(time.time())) + seconds
                return dict(record)
            raise ShareError("分享记录不存在")
        return self.mutate(update)

    def delete(self, record_id):
        record_id = str(record_id or "")
        if not record_id: raise ShareError("请选择要删除的分享记录")
        def update(data):
            for index, record in enumerate(data["shares"]):
                if hmac.compare_digest(str(record.get("id", "")), record_id):
                    del data["shares"][index]
                    return True
            raise ShareError("分享记录不存在")
        return self.mutate(update)

    def clear(self):
        def update(data):
            count = len(data["shares"])
            data["shares"] = []
            return count
        return self.mutate(update)

    def get_settings(self):
        return dict(self.read().get("settings") or {})

    def save_settings(self, external_base):
        value = str(external_base or "").strip().rstrip("/")
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment:
                raise ShareError("公网地址必须是完整的 HTTP 或 HTTPS 地址")
        def update(data): data["settings"] = {"external_base": value}
        self.mutate(update); return {"external_base": value}

def public_record(record, base_url):
    clean = {key: value for key, value in record.items() if key not in ("password_hash", "password_salt", "token")}
    clean["password_protected"] = bool(record.get("password_hash"))
    clean["status"] = Store.status(record)
    clean["url"] = base_url.rstrip("/") + "/s/" + record["token"]
    return clean
