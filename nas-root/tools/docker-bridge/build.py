#!/usr/bin/env python3
"""Build the standalone Docker bridge repair entry point."""
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def build_bridge_entrypoint():
    source = (HERE / 'src/docker_bridge_repair.py').read_text(encoding='utf-8').rstrip()
    wrapper = ('#!/bin/sh\n# Generated from docker_bridge_repair.py; edit the Python source and rebuild.\n'
               'set -eu\numask 077\ncommand -v python3 >/dev/null 2>&1 || { echo "缺少 python3" >&2; exit 2; }\n'
               'worker=$(mktemp /tmp/minas-docker-bridge.XXXXXXXX)\n'
               'trap \'rm -f -- "$worker"\' EXIT\n'
               'cat > "$worker" <<\'MINAS_DOCKER_BRIDGE_PY\'\n' + source + '\nMINAS_DOCKER_BRIDGE_PY\n'
               'python3 -B "$worker" "$@"\n')
    (ROOT / 'fix-docker-bridge.sh').write_text(wrapper, encoding='utf-8', newline='\n')


if __name__ == '__main__':
    build_bridge_entrypoint()
