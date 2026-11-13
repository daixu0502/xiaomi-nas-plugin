"""Download-only RAM runtime; persistent queues and core staging stay separate."""
import os,ctypes
from pathlib import Path
from plugin_paths import runtime_directory as official_runtime

def runtime_directory(home):
    path=Path(home)/'tmp/runtime'
    if str(home).startswith('/home/'):
        try:fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        except FileNotFoundError:raise RuntimeError('内存运行目录未就绪，请启动服务或重新安装')
        try:
            buf=ctypes.create_string_buffer(256)
            if ctypes.CDLL(None).fstatfs(fd,buf) or ctypes.c_long.from_buffer(buf).value!=0x01021994:
                raise RuntimeError('内存运行目录未就绪，请启动服务或重新安装；禁止回落到机械盘')
        finally:os.close(fd)
    return official_runtime(home)

def update_staging(home):
    runtime_directory(home)
    path=Path(home)/'tmp/update-staging'
    if path.is_symlink():raise PermissionError('更新暂存不能为链接')
    path.mkdir(mode=0o700,exist_ok=True)
    info=path.stat()
    if info.st_uid!=os.geteuid() or info.st_mode&0o077:raise PermissionError('更新暂存权限异常')
    return path
