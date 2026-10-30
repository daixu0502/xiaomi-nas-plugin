"""Open flock files without generating writable-close filesystem events."""
import os
import stat


def open_lock(path):
    # Linux flock permits exclusive locking of a read-only descriptor. Keep the
    # inode shared by every caller, including older processes during an update.
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise OSError("锁文件必须是当前用户拥有的独立普通文件")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
