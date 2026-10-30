"""Bound parameters through system SQLite; retain the firmware's FTS tokenizer."""
import ctypes as C
import ctypes.util
import contextlib
from pathlib import Path

LIBRARY = None


def library():
    global LIBRARY
    if LIBRARY is not None:
        return LIBRARY
    lib = C.CDLL(C.util.find_library('sqlite3') or 'libsqlite3.so.0')
    ptr, integer, text = C.c_void_p, C.c_int, C.c_char_p
    signatures = {
        'sqlite3_open_v2': ([text, C.POINTER(ptr), integer, text], integer),
        'sqlite3_close': ([ptr], integer),
        'sqlite3_errmsg': ([ptr], text),
        'sqlite3_prepare_v2': ([ptr, text, integer, C.POINTER(ptr), C.POINTER(text)], integer),
        'sqlite3_finalize': ([ptr], integer),
        'sqlite3_step': ([ptr], integer),
        'sqlite3_bind_text': ([ptr, integer, text, integer, ptr], integer),
        'sqlite3_bind_int64': ([ptr, integer, C.c_longlong], integer),
        'sqlite3_bind_double': ([ptr, integer, C.c_double], integer),
        'sqlite3_bind_null': ([ptr, integer], integer),
        'sqlite3_column_count': ([ptr], integer),
        'sqlite3_column_name': ([ptr, integer], text),
        'sqlite3_column_type': ([ptr, integer], integer),
        'sqlite3_column_int64': ([ptr, integer], C.c_longlong),
        'sqlite3_column_double': ([ptr, integer], C.c_double),
        'sqlite3_column_text': ([ptr, integer], text),
        'sqlite3_changes': ([ptr], integer),
        'sqlite3_busy_timeout': ([ptr, integer], integer),
        'sqlite3_enable_load_extension': ([ptr, integer], integer),
        'sqlite3_load_extension': ([ptr, text, text, C.POINTER(ptr)], integer),
        'sqlite3_free': ([ptr], None),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(lib, name); fn.argtypes = args; fn.restype = result
    LIBRARY = lib
    return lib


class Database:
    def __init__(self, path, writable=False, extension=None):
        self.lib = library()
        self.handle = C.c_void_p()
        rc = self.lib.sqlite3_open_v2(str(Path(path)).encode(), C.byref(self.handle), (2 if writable else 1) | 64, None)
        if rc:
            self.close()
            raise ValueError('无法打开现有官方媒体库')
        self.lib.sqlite3_busy_timeout(self.handle, 5000)
        try:
            if extension:
                error = C.c_void_p()
                self.lib.sqlite3_enable_load_extension(self.handle, 1)
                try:
                    rc = self.lib.sqlite3_load_extension(self.handle, str(extension).encode(), None, C.byref(error))
                    if error: self.lib.sqlite3_free(error)
                    if rc: raise ValueError('无法加载官方搜索分词扩展，已拒绝写入')
                finally:
                    self.lib.sqlite3_enable_load_extension(self.handle, 0)
            if not writable: self.query('PRAGMA query_only=ON')
            self.query('PRAGMA foreign_keys=ON')
        except BaseException:
            self.close(); raise

    def query(self, sql, values=()):
        statement = C.c_void_p()
        rc = self.lib.sqlite3_prepare_v2(self.handle, sql.encode(), -1, C.byref(statement), None)
        if rc:
            if statement: self.lib.sqlite3_finalize(statement)
            raise ValueError('媒体库操作失败：' + self.lib.sqlite3_errmsg(self.handle).decode(errors='replace'))
        try:
            for index, value in enumerate(values, 1):
                if value is None: rc = self.lib.sqlite3_bind_null(statement, index)
                elif isinstance(value, (int, bool)): rc = self.lib.sqlite3_bind_int64(statement, index, int(value))
                elif isinstance(value, float): rc = self.lib.sqlite3_bind_double(statement, index, value)
                elif isinstance(value, str):
                    raw = value.encode()
                    rc = self.lib.sqlite3_bind_text(statement, index, raw, len(raw), C.c_void_p(-1))
                else: raise ValueError('不支持的媒体库参数类型')
                if rc: raise ValueError('媒体库参数绑定失败')
            columns = self.lib.sqlite3_column_count(statement)
            names = [self.lib.sqlite3_column_name(statement, i).decode() for i in range(columns)]
            rows = []
            while True:
                rc = self.lib.sqlite3_step(statement)
                if rc == 101: break
                if rc != 100: raise ValueError('媒体库操作失败：' + self.lib.sqlite3_errmsg(self.handle).decode(errors='replace'))
                row = {}
                for i, name in enumerate(names):
                    kind = self.lib.sqlite3_column_type(statement, i)
                    if kind == 5: value = None
                    elif kind == 1: value = self.lib.sqlite3_column_int64(statement, i)
                    elif kind == 2: value = self.lib.sqlite3_column_double(statement, i)
                    else:
                        raw = self.lib.sqlite3_column_text(statement, i)
                        value = raw.decode(errors='replace') if raw is not None else None
                    row[name] = value
                rows.append(row)
            return rows
        finally:
            self.lib.sqlite3_finalize(statement)

    def one(self, sql, values=()):
        rows = self.query(sql, values)
        return rows[0] if rows else None

    @contextlib.contextmanager
    def restoration_triggers(self):
        """Only exact, guarded undo suppresses triggers; forward edits use firmware triggers."""
        fn = self.lib.sqlite3_db_config
        fn.argtypes = [C.c_void_p, C.c_int, C.c_int, C.POINTER(C.c_int)]; fn.restype = C.c_int
        previous = C.c_int(); result = C.c_int()
        if fn(self.handle, 1003, -1, C.byref(previous)) or fn(self.handle, 1003, 0, C.byref(result)):
            raise ValueError('系统 SQLite 不支持受保护的关联回退')
        try: yield
        finally:
            if fn(self.handle, 1003, previous.value, C.byref(result)): raise ValueError('恢复数据库触发器状态失败')

    def close(self):
        if getattr(self, 'handle', None):
            self.lib.sqlite3_close(self.handle); self.handle = None

    def __enter__(self): return self

    def __exit__(self, *args): self.close()
