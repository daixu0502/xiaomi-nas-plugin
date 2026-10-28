"""Synchronize shared scoped form controls; generated blocks stay last in each asset."""
import argparse
import re
from pathlib import Path
from version_catalog import ROOT, load

START = '/* BEGIN SHARED FORM CONTROLS */'
END = '/* END SHARED FORM CONTROLS */'

def planned():
    result = {}
    for plugin in load()['plugins']:
        base = ROOT / plugin / 'src/ui'
        # Some plugins load palette after style, others do the reverse. The
        # typography contract belongs in the last local stylesheet in either case.
        sheets = re.findall(r'href="([^"?:/]+\.css)(?:\?[^"<>]*)?"', (base/'index.html').read_text(encoding='utf-8'))
        if not sheets: raise ValueError('No local stylesheet: '+plugin)
        targets = {'style.css':[('SHARED FORM CONTROLS','form-controls.css')],
                   'client-bridge.js':[('SHARED FORM CONTROLS','form-controls.js')]}
        targets.setdefault(sheets[-1],[]).append(('SHARED TYPOGRAPHY','typography.css'))
        if re.search(r'class="[^"<>]*\btabs\b[^"<>]*"',(base/'index.html').read_text(encoding='utf-8')):
            targets[sheets[-1]].append(('SHARED MOBILE NAVIGATION','mobile-navigation.css'))
            targets['client-bridge.js'].append(('SHARED MOBILE NAVIGATION','mobile-navigation.js'))
        targets[sheets[-1]].append(('SHARED WORKBENCH','workbench.css'))
        targets['client-bridge.js'].append(('SHARED WORKBENCH','workbench.js'))
        for target, templates in targets.items():
            path = ROOT / plugin / 'src/ui' / target
            text = path.read_text(encoding='utf-8').replace('\r\n', '\n')
            for marker in ('SHARED FORM CONTROLS','SHARED TYPOGRAPHY','SHARED MOBILE NAVIGATION','SHARED WORKBENCH'):
                start='/* BEGIN '+marker+' */';end='/* END '+marker+' */'
                if start in text:
                    before,rest=text.split(start,1);_,after=rest.split(end,1)
                    text=before.rstrip()+'\n'+after.lstrip('\n')
            value=text.rstrip()+'\n'
            for marker,template in templates:
                block=(ROOT/'workspace-tools/ui'/template).read_text(encoding='utf-8')
                block=block.replace('@ROOT@','[data-minas-plugin="'+plugin+'"]')
                value+='\n/* BEGIN '+marker+' */\n'+block.rstrip()+'\n/* END '+marker+' */\n'
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
