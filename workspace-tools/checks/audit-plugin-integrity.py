"""Read-only NAS source integrity audit; never prints runtime credentials."""
import hashlib
import json
import os
from pathlib import Path
import stat

NAMES = ('mihomo', 'downloadcenter', 'dockermanager', 'nascenter', 'taskcenter', 'quickshare', 'sshrecovery', 'mediaeditor')
for user_home in sorted(Path('/home').glob('u[0-9]*')):
    registry_path = Path('/data/plugin') / (user_home.name + '.list')
    try:
        registry = json.loads(registry_path.read_text())
    except (OSError, ValueError):
        registry = {}
    for name in NAMES:
        home = user_home / 'plugin' / name
        if not home.exists():
            continue
        source = home / 'src'
        files = []
        for parent, _, names in os.walk(source, followlinks=False):
            for filename in names:
                path = Path(parent) / filename
                if stat.S_ISREG(path.lstat().st_mode):
                    files.append(path)
        digest = hashlib.sha256()
        for path in sorted(files, key=lambda p: os.fsencode(str(p))):
            digest.update((hashlib.sha256(path.read_bytes()).hexdigest() + '\n').encode())
        try:
            info = json.loads((home / 'INFO').read_text())
        except (OSError, ValueError):
            info = {}
        cache = [str(p.relative_to(source)) for p in files if '__pycache__' in p.parts or p.suffix == '.pyc']
        print(json.dumps(dict(user=user_home.name, plugin=name, version=info.get('version'), registered=name in registry,
                             source=str(source.resolve()), exists=source.exists(), files=len(files),
                             match=digest.hexdigest()==info.get('abstract'), calculated=digest.hexdigest(),
                             recorded=info.get('abstract'), bytecode=cache), ensure_ascii=False))
