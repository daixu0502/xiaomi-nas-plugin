"""Read each standalone plugin's INFO; never require repository-level version data."""
import ast
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ('dockermanager', 'mihomo', 'taskcenter', 'nascenter',
           'sshrecovery', 'quickshare', 'downloadcenter', 'mediaeditor')
COMPONENTS = {
    'privileged': 'workspace-tools/privileged-component/common/privileged_runtime.py',
    'recovery': 'workspace-tools/plugin-recovery/recovery.py',
}


def validate(value, source):
    if not isinstance(value, str) or not re.fullmatch(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)', value):
        raise ValueError('invalid version: ' + str(source))
    return value


def component_version(path):
    # Read the version literal without importing privileged or recovery code.
    tree = ast.parse(path.read_text(encoding='utf-8'))
    values = [ast.literal_eval(node.value) for node in tree.body
              if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == 'VERSION' for target in node.targets)]
    if len(values) != 1:
        raise ValueError('expected one VERSION assignment: ' + str(path))
    return validate(values[0], path)


def load(root=ROOT):
    root = Path(root)
    plugins = {}
    for plugin in PLUGINS:
        path = root / plugin / 'INFO'
        info = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(info, dict) or info.get('plugin') != plugin:
            raise ValueError('INFO plugin mismatch: ' + plugin)
        plugins[plugin] = validate(info.get('version'), path)
    return {'plugins': plugins,
            'components': {name: component_version(root / relative) for name, relative in COMPONENTS.items()}}
