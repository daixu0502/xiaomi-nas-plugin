"""Bounded user-owned logs; rotate writers while the service keeps running."""
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path


class LogStream:
    def __init__(self, path, maximum=2*1024*1024, backups=2):
        self.path=Path(path);self.maximum=maximum;self.backups=backups
        self.lock=threading.Lock();self.encoding='utf-8'

    def write(self, value):
        data=value.encode('utf-8',errors='replace') if isinstance(value,str) else value
        with self.lock:
            for offset in range(0,len(data),self.maximum):
                part=data[offset:offset+self.maximum]
                if self.path.is_symlink():raise OSError('redirected log')
                if self.path.exists() and self.path.stat().st_size+len(part)>self.maximum:
                    for n in range(self.backups,0,-1):
                        target=self.path.with_name(self.path.name+'.'+str(n))
                        source=self.path if n==1 else self.path.with_name(self.path.name+'.'+str(n-1))
                        if target.is_symlink() or source.is_symlink():raise OSError('redirected log')
                        if source.exists():os.replace(source,target)
                fd=os.open(self.path,os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'ab') as output:output.write(part)
        return len(value)

    def flush(self):pass
    def isatty(self):return False


def redirect(path):
    stream=LogStream(path);sys.stdout=stream;sys.stderr=stream
    return stream


def drain(stream, path):
    log=LogStream(path)
    def copy():
        try:
            while True:
                chunk=stream.read1(8192)
                if not chunk:break
                log.write(chunk)
        finally:stream.close()
    thread=threading.Thread(target=copy,daemon=True);thread.start();return thread


def run(log, pidfile, command):
    if os.geteuid()==0:raise RuntimeError('log relay requires plugin user')
    readfd,writefd=os.pipe()
    if os.fork()==0:
        os.close(writefd)
        try:
            sink=LogStream(log)
            while True:
                chunk=os.read(readfd,8192)
                if not chunk:break
                sink.write(chunk)
        finally:os.close(readfd)
        os._exit(0)
    os.close(readfd)
    Path(pidfile).write_text(str(os.getpid())+'\n')
    os.dup2(writefd,1);os.dup2(writefd,2);os.close(writefd)
    # Replace the launcher so the actual core is reparented to systemd after
    # control exits. Keeping a Python parent would hide the core's SIGCHLD.
    os.execvpe(command[0],command,os.environ)


if __name__=='__main__':
    os.umask(0o077)
    assert sys.argv[1]=='run' and sys.argv[4]=='--'
    sys.exit(run(sys.argv[2],sys.argv[3],sys.argv[5:]))
