"""Local tests stay on disk, never in a plugin's published Git tree."""
from pathlib import Path, PurePosixPath
import re
import subprocess


def is_test_path(name):
    path=PurePosixPath(name.replace('\\','/'))
    if path.suffix.lower() == '.md':
        return False
    return any(part in ('tests','__tests__') for part in path.parts) or bool(
        re.fullmatch(r'test_.*\.py|.*_test\.go|.*\.(?:test|spec)\.(?:py|js|jsx|ts|tsx|cjs|mjs)',path.name))


def assert_no_test_content(repo):
    repo=Path(repo)
    result=subprocess.run(['git','-c','safe.directory='+repo.as_posix(),'-C',str(repo),'ls-files','-z'],capture_output=True,check=True)
    paths=[p.decode('utf-8') for p in result.stdout.split(b'\0') if p]
    forbidden=[name for name in paths if is_test_path(name)]
    if forbidden:
        raise ValueError('测试文件仍被 Git 跟踪，拒绝发布；仅用 git rm --cached 取消跟踪并保留本地文件：'+', '.join(forbidden))
