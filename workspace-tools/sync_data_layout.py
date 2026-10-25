"""Generate owner-scoped migration; never distribute an all-component archive."""
from pathlib import Path
import ast
import re

ROOT=Path(__file__).resolve().parents[1]
BEGIN='# BEGIN MINAS DATA LAYOUT (generated)'
END='# END MINAS DATA LAYOUT'
SHELL_BEGIN='# BEGIN MINAS AUTO LAYOUT SHELL (generated)'
SHELL_END='# END MINAS AUTO LAYOUT SHELL'
PLUGINS=('dockermanager','mihomo','taskcenter','nascenter','sshrecovery','quickshare','downloadcenter')
PRIVATE={'dockermanager':['docker-manager-helper'],'taskcenter':['task-center-helper','task_schedule.py','task_maintenance.py'],
         'sshrecovery':['ssh-recovery-helper','restore-ssh.sh'],'mihomo':['api.py','mihomo-docker-proxy','docker_proxy_status.py'],
         'nascenter':['api.py','nas-center-helper','process_metrics.py']}
FLAT={'dockermanager':['docker-manager-helper'],'taskcenter':['task-center-helper','task_schedule.py','task_maintenance.py'],
      'sshrecovery':['ssh-recovery-helper','ssh-recovery-boot.sh']}


def select_plugin(source,plugin):
    """Remove unrelated migration branches from each distributed helper."""
    class Owner(ast.NodeTransformer):
        def visit_Compare(self,node):
            node=self.generic_visit(node)
            if isinstance(node.left,ast.Name) and node.left.id=='plugin' and len(node.ops)==1 and isinstance(node.ops[0],ast.Eq) and isinstance(node.comparators[0],ast.Constant):
                return ast.copy_location(ast.Constant(plugin==node.comparators[0].value),node)
            return node
        def visit_BoolOp(self,node):
            node=self.generic_visit(node)
            if isinstance(node.op,ast.And) and any(isinstance(v,ast.Constant) and v.value is False for v in node.values):return ast.copy_location(ast.Constant(False),node)
            return node
        def visit_If(self,node):
            node=self.generic_visit(node)
            if isinstance(node.test,ast.Constant) and isinstance(node.test.value,bool):return node.body if node.test.value else node.orelse
            return node
        def visit_FunctionDef(self,node):
            if node.name=='abort_plugin_layout' and plugin!='taskcenter':node.body=[ast.Return()]
            if node.name=='_layout_retire_copied' and plugin not in ('taskcenter','sshrecovery'):return None
            return self.generic_visit(node)
    return ast.unparse(ast.fix_missing_locations(Owner().visit(ast.parse(source))))+'\n'


def replace_block(content,begin,end,value):
    pattern=r'^'+re.escape(begin)+r'\n.*?^'+re.escape(end)+r'(?:\n|$)'
    text,count=re.subn(pattern,lambda m:value.rstrip()+'\n',content,flags=re.M|re.S)
    if count!=1:raise ValueError('expected one generated block: '+begin)
    return text


def helper(scope,paths=None,kind=None,plugin='',copy_paths=None,program=''):
    source=(ROOT/'workspace-tools/data-layout/scoped_common.py').read_text()
    source=source.replace("DATA_LAYOUT_SCOPE = 'unconfigured'",'DATA_LAYOUT_SCOPE = '+repr(scope))
    source=source.replace('DATA_LAYOUT_PATHS = {}','DATA_LAYOUT_PATHS = '+repr(paths or {}))
    if kind=='plugin':
        extra=(ROOT/'workspace-tools/data-layout/scoped_plugin.py').read_text()
        extra=extra.replace("DATA_LAYOUT_PLUGIN = ''",'DATA_LAYOUT_PLUGIN = '+repr(plugin))
        extra=extra.replace('DATA_LAYOUT_PRIVATE_FILES = []','DATA_LAYOUT_PRIVATE_FILES = '+repr(PRIVATE.get(plugin,[])))
        extra=extra.replace('DATA_LAYOUT_FLAT_FILES = []','DATA_LAYOUT_FLAT_FILES = '+repr(FLAT.get(plugin,[])))
        extra=select_plugin(extra,plugin)
        source+='\n\n'+extra
    elif kind=='tool':
        extra=(ROOT/'workspace-tools/data-layout/scoped_tool.py').read_text()
        extra=extra.replace('DATA_LAYOUT_COPY_PATHS = {}','DATA_LAYOUT_COPY_PATHS = '+repr(copy_paths or {}))
        extra=extra.replace("DATA_LAYOUT_PROGRAM = ''",'DATA_LAYOUT_PROGRAM = '+repr(program))
        if not copy_paths:
            start=extra.index('def _layout_copy_shared_state():')
            end=extra.index('\ndef ensure_data_layout():',start)
            extra=extra[:start]+'def _layout_copy_shared_state():\n    return\n\n'+extra[end:]
        source+='\n\n'+extra
    if scope=='docker-runtime':source+='\n\n'+(ROOT/'workspace-tools/data-layout/docker_runtime_hooks.py').read_text()
    if scope=='docker-network':source+="\nDATA_LAYOUT_SERVICES = ('minas-docker-ipv6.service','minas-docker-ipv6-events.service')\ndef _layout_lock_paths():\n    from pathlib import Path\n    return [Path('/data/docker-bridge-repair/lock')]\n"
    if scope=='ssh':source+='\n\n'+(ROOT/'workspace-tools/data-layout/ssh_hooks.py').read_text()
    return source.rstrip()+'\n'


def embed(path,source):
    text=path.read_text()
    text=replace_block(text,BEGIN,END,BEGIN+'\n'+source+END)
    path.write_text(text,encoding='utf-8',newline='\n')


def shell_guard(source,paths):
    return (SHELL_BEGIN+'\nminas_auto_layout() {\n'
            '    if [ -f /data/minas/migration/ssh/complete ] && { [ ! -e /data/ssh-autostart-fix ] || { [ -f /data/ssh-autostart-fix/restore-ssh.sh ] && grep -q "^# minas-ssh-layout-forwarder$" /data/ssh-autostart-fix/restore-ssh.sh; }; }; then return; fi\n'
            '    if [ ! -e /data/ssh-autostart-fix ] && [ ! -e /data/.minas-privileged/modules/sshrecovery/var ] && [ ! -e /data/.minas-privileged/sshrecovery ] && [ ! -e /data/minas/migration/ssh/state.json ]; then return; fi\n'
            '    MINAS_LAYOUT_SHELL_SOURCE="$0" python3 -B - <<\'MINAS_AUTO_LAYOUT_PY\'\n'+source+
            "\nensure_data_layout()\nMINAS_AUTO_LAYOUT_PY\n}\n"+SHELL_END)


def synchronize():
    embed(ROOT/'workspace-tools/privileged-component/common/privileged_runtime.py',helper('privileged'))
    embed(ROOT/'workspace-tools/plugin-recovery/recovery.py',helper('recovery'))
    embed(ROOT/'nas-root/tools/docker-upgrade/src/docker_runtime_upgrade.py',helper('docker-runtime',{
        '/data/.minas-docker-runtime':'/data/minas/docker/runtime',
        '/data/docker-upgrade-backups':'/data/minas/backups/docker'},'tool',program='docker/runtime/upgrade.py'))
    embed(ROOT/'nas-root/tools/docker-bridge/src/docker_bridge_repair.py',helper('docker-network',{
        '/data/docker-bridge-repair':'/data/minas/docker/network'},'tool',program='docker/network/repair.py'))
    for plugin in PLUGINS:
        source=helper('plugin-'+plugin,kind='plugin',plugin=plugin)
        source+='''\nif __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='当前插件的目录初始化及旧文件清理')
    parser.add_argument('--finish',action='store_true')
    parser.add_argument('--abort',action='store_true')
    parser.add_argument('--user')
    args=parser.parse_args()
    ensure_data_layout()
    if args.finish:
        if not args.user:parser.error('--finish requires --user')
        finish_plugin_layout(args.user)
    if args.abort:
        if not args.user:parser.error('--abort requires --user')
        abort_plugin_layout(args.user)
'''
        (ROOT/plugin/'installer/data_layout.py').write_text(source,encoding='utf-8',newline='\n')
        if plugin!='sshrecovery':
            entry=ROOT/plugin/'manage.sh';text=entry.read_text()
            abort='''
nas_layout_exit() {
    nas_layout_rc=$?
    trap - 0
    if [ "$nas_layout_rc" != 0 ]; then python3 -B "$INSTALLER_DIR/$PLUGIN_NAME/installer/data_layout.py" --abort --user "$NAS_USER" || true; fi
    flock -u 7 2>/dev/null || true
    exit "$nas_layout_rc"
}
trap nas_layout_exit 0
'''
            if 'nas_layout_exit() {' not in text:text=text.replace('nas_prepare install "${1:-}"\n','nas_prepare install "${1:-}"\n'+abort,1)
            finish='python3 -B "$INSTALLER_DIR/$PLUGIN_NAME/installer/data_layout.py" --finish --user "$NAS_USER" || nas_fail "当前插件旧文件清理未完成，已保留旧数据"'
            if finish not in text:text=text.replace('\nNAS_INSTALL_SCRIPT\n','\n'+finish+'\nNAS_INSTALL_SCRIPT\n',1)
            entry.write_text(text,encoding='utf-8',newline='\n')
    worker=ROOT/'sshrecovery/installer/common/modules/sshrecovery/restore-ssh.sh'
    paths={'/data/ssh-autostart-fix':'/data/minas/ssh'}
    copies={'/data/.minas-privileged/modules/sshrecovery/var':'/data/minas/privileged/modules/sshrecovery/var',
            '/data/.minas-privileged/sshrecovery':'/data/minas/privileged/modules/sshrecovery/var'}
    source=helper('ssh',paths,'tool',copy_paths=copies,program='ssh/restore-ssh.sh')
    worker.write_text(replace_block(worker.read_text(),SHELL_BEGIN,SHELL_END,shell_guard(source,paths)),encoding='utf-8',newline='\n')
    bootstrap=ROOT/'nas-root/tools/ssh/src/first-ssh-nas.sh'
    text=bootstrap.read_text()
    if SHELL_BEGIN in text:text=replace_block(text,SHELL_BEGIN,SHELL_END,'')
    text=re.sub(r'^\s*minas_auto_layout\s*$','',text,flags=re.M)
    bootstrap.write_text(text,encoding='utf-8',newline='\n')
    legacy=ROOT/'nas-root/xiaomi-storage-open-ssh-wsl-1.0.8.425.sh';text=legacy.read_text()
    if SHELL_BEGIN in text:text=replace_block(text,SHELL_BEGIN,SHELL_END,'')
    text=re.sub(r'^\s*minas_auto_layout\s*$','',text,flags=re.M)
    legacy.write_text(text,encoding='utf-8',newline='\n')
    print('owner-scoped layout helpers synchronized; first-open tools contain no migration')


if __name__=='__main__':synchronize()
