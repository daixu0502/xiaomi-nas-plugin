"""Distribute the unprivileged runtime utility, without a privileged dependency."""
from pathlib import Path
import re
ROOT=Path(__file__).resolve().parents[2]
storage=(ROOT/'plugins/common/runtime/storage_paths.py').read_text().rstrip()+'\n'
block='# BEGIN SHARED STORAGE PATHS\n'+storage+'# END SHARED STORAGE PATHS\n'
for relative in ('plugins/common/runtime/plugin_paths.py',
                 'plugins/common/privileged/common/privileged_runtime.py',
                 'plugins/sshrecovery/installer/common/modules/sshrecovery/ssh-recovery-helper'):
    path=ROOT/relative;text=path.read_text()
    if '# BEGIN SHARED STORAGE PATHS' in text:
        text,count=re.subn(r'^# BEGIN SHARED STORAGE PATHS\n.*?^# END SHARED STORAGE PATHS\n',lambda m:block,text,flags=re.M|re.S)
        assert count==1,relative
    else:
        marker='\n\ndef ' if relative.endswith('plugin_paths.py') else '\n\nclass '
        position=text.index(marker)
        text=text[:position]+'\n\n'+block+text[position:]
    path.write_text(text,encoding='utf-8',newline='\n')
source=(ROOT/'plugins/common/runtime/plugin_paths.py').read_bytes().replace(b'\r\n',b'\n')
for name in ('mihomo','quickshare','downloadcenter'):
    (ROOT/'plugins'/name/'src/files/plugin_paths.py').write_bytes(source)
    (ROOT/'plugins'/name/'src/files/service_control.sh').write_bytes((ROOT/'plugins/common/runtime/service_control.sh').read_bytes().replace(b'\r\n',b'\n'))
    (ROOT/'plugins'/name/'src/files/runtime_log.py').write_bytes((ROOT/'plugins/common/runtime/runtime_log.py').read_bytes().replace(b'\r\n',b'\n'))

(ROOT/'plugins/downloadcenter/src/files/event_wait.py').write_bytes((ROOT/'plugins/common/privileged/common/event_wait.py').read_bytes().replace(b'\r\n',b'\n'))
