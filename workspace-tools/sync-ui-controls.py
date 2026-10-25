"""Synchronize shared scoped form controls; generated blocks stay last in each asset."""
import argparse
from pathlib import Path
from version_catalog import ROOT, load

START = '/* BEGIN SHARED FORM CONTROLS */'
END = '/* END SHARED FORM CONTROLS */'

def planned():
    result = {}
    for plugin in load()['plugins']:
        for target, template in [('style.css', 'form-controls.css'), ('client-bridge.js', 'form-controls.js')]:
            path = ROOT / plugin / 'src/ui' / target
            text = path.read_text(encoding='utf-8').replace('\r\n', '\n')
            if START in text:
                before, rest = text.split(START, 1)
                _, after = rest.split(END, 1)
                if after.strip():
                    raise ValueError('Shared controls must be the final block: ' + str(path))
                text = before.rstrip() + '\n'
            block = (ROOT / 'workspace-tools/ui' / template).read_text(encoding='utf-8')
            block = block.replace('@ROOT@', '[data-minas-plugin="' + plugin + '"]')
            value = text.rstrip() + '\n\n' + START + '\n' + block.rstrip() + '\n' + END + '\n'
            if value != path.read_text(encoding='utf-8').replace('\r\n', '\n'):
                result[path] = value
    return result

def synchronize(check=False):
    changes = planned()
    if check and changes:
        raise ValueError('Run workspace-tools/sync-ui-controls.py: ' + ', '.join(str(p.relative_to(ROOT)) for p in changes))
    for path, value in changes.items():
        path.write_text(value, encoding='utf-8', newline='\n')
    return changes

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    print('Shared form control assets: ' + str(len(synchronize(parser.parse_args().check))) + ' changed')
