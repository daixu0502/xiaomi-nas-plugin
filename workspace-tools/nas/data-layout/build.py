"""Generate the standalone inspection, cleanup and uninstall command."""
from pathlib import Path
import re,shlex
HERE=Path(__file__).resolve().parent
def build():
    source=(HERE/'src/cleanup.py').read_text().rstrip()
    wrapper=('#!/bin/sh\n# Generated from cleanup.py; no data migration.\nset -eu\numask 077\n'
             'export PATH="${PATH:+$PATH:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/data/docker"\n'
             'worker=$(mktemp /tmp/minas-manager.XXXXXXXX)\ntrap \'rm -f -- "$worker"\' EXIT\n'
             'cat > "$worker" <<\'MINAS_LAYOUT_PY\'\n'+source+'\nMINAS_LAYOUT_PY\npython3 -B "$worker" "$@"\n')
    (HERE.parents[2]/'scripts/migrate-minas-data.sh').write_text(wrapper,encoding='utf-8',newline='\n')
if __name__=='__main__':build()
