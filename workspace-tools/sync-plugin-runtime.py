"""Distribute the unprivileged runtime utility, without a privileged dependency."""
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
source=(ROOT/'workspace-tools/plugin-runtime/plugin_paths.py').read_bytes().replace(b'\r\n',b'\n')
for name in ('mihomo','quickshare','downloadcenter'):
    (ROOT/name/'src/files/plugin_paths.py').write_bytes(source)
