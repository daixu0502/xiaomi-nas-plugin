"""Lifecycle requests from CGI/update workers always enter the fixed broker."""
import contextlib
import fcntl
import os
import subprocess
from download_lib import DownloadError
from runtime_lock import open_lock


def control(m, action, temporary=False):
    if action not in ('start', 'stop', 'restart', 'finish') or (action == 'finish' and not temporary):
        raise DownloadError('无效服务操作')
    operation = 'update-' + action if temporary else action
    result = subprocess.run(['/bin/sh', str(m.home / 'scripts/control'), operation],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=120,
                            env={k:v for k,v in os.environ.items() if k != 'MINAS_SERVICE_MANAGED'})
    if result.returncode:
        raise DownloadError('下载服务操作失败，请检查服务日志')


@contextlib.contextmanager
def update_transaction(m):
    # Separate from lifecycle.lock, acquired by systemd's start/stop child.
    # Holding lifecycle.lock while awaiting systemctl would deadlock.
    with open_lock(m.runtime / 'engine-transaction.lock') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise DownloadError('另一个内核切换正在进行')
        yield
