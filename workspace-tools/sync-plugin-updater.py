"""Distribute the updater and narrow CGI adapter to every standalone package."""
import argparse
from pathlib import Path
import re
from version_catalog import ROOT, load

START = '# BEGIN SHARED ONLINE UPDATE CGI'
END = '# END SHARED ONLINE UPDATE CGI'


def planned():
    changes = {}
    source = ROOT / 'workspace-tools/plugin-updater'
    for plugin in load()['plugins']:
        base = ROOT / plugin
        for name in ('updater.py', 'release-public-key.txt', 'README.md'):
            target = base / 'installer/updater' / name
            data = (source / name).read_bytes().replace(b'\r\n', b'\n')
            if not target.exists() or target.read_bytes() != data:
                changes[target] = data
        target = base / 'src/files/online_update_cgi.py'
        data = (source / target.name).read_bytes().replace(b'\r\n', b'\n')
        if not target.exists() or target.read_bytes() != data:
            changes[target] = data
        cgi = base / 'src/ui' / (plugin + '.cgi')
        text = cgi.read_text(encoding='utf-8').replace('\r\n', '\n')
        text = re.sub(re.escape(START) + r'.*?' + re.escape(END) + r'\n', '', text, flags=re.S)
        shebang, rest = text.split('\n', 1)
        if 'python' in shebang:
            block = "import sys as _update_sys\nfrom pathlib import Path as _UpdatePath\n_update_sys.dont_write_bytecode = True\n_update_sys.path.insert(0, str(_UpdatePath(__file__).resolve().parent.parent / 'files'))\nfrom online_update_cgi import intercept as _online_update_intercept\n_online_update_intercept('" + plugin + "')\n"
        else:
            block = "case \"&${QUERY_STRING:-}&\" in\n    *action=plugin_update_*)\n        update_entry=$(readlink -f \"$0\") || exit 1\n        exec /usr/bin/python3 -B \"$(dirname \"$update_entry\")/../files/online_update_cgi.py\" '" + plugin + "';;\nesac\n"
        value = shebang + '\n' + START + '\n' + block + END + '\n' + rest
        if value != cgi.read_text(encoding='utf-8').replace('\r\n', '\n'):
            changes[cgi] = value.encode()
    return changes


def synchronize(check=False):
    changes = planned()
    if check and changes:
        raise ValueError('Run workspace-tools/sync-plugin-updater.py: ' + ', '.join(str(p.relative_to(ROOT)) for p in changes))
    if not check:
        for path, value in changes.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
    return changes


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    print('Online update distribution: ' + str(len(synchronize(parser.parse_args().check))) + ' changed')
