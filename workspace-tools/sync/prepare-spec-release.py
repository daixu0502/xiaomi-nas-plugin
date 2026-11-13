"""Sync standard-layout metadata, icons and standalone recovery."""
import json
from pathlib import Path
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[2]
from version_catalog import load
from sync_versions import synchronize

def prepare():
    from sync_data_layout import synchronize as sync_layout
    sync_layout()
    synchronize()
    for plugin,version in load()['plugins'].items():
        base=ROOT / 'plugins' / plugin
        assert not (base/'payload').exists(),plugin
        config=json.loads((base/'src/ui/config').read_text(encoding='utf-8'))
        info=json.loads((base/'INFO').read_text(encoding='utf-8'))
        if info['version']!=version:info['timestamp']=int(time.time())
        info.update(plugin=plugin,version=version,name=config['title'],desc=config['desc'],
                    type='standard',system=False,ext={})
        info['size']=sum(p.stat().st_size for p in (base/'src').rglob('*') if p.is_file())
        (base/'INFO').write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
        subprocess.run([sys.executable,'-B',str(ROOT/'workspace-tools/icons'/(plugin+'.py')),str(base/'src/ui/icon.png')],check=True)
if __name__=='__main__':prepare()
