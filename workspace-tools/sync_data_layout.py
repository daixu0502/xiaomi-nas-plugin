"""Embed current-layout initialization; old paths belong only to the cleanup tool."""
from pathlib import Path
import re
ROOT=Path(__file__).resolve().parents[1]
PLUGINS=('dockermanager','mihomo','taskcenter','nascenter','sshrecovery','quickshare','downloadcenter','mediaeditor')
def synchronize():
    source=(ROOT/'workspace-tools/data-layout/scoped_common.py').read_text()
    for name in ('workspace-tools/privileged-component/common/privileged_runtime.py','workspace-tools/plugin-recovery/recovery.py','nas-root/tools/docker-upgrade/src/docker_runtime_upgrade.py','nas-root/tools/docker-bridge/src/docker_bridge_repair.py'):
        p=ROOT/name;text=p.read_text()
        text,count=re.subn(r'^# BEGIN MINAS DATA LAYOUT \(generated\)\n.*?^# END MINAS DATA LAYOUT\n',lambda m:'# BEGIN MINAS DATA LAYOUT (generated)\n'+source+'# END MINAS DATA LAYOUT\n',text,flags=re.M|re.S)
        assert count==1,name;p.write_text(text,encoding='utf-8',newline='\n')
    for name in PLUGINS:
        (ROOT/name/'installer/data_layout.py').write_text(source+"\nif __name__=='__main__':ensure_data_layout()\n",encoding='utf-8',newline='\n')
if __name__=='__main__':synchronize()
