#!/usr/bin/env python3
"""Small stable boot entry. Recover with the transaction's immutable engine."""
import contextlib
import json
import fcntl
import os
from pathlib import Path
import re
import select
import stat
import subprocess

BASE = Path('/data/minas/plugins/manager')
INSTALL_LOCK = Path('/data/plugin/.local-plugin-installer.lock')
LEGACY_GUARD = Path('/run/minas-plugin-maintenance')
UNITS = Path('/etc/systemd/system')
OBSERVERS = (('eventsRunning', 'minas-maintenance-events.service'),
             ('watchRunning', 'minas-plugin-recovery-v2-watch.service'))


@contextlib.contextmanager
def installer_lock():
    fd = os.open(INSTALL_LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('unsafe installer lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def resume_repair_observers(state, units):
    for field, name in OBSERVERS:
        if not (units / name).is_file():
            continue
        active = state.get(field)
        if active is None:
            # Older repair journals saved unit enablement but no running flags.
            result = subprocess.run(['/usr/bin/systemctl', 'is-enabled', name], capture_output=True, timeout=15)
            active = result.stdout.strip() in (b'enabled', b'enabled-runtime')
        if active:
            subprocess.run(['/usr/bin/systemctl', 'start', '--no-block', name], check=True, timeout=15)


def recover_orphan():
    marker = BASE / 'maintenance.json'
    if not marker.exists() and not (LEGACY_GUARD.is_file() and LEGACY_GUARD.read_text().startswith('manager:')):
        return
    engine = BASE / 'cli.py'
    if engine.is_file():
        subprocess.run(['/usr/bin/python3', '-B', str(engine), 'recover', '--backend'], check=True)
        return
    # An interrupted first install may have published only this bootstrap.
    with installer_lock():
        if list((BASE / 'transactions').glob('*/journal.json')):
            return  # the journal owns recovery
        if marker.is_file():
            transaction = json.loads(marker.read_bytes())['transaction']
            if not re.fullmatch('[0-9a-f]{32}', transaction) or (BASE / 'transactions' / transaction).exists():
                raise ValueError('incomplete transaction; preserving maintenance guard')
            marker.unlink()
            parent = os.open(BASE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        if LEGACY_GUARD.is_file() and LEGACY_GUARD.read_text().startswith('manager:'):
            LEGACY_GUARD.unlink()


def main():
    if os.geteuid() != 0:
        raise PermissionError('root required')
    directory = BASE / 'transactions'
    for path in sorted(directory.iterdir()) if directory.exists() else []:
        if not re.fullmatch('[0-9a-f]{32}', path.name):
            raise ValueError('invalid transaction directory')
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue  # the watcher finished recovery after enumeration
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError('unsafe transaction')
        journal = path / 'journal.json'
        try:
            state = json.loads(journal.read_bytes())
        except FileNotFoundError:
            continue
        if state['phase'] == 'initializing':
            continue  # no device mutation is permitted before engine publication
        engine = path / 'engine/cli.py'
        if engine.is_symlink() or not engine.is_file():
            if not journal.exists():
                continue
            raise ValueError('transaction recovery engine missing')
        command = ['/usr/bin/python3', '-B', str(engine), 'recover', '--backend']
        pools = set(state.get('pendingPools', []))
        if state['phase'] == 'committed' and pools:
            # Finish the mounted portion and release observers now. Only cleanup
            # of the unmounted pools needs to wait for a mount notification.
            subprocess.run(command, check=True)
            if not journal.exists():
                break
            pools = set(json.loads(journal.read_bytes()).get('pendingPools', []))
        if not state.get('admissionOnly') and state['phase'] not in ('committed', 'rolled-back'):
            pools.update(row['pool'] for row in state['targets'] if row.get('pool'))
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
        if journal.exists():
            subprocess.run(command, check=True)
            # Old frozen engines replay the journal but can leave an enabled
            # watcher stopped after rolling back their own interrupted repair.
            if state.get('action') == 'repair' and state['phase'] != 'committed' and 'watchRunning' not in state:
                with installer_lock():
                    if (not (BASE / 'maintenance.json').exists() and not LEGACY_GUARD.exists()
                            and not any(directory.glob('*/journal.json'))):
                        resume_repair_observers(state, UNITS)
        break
    else:
        # Older versions could publish the persistent guard and lose its journal.
        # The installed engine takes the installer lock and re-scans before clearing it.
        recover_orphan()


if __name__ == '__main__':
    main()
