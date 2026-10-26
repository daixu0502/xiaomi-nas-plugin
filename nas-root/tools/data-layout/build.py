"""Generate the standalone inspection, cleanup and uninstall command."""
from pathlib import Path
import re,shlex
HERE=Path(__file__).resolve().parent
def build():
    project=HERE.parents[2]
    source=(HERE/'src/cleanup.py').read_text().rstrip()
    uninstallers={}
    for plugin in ('dockermanager','mihomo','taskcenter','nascenter','sshrecovery','quickshare','downloadcenter','mediaeditor'):
        text=(project/plugin/'manage.sh').read_text()
        common=text.split("cat <<'NAS_COMMON_SCRIPT'\n",1)[1].split('\nNAS_COMMON_SCRIPT',1)[0]
        keys=('PLUGIN_NAME','PLUGIN_LABEL','PLUGIN_VERSION','UNINSTALL_NOTE')
        metadata={key:re.search(r'^'+key+"='(.*)'$",text,re.M).group(1) for key in keys}
        uninstallers[plugin]='#!/bin/sh\nset -eu\n'+'\n'.join(key+'='+shlex.quote(value) for key,value in metadata.items())+'\n'+common+'\nnas_uninstall "${1:-}"\n'
    source=source.replace('UNINSTALLERS = {}','UNINSTALLERS = '+repr(uninstallers))
    wrapper=('#!/bin/sh\n# Generated from cleanup.py; no data migration.\nset -eu\numask 077\n'
             'export PATH="${PATH:+$PATH:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/data/docker"\n'
             'worker=$(mktemp /tmp/minas-manager.XXXXXXXX)\ntrap \'rm -f -- "$worker"\' EXIT\n'
             'cat > "$worker" <<\'MINAS_LAYOUT_PY\'\n'+source+'\nMINAS_LAYOUT_PY\npython3 -B "$worker" "$@"\n')
    (HERE.parents[1]/'migrate-minas-data.sh').write_text(wrapper,encoding='utf-8',newline='\n')
if __name__=='__main__':build()
