#!/usr/bin/env python3
"""Build standalone shell entry points from reviewed, secret-free source files."""
import argparse
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PROJECT = ROOT.parent


def build_ssh_entrypoint():
    """Generate two pure shell entry points from the same reviewed worker."""
    worker = (HERE / 'src/restore-ssh.sh').read_text(encoding='utf-8').rstrip() + '\n'
    shared = PROJECT / 'sshrecovery/installer/common/modules/sshrecovery/restore-ssh.sh'
    if shared.is_file() and shared.read_text(encoding='utf-8') != worker:
        raise ValueError('Shared SSH worker differs from the plugin; synchronize the reviewed sources first')
    marker = 'MINAS_SSH_REPAIR_SH_SOURCE'
    if marker in worker.splitlines():
        raise ValueError('Source conflicts with heredoc delimiter: ' + marker)
    embedded = ('write_repair_sources() {\n'
                '    cat > "$STAGE/restore-ssh.sh" <<\'' + marker + '\'\n' +
                worker + marker + '\n}\n')
    bootstrap = (HERE / 'src/first-ssh-nas.sh').read_text(encoding='utf-8').rstrip() + '\n'
    bootstrap_marker = 'MINAS_FIRST_SSH_SOURCE'
    if bootstrap_marker in bootstrap.splitlines():
        raise ValueError('Bootstrap source conflicts with heredoc delimiter')
    for name,source in [('xiaomi-storage-open-ssh-wsl-1.0.9.462.sh','open-ssh-client.sh'),
                        ('install-ssh-autostart-fix.sh','install-ssh-client.sh')]:
        client=(HERE/'src'/source).read_text(encoding='utf-8')
        function = 'write_bootstrap_sources' if source == 'open-ssh-client.sh' else 'write_repair_sources'
        start=client.index(function + '() {')
        end=client.index('\n}\n',start)+3
        selected = embedded
        if source == 'open-ssh-client.sh':
            selected = ('write_bootstrap_sources() {\n'
                        '    cat > "$STAGE/first-ssh-nas.sh" <<\'' + bootstrap_marker + '\'\n' +
                        bootstrap + bootstrap_marker + '\n}\n')
        standalone=client[:start]+selected+'\n'+client[end:]
        if name=='install-ssh-autostart-fix.sh':
            standalone=standalone.replace('then main "$@"; fi','then main --install-boot-only "$@"; fi')
        (ROOT/name).write_text(standalone,encoding='utf-8',newline='\n')


def build():
    # Shared worker is shipped by the plugin and both standalone shell entries.
    plugin_worker = PROJECT / 'sshrecovery/installer/common/modules/sshrecovery/restore-ssh.sh'
    if plugin_worker.is_file():
        (HERE / 'src/restore-ssh.sh').write_bytes(plugin_worker.read_bytes().replace(b'\r\n', b'\n'))
    build_ssh_entrypoint()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-entrypoint-only', action='store_true',
                        help='Only regenerate the two pure shell SSH entry points from their adjacent sources')
    args = parser.parse_args()
    if args.ssh_entrypoint_only:
        build_ssh_entrypoint()
    else:
        build()
