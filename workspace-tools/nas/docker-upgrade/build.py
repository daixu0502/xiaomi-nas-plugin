"""Generate the single-file NAS entry; no companion files are needed on NAS."""
from pathlib import Path
HERE=Path(__file__).resolve().parent
def build():
    source=(HERE/'src/docker_runtime_upgrade.py').read_text().rstrip()
    wrapper=('#!/bin/sh\n# Generated from docker_runtime_upgrade.py.\nset -eu\numask 077\n'
             'export PATH="${PATH:+$PATH:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/data/docker"\n'
             'command -v python3 >/dev/null 2>&1 || { echo "缺少 python3" >&2; exit 2; }\n'
             'worker=$(mktemp /tmp/minas-docker-upgrade.XXXXXXXX)\n'
             'trap \'rm -f -- "$worker"\' EXIT\n'
             'cat > "$worker" <<\'MINAS_DOCKER_UPGRADE_PY\'\n'+source+'\nMINAS_DOCKER_UPGRADE_PY\n'
             'python3 -B "$worker" "$@"\n')
    target=HERE.parents[2]/'scripts/upgrade-docker-29.9.2.sh'
    target.write_text(wrapper,encoding='utf-8',newline='\n')
    target.chmod(0o755)
    print(target.name)
if __name__=='__main__':build()
