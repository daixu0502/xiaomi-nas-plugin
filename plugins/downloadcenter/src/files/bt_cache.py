"""Manage plugin-generated seeds only; protect tasks and engine resume data."""
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from download_lib import DownloadError, torrent_info

HASH = r'[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?'
KEY = re.compile(r'(?:metadata/[a-f0-9]{24}|qb)/' + HASH + r'\.torrent')


@contextlib.contextmanager
def directory(m, parts):
    # var is the installer-managed persistent link. Never follow child links.
    if os.geteuid() == 0:
        raise DownloadError('种子缓存必须以插件所属用户身份访问')
    from plugin_paths import open_state_directory
    fd = open_state_directory(m.home)
    try:
        if os.fstat(fd).st_uid != os.geteuid():
            raise DownloadError('插件状态目录所属用户不正确')
        for name in parts:
            try: child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                yield None
                return
            os.close(fd); fd = child
            if os.fstat(fd).st_uid != os.geteuid():
                raise DownloadError('种子缓存目录所属用户不正确')
        yield fd
    finally: os.close(fd)


def location(key):
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise DownloadError('种子缓存编号无效')
    parts = key.split('/')
    parent = parts[:-1] if parts[0] == 'metadata' else ['qb-profile', 'qBittorrent', 'data', 'BT_backup']
    return parent, parts[-1]


def version(info):
    return hashlib.sha256(repr((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)).encode()).hexdigest()


def inspect(fd, name, expected=None):
    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise DownloadError('种子缓存不是当前用户的独立普通文件')
    if expected is not None and (not isinstance(expected, str) or expected != version(info)):
        raise DownloadError('文件已改变，请刷新种子列表后重试')
    return info


def ownership(m):
    if os.geteuid() == 0:
        raise DownloadError('种子缓存必须以插件所属用户身份访问')
    tasks, hashes = {}, {}
    for row in m.rows():
        snap = json.loads(row['snapshot'])
        label = snap.get('name') or row['id']
        tasks[row['id']] = label
        for value in (row.get('gid'), snap.get('infoHash'), snap.get('torrentHash')):
            if isinstance(value, str) and re.fullmatch(HASH, value):
                hashes[value.lower()] = label
                hashes[value[:40].lower()] = label
    verified = True
    try:
        aria = m.rpc('tellActive') + m.rpc('tellWaiting', 0, 10000)
        if len(aria) >= 10000: raise DownloadError('核心任务过多')
        for job in aria:
            for path in [job.get('dir', '')] + [f.get('path', '') for f in job.get('files', [])]:
                for base in (m.var / 'metadata', (m.var / 'metadata').resolve()):
                    try:
                        parts = Path(path).relative_to(base).parts
                        if parts: tasks[parts[0]] = 'aria2 核心任务'
                    except ValueError: pass
            value = job.get('infoHash', '')
            if re.fullmatch(HASH, value): hashes[value.lower()] = 'aria2 核心任务'
        for job in m.qb.torrents():
            for field in ('hash', 'infohash_v1', 'infohash_v2'):
                value = job.get(field, '')
                if isinstance(value, str) and re.fullmatch(HASH, value):
                    hashes[value.lower()] = job.get('name') or 'qB 核心任务'
                    hashes[value[:40].lower()] = hashes[value.lower()]
            for base in (m.var / 'metadata', (m.var / 'metadata').resolve()):
                try:
                    parts = Path(job.get('save_path', '')).relative_to(base).parts
                    if parts: tasks[parts[0]] = job.get('name') or 'qB 核心任务'
                except ValueError: pass
    except (DownloadError, OSError, ValueError, TypeError, KeyError):
        verified = False
    return tasks, hashes, verified


def protection(key, fd, owned):
    tasks, hashes, verified = owned
    parts = key.split('/'); name = parts[-1]; stem = name[:-8].lower()
    label = tasks.get(parts[1], '') if parts[0] == 'metadata' else ''
    label = label or hashes.get(stem, '')
    if label: return '任务使用中：' + label
    companions = (name + '.aria2', name + '.tmp') if parts[0] == 'metadata' else (name[:-8] + '.fastresume',)
    for companion in companions:
        try: os.stat(companion, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError: continue
        return '关联续传或写入状态，已保护'
    if not verified: return '下载核心未就绪，暂不能确认占用'
    return ''


def listing(m):
    owned = ownership(m)
    result = {'files': [], 'bytes': 0, 'skipped': 0, 'partial': False, 'verified': owned[2]}
    folders = [('qb', ['qb-profile', 'qBittorrent', 'data', 'BT_backup'])]
    with directory(m, ['metadata']) as fd:
        if fd is not None:
            with os.scandir(fd) as entries:
                for count, entry in enumerate(entries):
                    if count >= 2000: result['partial'] = True; break
                    if re.fullmatch(r'[a-f0-9]{24}', entry.name) and entry.is_dir(follow_symlinks=False):
                        folders.append(('metadata/' + entry.name, ['metadata', entry.name]))
                    else: result['skipped'] += 1
    visited = 0
    for prefix, parts in folders:
        try:
            with directory(m, parts) as fd:
                if fd is None: continue
                with os.scandir(fd) as entries:
                    for entry in entries:
                        visited += 1
                        if visited > 2000: result['partial'] = True; break
                        key = prefix + '/' + entry.name
                        if not KEY.fullmatch(key): continue
                        try: info = inspect(fd, entry.name)
                        except (DownloadError, OSError): result['skipped'] += 1; continue
                        reason = protection(key, fd, owned)
                        result['files'].append({'key': key, 'name': entry.name, 'size': info.st_size,
                            'modified': info.st_mtime, 'version': version(info), 'protected': bool(reason),
                            'reason': reason, 'source': 'qB 种子备份' if prefix == 'qb' else '磁链元数据'})
                        result['bytes'] += info.st_size
        except OSError:
            result['skipped'] += 1
        if result['partial'] and visited > 2000: break
    result['files'].sort(key=lambda row: (row['protected'], row['key']))
    return result


def preview(m, data):
    parts, name = location(data.get('key'))
    with directory(m, parts) as fd:
        if fd is None: raise DownloadError('种子缓存目录不存在，请刷新')
        before = inspect(fd, name, data.get('version', ''))
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(handle, 'rb') as stream:
            if version(os.fstat(stream.fileno())) != version(before): raise DownloadError('文件已改变，请刷新')
            raw = stream.read(4 * 1024 * 1024 + 1)
            if version(os.fstat(stream.fileno())) != version(before): raise DownloadError('文件已改变，请刷新')
    return dict(torrent_info(raw), torrent=base64.b64encode(raw).decode(), filename=name)


def delete(m, data):
    parts, name = location(data.get('key'))
    owned = ownership(m)  # Recheck under api.lock; never trust the list's badge.
    with directory(m, parts) as fd:
        if fd is None: raise DownloadError('种子缓存目录不存在，请刷新')
        info = inspect(fd, name, data.get('version', ''))
        reason = protection(data['key'], fd, owned)
        if reason: raise DownloadError(reason)
        os.unlink(name, dir_fd=fd)
        os.fsync(fd)
    return {'files': 1, 'bytes': info.st_size}
