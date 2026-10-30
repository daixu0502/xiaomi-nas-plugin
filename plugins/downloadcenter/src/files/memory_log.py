"""Bounded ordinary logs that can be dropped when the RAM budget is full."""
import errno,sys,threading
from runtime_log import LogStream

class MemoryLog(LogStream):
    def __init__(self,path):super().__init__(path,maximum=1024*1024,backups=1)
    def write(self,value):
        try:return super().write(value)
        except OSError as error:
            if error.errno!=errno.ENOSPC:raise
            return len(value)

def redirect(path):sys.stdout=sys.stderr=MemoryLog(path)

def drain(stream,path):
    sink=MemoryLog(path)
    def copy():
        try:
            while True:
                data=stream.read1(8192)
                if not data:break
                sink.write(data)
        finally:stream.close()
    thread=threading.Thread(target=copy,daemon=True);thread.start();return thread
