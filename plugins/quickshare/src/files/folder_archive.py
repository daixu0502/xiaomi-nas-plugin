"""Bounded, streaming ZIP archives; never follow links or create a temporary ZIP."""
import os
import stat
import zipfile
from contextlib import contextmanager
from pathlib import PurePosixPath

from quickshare_lib import ALLOWED_ROOTS, ShareError

MAX_ENTRIES = 10000
MAX_DEPTH = 64
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def safe_name(name):
    if not name or name in ('.', '..') or '\\' in name or any(ord(c) < 32 for c in name):
        raise ShareError('文件夹含有不适合 ZIP 的文件名，请修改名称后重试')
    return name


@contextmanager
def open_folder(store, relative):
    parts = PurePosixPath(relative).parts
    if not parts or parts[0] not in ALLOWED_ROOTS or '..' in parts:
        raise ShareError('路径不在允许的用户目录内')
    fd = os.open(store.pool_root, DIR_FLAGS)
    try:
        for name in parts:
            safe_name(name)
            child = os.open(name, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def open_entry(root_fd, parts, is_dir=False):
    fd = os.dup(root_fd)
    try:
        for index, name in enumerate(parts):
            flags = DIR_FLAGS if is_dir or index < len(parts) - 1 else FILE_FLAGS
            child = os.open(safe_name(name), flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def inventory(root_fd):
    """Metadata only. Bounded entry/depth limits prevent unbounded memory/recursion."""
    entries = []
    def walk(fd, prefix):
        if len(prefix) > MAX_DEPTH:
            raise ShareError('文件夹层级超过 64 层，请分享更小的子文件夹')
        with os.scandir(fd) as children:
            for child in children:
                if child.name.startswith('.') or child.is_symlink():
                    continue
                parts = prefix + (safe_name(child.name),)
                info = child.stat(follow_symlinks=False)
                is_dir = stat.S_ISDIR(info.st_mode)
                if not is_dir and not stat.S_ISREG(info.st_mode):
                    continue
                if len(entries) >= MAX_ENTRIES:
                    raise ShareError('文件夹超过 10000 个文件和子目录，请分开分享')
                entries.append((parts, is_dir, info))
                # Check permissions/type now, before sending download headers.
                with open_entry(fd, (child.name,), is_dir) as child_fd:
                    if signature(os.fstat(child_fd)) != signature(info):
                        raise ShareError('文件夹内容正在变化，请稍后重试')
                    if is_dir:
                        walk(child_fd, parts)
    walk(root_fd, ())
    return entries


class ZipOutput:
    """Unseekable target supported by Python zipfile (ZIP64 + data descriptors)."""
    def __init__(self, output):
        self.output = output
        self.position = 0
        self.failed = False

    def write(self, data):
        if self.failed:
            raise OSError('ZIP transfer aborted')
        try:
            self.output.write(data)
        except OSError:
            self.failed = True
            raise
        self.position += len(data)
        return len(data)

    def tell(self):
        return self.position

    def flush(self):
        if not self.failed:
            self.output.flush()


def write_zip(output, root_fd, name, entries):
    root_name = safe_name(name)
    sink = ZipOutput(output)
    archive = zipfile.ZipFile(sink, 'w', compression=zipfile.ZIP_STORED, allowZip64=True)
    try:
        archive.writestr(root_name + '/', b'')
        for parts, is_dir, before in entries:
            path = root_name + '/' + '/'.join(parts)
            with open_entry(root_fd, parts, is_dir) as fd:
                current = os.fstat(fd)
                if signature(current) != signature(before):
                    raise ShareError('文件夹内容已改变，请重新下载')
                if is_dir:
                    archive.writestr(path + '/', b'')
                    continue
                if not stat.S_ISREG(current.st_mode):
                    raise ShareError('文件类型已改变，请重新下载')
                info = zipfile.ZipInfo(path)
                info.file_size = current.st_size
                info.external_attr = 0o100644 << 16
                with archive.open(info, 'w', force_zip64=True) as target:
                    remaining = current.st_size
                    while remaining:
                        chunk = os.read(fd, min(1024 * 1024, remaining))
                        if not chunk:
                            raise ShareError('文件内容已改变，请重新下载')
                        target.write(chunk)
                        remaining -= len(chunk)
                if signature(os.fstat(fd)) != signature(before):
                    raise ShareError('文件内容已改变，请重新下载')
        archive.close()
    except BaseException:
        # Do not finish the central directory of an interrupted/invalid archive.
        sink.failed = True
        archive.fp = None
        raise
