"""Find only this user's quickshare server with the exact plugin state path."""
import os
from pathlib import Path
import sys


def managed_pids(home):
    home = Path(home)
    script = (home / 'src/files/quickshare_server.py').resolve()
    state = (home / 'var').resolve()
    for process in Path('/proc').glob('[0-9]*'):
        try:
            if process.stat().st_uid != os.geteuid():
                continue
            args = [os.fsdecode(v) for v in (process / 'cmdline').read_bytes().split(b'\0') if v]
            if '--state' not in args:
                continue
            index = args.index('--state')
            if index < 1 or index + 1 >= len(args):
                continue
            if Path(args[index - 1]).resolve() != script or Path(args[index + 1]).resolve() != state:
                continue
            os.kill(int(process.name), 0)
            yield int(process.name)
        except (OSError, ValueError):
            continue


if __name__ == '__main__':
    values = list(managed_pids(sys.argv[1]))
    if values:
        print('\n'.join(map(str, values)))
    else:
        sys.exit(1)
