#!/usr/bin/env python3
"""Small stable boot entry. Recover with the transaction's immutable engine."""
import json
import os
from pathlib import Path
import re
import select
import stat
import subprocess

BASE = Path('/data/minas/plugins/manager')


def main():
    if os.geteuid() != 0:
        raise PermissionError('root required')
    directory = BASE / 'transactions'
    for path in sorted(directory.iterdir()) if directory.exists() else []:
        if not re.fullmatch('[0-9a-f]{32}', path.name):
            raise ValueError('invalid transaction directory')
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError('unsafe transaction')
        journal = path / 'journal.json'
        if not journal.is_file():
            continue
        state = json.loads(journal.read_bytes())
        if state['phase'] == 'initializing':
            continue  # no device mutation is permitted before engine publication
        pools = set(state.get('pendingPools', [])) or {row['pool'] for row in state['targets'] if row.get('pool')}
        if not all(re.fullmatch('pool[0-9]+', pool) for pool in pools):
            raise ValueError('invalid pool in transaction')
        # A delayed NAS data-pool mount must not restore into its empty mountpoint.
        # Wait on mount events, with no periodic scans of source or user state.
        with open('/proc/self/mounts') as mounts:
            poll = select.poll()
            poll.register(mounts.fileno(), select.POLLPRI | select.POLLERR)
            while not all(os.path.ismount('/nas/' + pool) for pool in pools):
                poll.poll()
                mounts.seek(0); mounts.read()
        engine = path / 'engine/cli.py'
        if engine.is_symlink() or not engine.is_file():
            raise ValueError('transaction recovery engine missing')
        subprocess.run(['/usr/bin/python3', '-B', str(engine), 'recover', '--backend'], check=True)
        break


if __name__ == '__main__':
    main()
