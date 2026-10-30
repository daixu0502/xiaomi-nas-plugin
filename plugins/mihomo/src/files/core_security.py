#!/usr/bin/env python3
"""Reject Go builds predating the October 2026 TLS/x509 security releases."""
import re
import sys


def verify(text):
    match = re.search(r'\bgo(\d+)\.(\d+)\.(\d+)(?=\s|$)', text)
    if not match:
        raise ValueError('无法确认内核的正式 Go 编译版本，已停止安装或更新')
    version = tuple(map(int, match.groups()))
    if version[:2] == (1, 26):
        safe = version[2] >= 10
    elif version[:2] == (1, 27):
        safe = version[2] >= 3
    else:
        safe = version >= (1, 28, 0)
    if not safe:
        raise ValueError('内核使用 Go %s，未达到安全基线；需 Go 1.26.10、1.27.3 或更新正式版（CVE-2026-111943）' % '.'.join(map(str, version)))
    return '.'.join(map(str, version))


if __name__ == '__main__':
    try:
        verify(sys.stdin.read(8192))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
