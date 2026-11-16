"""Persistent upload reservations and recovery, scoped to recorded files only."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

from folder_archive import open_folder
from quickshare_lib import ShareError, Store


def identity(info):
    return [info.st_dev, info.st_ino]


def change(store, ident, **values):
    def update(data):
        data['uploads'][ident].update(values)
    store.mutate(update)


def finish(store, ident, committed):
    def update(data):
        entry = data.get('uploads', {}).pop(ident, None)
        if entry and not committed:
            for share in data['shares']:
                if share.get('id') == entry['shareId']:
                    share['uses'] = max(0, int(share.get('uses', 0)) - 1)
                    break
    store.mutate(update)


def recover_one(store, ident, entry, directory=None):
    if not re.fullmatch(r'[0-9a-f]{32}', ident):
        raise ShareError('上传恢复记录编号无效')
    if directory is None:
        with open_folder(store, entry['path']) as fd:
            return recover_one(store, ident, entry, fd)
    if identity(os.fstat(directory)) != entry['directory']:
        raise ShareError('上传目录已改变，保留恢复记录')
    temporary = '.quickshare-upload-' + ident
    committed = entry.get('state') == 'committed'
    expected = entry.get('file')
    target = entry.get('target')
    if target:
        if target != Path(target).name or target in ('.', '..'):
            raise ShareError('上传恢复文件名无效')
        try:
            info = os.stat(target, dir_fd=directory, follow_symlinks=False)
            committed = committed or (stat.S_ISREG(info.st_mode) and identity(info) == expected)
        except FileNotFoundError:
            pass
    try:
        info = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        info = None
    if info is not None:
        # Before the inode is journaled, no upload bytes have been written.
        owned = stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
        matches = identity(info) == expected if expected else info.st_size == 0 and info.st_nlink == 1
        if not owned or not matches:
            raise ShareError('上传暂存文件已改变，拒绝删除')
        os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
    finish(store, ident, committed)
    return committed


@contextmanager
def server_guard(store):
    """No second server may reclaim the first server's live reservations."""
    fd = os.open(store.state_dir / 'upload-server.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ShareError('上传服务锁文件异常')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for ident, entry in store.read().get('uploads', {}).items():
            try:
                recover_one(store, ident, entry)
            except (OSError, ValueError, KeyError, ShareError):
                # Missing pools or changed files must never lead to broad deletion.
                print('Upload recovery pending: ' + ident, flush=True)
        yield
    finally:
        os.close(fd)


class Upload:
    def __init__(self, store, record, name, remote_ip):
        self.store, self.record, self.name, self.remote_ip = store, record, name, remote_ip
        self.ident = uuid.uuid4().hex
        self.temporary = '.quickshare-upload-' + self.ident
        self.fd = None
        self.committed = False

    def __enter__(self):
        self.folder = open_folder(self.store, self.record['path'])
        self.directory = self.folder.__enter__()
        self.reserved = False
        try:
            directory_id = identity(os.fstat(self.directory))
            def reserve(data):
                for record in data['shares']:
                    if record.get('id') == self.record['id'] and record.get('token') == self.record['token']:
                        state = Store.status(record)
                        if state != 'active' or record.get('kind') != 'upload':
                            raise ShareError('上传链接不可用：' + state)
                        record['uses'] = int(record.get('uses', 0)) + 1
                        record['last_used_at'] = int(time.time())
                        record['last_ip'] = str(self.remote_ip)[:64]
                        data.setdefault('uploads', {})[self.ident] = {
                            'shareId': record['id'], 'path': record['path'],
                            'directory': directory_id, 'state': 'reserved'}
                        return
                raise ShareError('上传链接不存在')
            self.store.mutate(reserve)
            self.reserved = True
            self.fd = os.open(self.temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=self.directory)
            change(self.store, self.ident, file=identity(os.fstat(self.fd)))
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def commit(self):
        os.fsync(self.fd)
        expected = identity(os.fstat(self.fd))
        original = Path(self.name)
        for number in range(10000):
            name = self.name if not number else f'{original.stem} ({number}){original.suffix}'
            change(self.store, self.ident, state='ready', target=name)
            if identity(os.stat(self.temporary, dir_fd=self.directory, follow_symlinks=False)) != expected:
                raise ShareError('上传暂存文件已被替换')
            try:
                os.link(self.temporary, name, src_dir_fd=self.directory, dst_dir_fd=self.directory,
                        follow_symlinks=False)
            except FileExistsError:
                continue
            self.committed = True
            self.name = name
            os.fsync(self.directory)
            change(self.store, self.ident, state='committed')
            return name
        raise ShareError('同名文件过多，请修改文件名后重试')

    def __exit__(self, exc_type, exc, traceback):
        try:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            if self.reserved:
                entry = self.store.read().get('uploads', {}).get(self.ident)
                if entry:
                    recover_one(self.store, self.ident, entry, self.directory)
        finally:
            self.folder.__exit__(exc_type, exc, traceback)
