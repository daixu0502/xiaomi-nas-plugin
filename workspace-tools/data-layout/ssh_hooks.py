"""SSH-only compatibility while an older SSH plugin is still installed."""
DATA_LAYOUT_SSH_COMPAT = '#!/bin/sh\n# minas-ssh-layout-forwarder\nexec /bin/sh /data/minas/ssh/restore-ssh.sh "$@"\n'


def _layout_is_compat(path):
    from pathlib import Path
    path=Path(path)
    return path.is_dir() and {p.name for p in path.iterdir()}=={'restore-ssh.sh'} and (path/'restore-ssh.sh').read_text()==DATA_LAYOUT_SSH_COMPAT


def _layout_remove_compat():
    from pathlib import Path
    path=Path('/data/ssh-autostart-fix')
    if _layout_is_compat(path):(path/'restore-ssh.sh').unlink();path.rmdir()


def _layout_after_commit():
    import json
    from pathlib import Path
    registry=Path('/data/.minas-privileged/registry.json')
    if Path('/data/minas/ssh/restore-ssh.sh').is_file() and registry.is_file() and any('sshrecovery' in grants for grants in json.loads(registry.read_text()).get('users',{}).values()):
        old=Path('/data/ssh-autostart-fix');old.mkdir(mode=0o700,exist_ok=True);_layout_secure(old)
        _layout_atomic(old/'restore-ssh.sh',DATA_LAYOUT_SSH_COMPAT,0o700)
    _layout_atomic(Path(DATA_LAYOUT_ROOT)/'migration/ssh/complete',b'complete\n')


def _layout_lock_paths():
    from pathlib import Path
    return [Path(p)/'operations.lock' for p in ('/data/minas/privileged/modules/sshrecovery/var',
               '/data/.minas-privileged/modules/sshrecovery/var','/data/.minas-privileged/sshrecovery')]
