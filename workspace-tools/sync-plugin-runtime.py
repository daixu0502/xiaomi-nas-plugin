"""Distribute the unprivileged runtime utility, without a privileged dependency."""
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
source=(ROOT/'workspace-tools/plugin-runtime/plugin_paths.py').read_bytes().replace(b'\r\n',b'\n')
for name in ('mihomo','quickshare','downloadcenter'):
    (ROOT/name/'src/files/plugin_paths.py').write_bytes(source)
    (ROOT/name/'src/files/service_control.sh').write_bytes((ROOT/'workspace-tools/plugin-runtime/service_control.sh').read_bytes().replace(b'\r\n',b'\n'))
    (ROOT/name/'src/files/runtime_log.py').write_bytes((ROOT/'workspace-tools/plugin-runtime/runtime_log.py').read_bytes().replace(b'\r\n',b'\n'))

(ROOT/'downloadcenter/src/files/event_wait.py').write_bytes((ROOT/'workspace-tools/privileged-component/common/event_wait.py').read_bytes().replace(b'\r\n',b'\n'))
