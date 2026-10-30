"""Distribute the unprivileged runtime utility, without a privileged dependency."""
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
source=(ROOT/'plugins/common/runtime/plugin_paths.py').read_bytes().replace(b'\r\n',b'\n')
for name in ('mihomo','quickshare','downloadcenter'):
    (ROOT/'plugins'/name/'src/files/plugin_paths.py').write_bytes(source)
    (ROOT/'plugins'/name/'src/files/service_control.sh').write_bytes((ROOT/'plugins/common/runtime/service_control.sh').read_bytes().replace(b'\r\n',b'\n'))
    (ROOT/'plugins'/name/'src/files/runtime_log.py').write_bytes((ROOT/'plugins/common/runtime/runtime_log.py').read_bytes().replace(b'\r\n',b'\n'))

(ROOT/'plugins/downloadcenter/src/files/event_wait.py').write_bytes((ROOT/'plugins/common/privileged/common/event_wait.py').read_bytes().replace(b'\r\n',b'\n'))
