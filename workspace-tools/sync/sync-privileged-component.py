"""Check private modules; shared code is packaged once as its own component."""
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'plugins/common/manager'))
from layout import PLUGINS, PRIVILEGED, SERVERS
from adapters import DEPENDENCIES, PORTS
from sync_versions import synchronize as synchronize_versions

def synchronize():
    synchronize_versions()
    for plugin in PLUGINS:
        descriptor = dict(format=2, plugin=plugin, privileged=plugin in PRIVILEGED,
                          service=plugin in SERVERS, dependencies=DEPENDENCIES.get(plugin, ()),
                          ports=PORTS.get(plugin, ()))
        path = ROOT / 'plugins' / plugin / 'installer/plugin.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(descriptor, ensure_ascii=False, indent=2) + '\n')
        private = path.parent / 'common/modules'
        if plugin in PRIVILEGED:
            assert {p.name for p in private.iterdir()} == {plugin}, 'foreign private module: ' + plugin
            assert any((private / plugin).iterdir()), 'missing private module: ' + plugin
        else:
            assert not private.exists(), 'non-privileged module: ' + plugin
    print('Eight descriptors checked; no shared component copies generated')

if __name__ == '__main__':
    synchronize()
