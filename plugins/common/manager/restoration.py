"""Recovery mutations owned by the common transaction engine."""
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time


def restore_component(r, saved, user, plugin, transaction=None):
    payload = saved if (saved / 'scripts').is_dir() else saved / 'payload'
    updater = payload / 'installer/updater'
    if updater.is_dir():
        rule = Path('/etc/sudoers.d') / ('minas-update-' + user + '-' + plugin)
        if not r.plain(rule).is_file() or not r.plain(Path('/data/minas/plugins/updater/updater.py')).is_file():
            r.call([sys.executable, '-I', '-B', updater / 'updater.py', 'bootstrap', '--user', user, '--plugin', plugin, '--payload', payload])
    common = payload / 'installer/common'
    if not common.exists(): common = payload / 'common'
    admin = common / 'component_admin.py'
    if not admin.is_file():
        return
    required = (Path('/data/minas/privileged/minas-helper'),
                Path('/data/minas/privileged/registry.json'),
                Path('/etc/sudoers.d') / (plugin + '-' + user), Path('/etc/cron.d/minas-privileged-v2'))
    manifest = r.read(common / 'manifest.json')
    code_present = all(r.plain(Path('/data/minas/privileged') / name).is_file() for name in manifest['files'])
    if code_present and all(r.plain(path).is_file() for path in required):
        grants = r.read(Path('/data/minas/privileged/registry.json')).get('users', {}).get(user, {})
        if plugin in grants:
            return
    # Reapply missing sudo/cron entries from a root-owned package. Existing root
    # grants, task state and SSH recovery material are owned by component_admin.
    r.call([sys.executable, '-B', admin, 'install', '--plugin', plugin, '--user', user,
          '--payload', payload, '--plugin-version', r.read(saved / 'INFO')['version'],
          *(['--manager-transaction', transaction] if transaction else [])])


def restore_missing_source(r, src, backup, owner):
    """Complete partial cleanup; preserve conflicting local edits for inspection."""
    for path in src.rglob('*'):
        expected = backup / path.relative_to(src)
        r.require(expected.exists() or expected.is_symlink(), 'extra source file: ' + str(path))
        if path.is_symlink():
            r.require(expected.is_symlink() and os.readlink(path) == os.readlink(expected), 'changed source link')
        elif path.is_file():
            r.require(expected.is_file() and not expected.is_symlink() and path.read_bytes() == expected.read_bytes(),
                    'source changed; refusing silent rollback')
        else:
            r.require(path.is_dir() and expected.is_dir(), 'invalid source node')
    for expected in sorted(backup.rglob('*'), key=lambda p: len(p.parts)):
        path = src / expected.relative_to(backup)
        if path.exists() or path.is_symlink() or expected.is_symlink():
            continue
        if expected.is_dir():
            r.plain(path).mkdir()
            os.chown(path, *owner)
            path.chmod(stat.S_IMODE(expected.stat().st_mode))
        else:
            r.atomic(path, expected.read_bytes(), stat.S_IMODE(expected.stat().st_mode), owner)


def restore_locked_record(r, directory, intents=None, check_source=True, transaction=None):
    if (directory / 'removed').exists():
        return
    saved = r.load_generation(directory)
    record = r.read(saved / 'record.json')
    user, plugin = record['user'], record['plugin']
    home, owner = r.identity(user, plugin)
    r.require(directory.name == user + '-' + plugin, 'record identity mismatch')
    r.apply_native_intent(record, (r.native_intents() if intents is None else intents).get((user, plugin)))
    r.apply_service_intent(record, directory)
    src = Path(record['src'])
    match = re.fullmatch(r'/nas/(pool[0-9]+)/' + user + r'/plugin/pluginsrc/' + plugin, str(src))
    r.require(match and os.path.ismount('/nas/' + match[1]), 'data pool not ready')
    r.plain(home)
    r.plain(src)
    destination = r.plain(r.DATA / user / plugin)
    for name in ('etc', 'var'):
        r.require(r.plain(destination / name).is_dir(), 'persistent data missing; refusing empty state')
    registry = Path('/data/plugin') / (user + '.list')
    # Never restore a removed account or overwrite a broken registry with {}.
    r.require(registry.is_file(), 'user registry not ready')
    with r.user_lock(user):
        data = r.read(registry)
        live = data.get(plugin)
        expected_info = r.read(saved / 'INFO')
        source_ok = src.is_dir() and (not check_source or r.digest(src) == expected_info['abstract'])
        complete = ((src / 'ui/config').is_file() and (home / 'INFO').is_file()
                    and all((home / 'scripts' / p.name).is_file() for p in ((saved / 'scripts') if (saved / 'scripts').is_dir() else (saved / 'payload/scripts')).iterdir())
                    and all((home / name).is_symlink() for name in ('src', 'tmp', 'etc', 'var')) and source_ok)
        if complete and isinstance(live, dict) and live.get('install') is True and live.get('info', {}).get('version') == expected_info['version']:
            r.observe_state(record, live, user, plugin, home, src)
        r.apply_service_intent(record, directory)
        if r.read(saved / 'record.json') != record:
            r.save(saved / 'record.json', record)
        repair = not complete or not isinstance(live, dict) or live.get('install') is not True
        if (home / 'INFO').is_file():
            r.require(r.read(home / 'INFO')['version'] == expected_info['version'], 'different installed version; capture it explicitly')
        if src.is_dir() and not source_ok:
            restore_missing_source(r, src, saved / 'src', owner)
            r.require(r.digest(src) == expected_info['abstract'], 'source changed; refusing silent rollback')
        if repair:
            home.mkdir(parents=True, exist_ok=True)
            os.chown(home, *owner)
            home.chmod(0o700)
            if not src.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                # Restore through a same-filesystem staging directory.
                stage = Path(tempfile.mkdtemp(prefix='.' + plugin + '-restore-', dir=src.parent))
                try:
                    shutil.copytree(saved / 'src', stage, dirs_exist_ok=True, symlinks=True)
                    for p in [stage, *stage.rglob('*')]:
                        os.chown(p, *owner, follow_symlinks=False)
                    os.replace(stage, src)
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
            r.require(r.digest(src) == expected_info['abstract'], 'incomplete source: retained for manual inspection')
            r.protected(r.DATA, 0o711)
            destination = r.plain(r.DATA / user / plugin)
            for name in ('etc', 'var'):
                r.require(r.plain(destination / name).is_dir(), 'persistent data missing; refusing empty state')
                r.linked(home / name, destination / name, owner)
            scripts = r.plain(home / 'scripts')
            scripts.mkdir(exist_ok=True)
            os.chown(scripts, *owner)
            for p in ((saved / 'scripts') if (saved / 'scripts').is_dir() else (saved / 'payload/scripts')).iterdir():
                r.atomic(scripts / p.name, p.read_bytes(), 0o755, owner)
            r.atomic(home / 'INFO', r.encoded(expected_info), 0o644, owner)
            r.linked(home / 'src', src, owner)
            r.linked(src / 'INFO', home / 'INFO', owner)
        # A tmp symlink can survive while its target is removed. Initialize the
        # parent and check the target even when the rest of the install is intact.
        tmp = Path(record['tmp'])
        if check_source or repair:
            r.prepare_tmp(user, src, tmp, owner)
        r.linked(home / 'tmp', tmp, owner)
        if plugin in r.SERVERS:
            r.install_daemon(user, plugin)
            if plugin=='downloadcenter' and r.memory_download(home):r.download_runtime(user)
        webroot = Path(r.read(Path('/etc/config/plugin')).get('settings', {}).get('nginx_plugin', '/data/plugin/www'))
        r.plain(webroot / user).mkdir(parents=True, exist_ok=True)
        web = webroot / user / plugin
        if web.is_symlink() and os.readlink(web) == str(src / 'ui'):
            web.unlink()
        r.linked(web, home / 'src/ui', owner)
        icon = Path('/data/plugin/www/icon') / (plugin + '.icon')
        r.plain(icon.parent).mkdir(parents=True, exist_ok=True)
        icon_source = saved / 'src/ui/icon.png'
        if not icon_source.exists(): icon_source = saved / 'payload/ui/icon.png'
        expected_icon = icon_source.read_bytes()
        if not icon.is_file() or icon.read_bytes() != expected_icon:
            r.atomic(icon, expected_icon, 0o644, owner)
        r.port_registry(user, plugin, expected_info['port'])
        # Merge exactly one entry; all official/other-user entries remain intact.
        if repair:
            # Restoring files is not a new user enable action. Preserve both
            # the native toggle and the separate daemon/scheduler intent.
            record['entry'].update(enable=record['enabled'],
                                   status='running' if record['enabled'] else 'disable')
            r.save(saved / 'record.json', record)
        entry = dict(record['entry'], enable=record['enabled'])
        if data.get(plugin) != entry:
            data[plugin] = entry
            r.save(registry, data)
        if check_source or repair:
            r.verify(user, plugin, home, src)
    # Do not hold the list lock while lifecycle hooks update the list themselves.
    restore_component(r, saved, user, plugin, transaction)
    update = directory / 'service-update.json'
    if update.is_file() and r.read(update).get('until', 0) > time.time():
        return
    if plugin in r.SERVERS and not record['running'] and record.get('runtime_intent_time'):
        if r.control(user, plugin, home, src, 'status', False).returncode == 0:
            r.control(user, plugin, home, src, 'stop')
    if plugin in r.SERVERS and record['running']:
        if r.control(user, plugin, home, src, 'status', False).returncode:
            result = r.control(user, plugin, home, src, 'start', False)
            # A user-owned control may start successfully but cannot update the
            # firmware's root-owned list. Verify the service independently.
            deadline = time.monotonic() + 20
            while r.control(user, plugin, home, src, 'status', False).returncode:
                r.require(time.monotonic() < deadline,
                        'service did not recover: ' + plugin + ' (start exit ' + str(result.returncode) + ')')
                time.sleep(0.25)
    # Only resume the scheduler flag; never execute a task or an SSH recovery.
    if repair:
        print('已恢复：' + user + '/' + plugin, flush=True)

