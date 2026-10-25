"""Generate the standalone layout migration command."""
from pathlib import Path
HERE=Path(__file__).resolve().parent
def build():
    source=(HERE/'src/migrate_data_layout.py').read_text().rstrip()
    wrapper=('#!/bin/sh\n# Generated from migrate_data_layout.py.\nset -eu\numask 077\n'
             'export PATH="${PATH:+$PATH:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/data/docker"\n'
             'worker=$(mktemp /tmp/minas-layout.XXXXXXXX)\ntrap \'rm -f -- "$worker"\' EXIT\n'
             'cat > "$worker" <<\'MINAS_LAYOUT_PY\'\n'+source+'\nMINAS_LAYOUT_PY\npython3 -B "$worker" "$@"\n')
    (HERE.parents[1]/'migrate-minas-data.sh').write_text(wrapper,encoding='utf-8',newline='\n')
if __name__=='__main__':build()
