#!/usr/bin/env python3
"""Build standalone shell entry points from reviewed, secret-free source files."""
import argparse
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]
ROOT = PROJECT / 'scripts'
BASH_PROBE = re.compile(r'^# BEGIN MINAS BASH PROBE\n.*?^# END MINAS BASH PROBE\n', re.M | re.S)


def without_migration(source):
    source=re.sub(r'^# BEGIN MINAS AUTO LAYOUT SHELL \(generated\)\n.*?^# END MINAS AUTO LAYOUT SHELL\n', '', source, flags=re.M|re.S)
    source=re.sub(r'^\s*minas_auto_layout\s*$', '', source, flags=re.M)
    return source


def build_ssh_entrypoint(boot_installer_only=False):
    """Generate two pure shell entry points from the same reviewed worker."""
    worker = (HERE / 'src/restore-ssh.sh').read_text(encoding='utf-8').rstrip() + '\n'
    shared = PROJECT / 'plugins/sshrecovery/installer/common/modules/sshrecovery/restore-ssh.sh'
    if shared.is_file() and shared.read_text(encoding='utf-8') != worker:
        raise ValueError('Shared SSH worker differs from the plugin; synchronize the reviewed sources first')
    versions=re.findall(r"^SSH_RECOVERY_VERSION='([0-9]+\.[0-9]+\.[0-9]+)'$",worker,re.M)
    if len(versions)!=1:raise ValueError('Expected one SSH recovery script version marker')
    script_version=versions[0]
    probe = BASH_PROBE.search(worker).group(0)
    first = HERE / 'src/first-ssh-nas.sh'
    if not boot_installer_only:
        first.write_text(BASH_PROBE.sub(lambda _: probe, first.read_text(encoding='utf-8')),
                         encoding='utf-8', newline='\n')
    marker = 'MINAS_SSH_REPAIR_SH_SOURCE'
    if marker in worker.splitlines():
        raise ValueError('Source conflicts with heredoc delimiter: ' + marker)
    embedded = ('write_repair_sources() {\n'
                '    cat > "$STAGE/restore-ssh.sh" <<\'' + marker + '\'\n' +
                worker + marker + '\n}\n')
    bootstrap = without_migration((HERE / 'src/first-ssh-nas.sh').read_text(encoding='utf-8')).rstrip() + '\n'
    bootstrap_marker = 'MINAS_FIRST_SSH_SOURCE'
    if bootstrap_marker in bootstrap.splitlines():
        raise ValueError('Bootstrap source conflicts with heredoc delimiter')
    for name,source in [('xiaomi-storage-open-ssh-wsl-1.0.9.462.sh','open-ssh-client.sh'),
                        ('install-ssh-autostart-fix.sh','install-ssh-client.sh')]:
        if boot_installer_only and source!='install-ssh-client.sh':continue
        client=(HERE/'src'/source).read_text(encoding='utf-8')
        if source=='install-ssh-client.sh':
            if client.count('@SSH_RECOVERY_VERSION@')!=3:raise ValueError('Missing installer version template')
            client=client.replace('@SSH_RECOVERY_VERSION@',script_version)
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
    legacy=ROOT/'xiaomi-storage-open-ssh-wsl-1.0.8.425.sh'
    if legacy.is_file() and not boot_installer_only:
        text=without_migration(legacy.read_text())
        # The legacy opener keeps its original bootstrap and boot installer;
        # its embedded repair worker must never migrate installed components.
        text=text.replace("# Compatibility patch 2.1.2:","# Compatibility patch 2.1.3:")
        text=text.replace('兼容补丁 2.1.2','兼容补丁 2.1.3').replace("echo '2.1.2 用法", "echo '2.1.3 用法")
        legacy.write_text(text,encoding='utf-8',newline='\n')


def build(boot_installer_only=False):
    # Shared worker is shipped by the plugin and both standalone shell entries.
    plugin_worker = PROJECT / 'plugins/sshrecovery/installer/common/modules/sshrecovery/restore-ssh.sh'
    if plugin_worker.is_file():
        (HERE / 'src/restore-ssh.sh').write_bytes(plugin_worker.read_bytes().replace(b'\r\n', b'\n'))
    build_ssh_entrypoint(boot_installer_only=boot_installer_only)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-entrypoint-only', action='store_true',
                        help='Only regenerate the two pure shell SSH entry points from their adjacent sources')
    parser.add_argument('--boot-installer-only', action='store_true',
                        help='Sync the plugin worker and rebuild only the boot repair installer')
    args = parser.parse_args()
    if args.ssh_entrypoint_only:
        build_ssh_entrypoint()
    else:
        build(boot_installer_only=args.boot_installer_only)
