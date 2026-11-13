"""Remove empty managed providers without altering other saved configuration."""
import json
import os
import re
import sys
import tempfile
from pathlib import Path


def has_nodes(path):
    try:
        text=Path(path).read_text()
        try:return bool(json.loads(text).get('proxies'))
        except ValueError:
            return bool(re.search(r'^\s*-\s+\S',text,re.M) or re.search(r'^proxies:\s*\[\s*[^\s\]]',text,re.M))
    except OSError:return False


def prune(text, base):
    if not text.startswith('# Managed by the Xiaomi Smart Storage Mihomo plugin.'):return text
    for name,file in [('APP-MANUAL','app-manual.yaml'),('APP-SUBSCRIPTION','app-subscription.yaml')]:
        # Use the path in the candidate as subscription validation can point at
        # a temporary provider which has not been published yet.
        block=re.search(r'^  '+name+r':\n(?:(?: {4}.*|)\n)*',text,re.M)
        if not block:continue
        found=re.search(r'^    path:\s*(.+)$',block[0],re.M)
        value=found[1].strip() if found else './providers/'+file
        try:value=json.loads(value)
        except ValueError:value=value.strip("'\"")
        path=Path(value);path=path if path.is_absolute() else Path(base)/path
        if not has_nodes(path):
            text=text[:block.start()]+text[block.end():]
            text=re.sub(r'^      - '+name+r'\n','',text,flags=re.M)
    text=re.sub(r'^proxy-providers:\n\s*(?=proxy-groups:)', '', text, flags=re.M)
    text=re.sub(r'^    use:\n(?=\s*\nrules:)', '', text, flags=re.M)
    return text


if __name__=='__main__':
    path=Path(sys.argv[1]);old=path.read_text();new=prune(old,Path(sys.argv[2]))
    if new!=old:
        mode=path.stat().st_mode&0o777
        fd,name=tempfile.mkstemp(prefix='.providers-',dir=path.parent)
        try:
            with os.fdopen(fd,'w') as f:f.write(new);f.flush();os.fsync(f.fileno());os.fchmod(f.fileno(),mode)
            os.replace(name,path)
        finally:
            if os.path.exists(name):os.unlink(name)
