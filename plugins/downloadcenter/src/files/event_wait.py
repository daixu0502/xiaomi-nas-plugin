"""Linux event wait: parent-directory changes, mounts and real-time deadlines.

No polling stat/read of watched files. Paths are selected by trusted callers.
"""
import ctypes
import errno
import os
import select
import struct
import time


class Timespec(ctypes.Structure):
    _fields_ = [('seconds', ctypes.c_long), ('nanos', ctypes.c_long)]


class TimerSpec(ctypes.Structure):
    _fields_ = [('interval', Timespec), ('value', Timespec)]


class Events:
    def __init__(self):
        self.lib = ctypes.CDLL(None, use_errno=True)
        self.fd = self.lib.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        self.timer = self.lib.timerfd_create(0, os.O_NONBLOCK | os.O_CLOEXEC)
        self.mount = os.open('/proc/self/mounts', os.O_RDONLY | os.O_CLOEXEC)
        self.pipe = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0 or self.timer < 0: raise OSError(ctypes.get_errno(), 'event descriptors unavailable')
        self.poller = select.poll()
        for fd in (self.fd, self.timer, self.pipe[0]): self.poller.register(fd, select.POLLIN)
        self.poller.register(self.mount, select.POLLPRI | select.POLLERR)
        self.watches = {}
        self.filters = {}

    def watch(self, path, names=None):
        path = os.fspath(path)
        self.filters[path] = names
        if path in self.watches.values(): return
        wd = self.lib.inotify_add_watch(self.fd, os.fsencode(path), 0x2 | 0x80 | 0x100 | 0x200 | 0x400 | 0x800)
        if wd < 0:
            error = ctypes.get_errno()
            if error in (errno.ENOENT, errno.ENOTDIR): return
            raise OSError(error, 'cannot watch directory')
        self.watches[wd] = path

    def wake(self):
        try: os.write(self.pipe[1], b'x')
        except BlockingIOError: pass

    def wait(self, deadline=None, timeout=None):
        # CANCEL_ON_SET wakes the scheduler when CLOCK_REALTIME jumps.
        stamp = deadline if deadline is not None else time.time()+10*365*86400
        stamp = max(time.time()+.001, stamp)
        spec = TimerSpec(Timespec(0,0), Timespec(int(stamp),int(stamp%1*1e9)))
        if self.lib.timerfd_settime(self.timer, 1|2, ctypes.byref(spec), None):
            raise OSError(ctypes.get_errno(), 'cannot arm deadline')
        until=None if timeout is None else time.monotonic()+timeout
        while True:
            changed = set()
            for fd, flags in self.poller.poll(None if until is None else max(0,int((until-time.monotonic())*1000))):
                if fd == self.mount:
                    os.lseek(fd,0,0);os.read(fd,1024*1024);changed.add(('mount',''))
                elif fd == self.fd:
                    while True:
                        try: data = os.read(fd,65536)
                        except BlockingIOError: break
                        offset = 0
                        while offset < len(data):
                            wd,mask,cookie,size = struct.unpack_from('iIII',data,offset)
                            name = os.fsdecode(data[offset+16:offset+16+size].split(b'\0',1)[0])
                            path=self.watches.get(wd,'')
                            names=self.filters.get(path)
                            if names is None or name in names or not name:changed.add((path,name))
                            if mask & (0x8000 | 0x400 | 0x800): self.watches.pop(wd,None)
                            if mask & 0x4000: changed.add(('overflow',''))
                            offset += 16+size
                else:
                    try: os.read(fd,65536)
                    except OSError as exc:
                        if exc.errno not in (errno.EAGAIN,errno.ECANCELED): raise
                        if exc.errno==errno.ECANCELED:changed.add(('clock',''))
                    changed.add(('deadline' if fd==self.timer else 'wake',''))
            if changed or (until is not None and time.monotonic()>=until):return changed

    def close(self):
        for fd in (self.fd,self.timer,self.mount,*self.pipe): os.close(fd)

    def __enter__(self): return self
    def __exit__(self,*args): self.close()
