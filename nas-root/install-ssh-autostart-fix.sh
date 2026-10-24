#!/usr/bin/env bash
# Xiaomi NAS SSH 2.1.1. Protocol adapted from Scruel's minas-enable-ssh_wsl.sh:
# https://github.com/scruel/xiaomi-storage-minas-tool/blob/main/minas-enable-ssh_wsl.sh
# Requires existing administrator authority; never modifies RPMB or root passwords.
set -Eeuo pipefail
umask 077
fail() { printf '失败：%s\n' "$*" >&2; exit 1; }
need() { local tool; for tool in "$@"; do command -v "$tool" >/dev/null || fail "缺少命令：$tool"; done; }
usage() {
    if [[ ${BOOT_ONLY:-0} == 1 ]]; then
        cat <<'INSTALL_HELP'
小米 NAS 纯 Shell 开机修复安装器 2.1.1
用法：bash install-ssh-autostart-fix.sh [NAS IPv4] [--identity 私钥路径]
需要已能通过公钥执行真实 root SSH 命令；不新增登录公钥。
安装与 SSH 恢复插件相同的开机 worker，不重启 NAS。
  -h, --help              显示帮助
INSTALL_HELP
        return
    fi
    cat <<'HELP'
小米 NAS SSH 开启与开机修复 2.1.1（纯 Shell）
用法：bash xiaomi-storage-open-ssh-wsl.sh [NAS IPv4] [选项]
  --user u数字            保存恢复公钥的 NAS 账户；多账户可交互选择
  --identity 路径         WSL 私钥路径（默认 ~/.ssh/id_ed25519）
  --cert-dir 路径         已登录小米 Windows 客户端的 minasCert 目录（WSL 路径）
  --legacy-bootstrap      强制测试旧证书引导；新固件可能已封堵该入口
  --probe-bootstrap       只读 SSH 设置，仅用临时 UID 标记探测旧入口
  --check                 只验证公钥登录和真实 root 命令，不修改 NAS
  --install-boot-only     已有 SSH 时安装/更新开机修复，不新增登录公钥
  --portainer             通过已映射 NAS / 的运行中容器恢复
  --portainer-url URL     默认 http://NAS:9001；支持验证证书的 HTTPS
  --portainer-user 名称   Portainer 管理员（密码交互输入，不保存）
  --endpoint 数字         Portainer 环境 ID；只有一个环境时自动选择
  --container 名称       应急容器名称（默认 test）
  -h, --help              显示帮助
默认安装开机修复；常规恢复使用 Shell，旧目录自动迁移使用 NAS 的 Python3。
证书及 Portainer 路径需要 curl、jq；现有 SSH 路径只需 Bash、OpenSSH 等基本命令。
HELP
}
ipv4() {
    local part; local -a parts
    [[ $1 =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    IFS=. read -r -a parts <<< "$1"
    for part in "${parts[@]}"; do ((10#$part <= 255)) || return 1; done
}
valid_user() { [[ $1 =~ ^u[0-9]+$ ]]; }
select_user() {
    local raw=$1 choice i; local -a users=()
    while IFS= read -r choice; do
        [[ -n $choice ]] || continue
        valid_user "$choice" || fail 'NAS 返回的账户列表无效'
        users+=("$choice")
    done < <(printf '%s\n' "$raw" | LC_ALL=C sort -u)
    ((${#users[@]})) || fail '未发现 /home/u数字 格式的 NAS 账户'
    if [[ -n $NAS_USER ]]; then
        for choice in "${users[@]}"; do [[ $choice != "$NAS_USER" ]] || return 0; done
        fail "--user 不在 NAS 账户列表中，可选账户：${users[*]}"
    fi
    if ((${#users[@]} == 1)); then NAS_USER=${users[0]}; return; fi
    [[ -t 0 ]] || fail "发现多个 NAS 账户，请通过 --user 指定：${users[*]}"
    printf '%s\n' '请选择保存 SSH 恢复材料的 NAS 账户：'
    for i in "${!users[@]}"; do printf '  %d. %s\n' "$((i+1))" "${users[i]}"; done
    while :; do
        read -r -p '输入编号或完整账户名（Ctrl+C 取消）：' choice || fail '未选择 NAS 账户'
        for i in "${!users[@]}"; do
            if [[ $choice == "$((i+1))" || $choice == "${users[i]}" ]]; then NAS_USER=${users[i]}; return; fi
        done
        printf '%s\n' '选择无效，请重试。'
    done
}
ssh_ready() {
    local result
    result=$(ssh "${SSH_OPTIONS[@]}" "root@$NAS_IP" 'printf "__MINAS_ROOT_COMMAND_OK__\n"; id -u' 2>/dev/null) || return 1
    [[ ${result//$'\r'/} == $'__MINAS_ROOT_COMMAND_OK__\n0' ]]
}
prepare_identity() {
    mkdir -p "$(dirname "$IDENTITY")"
    if [[ ! -e $IDENTITY ]]; then
        [[ ! -e $IDENTITY.pub ]] || fail '仅存在公钥，缺少对应私钥；请用 --identity 指定现有密钥'
        ssh-keygen -q -t ed25519 -N '' -f "$IDENTITY" -C '' </dev/null
    fi
    ssh-keygen -y -f "$IDENTITY" | awk '{print $1 " " $2}' > "$STAGE/public.key"
    awk 'NF!=2 || $1 !~ /^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521))$/ {exit 1}' "$STAGE/public.key" || fail '不支持此公钥类型'
    ssh-keygen -lf "$STAGE/public.key" >/dev/null || fail '无法校验公钥'
}
write_repair_sources() {
    cat > "$STAGE/restore-ssh.sh" <<'MINAS_SSH_REPAIR_SH_SOURCE'
#!/bin/sh
# Pure POSIX shell recovery for Xiaomi NAS. Invoked with existing root authority.
set -eu
umask 077
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
WORK=/data/minas/ssh
STATE=/data/minas/privileged/modules/sshrecovery/var
PASSWD=/etc/passwd
SHELL_DROPIN=/etc/systemd/system/dropbear@.service.d/90-normal-shell.conf
BOOT_DROPIN=/etc/systemd/system/minas.boot_check.service.d/90-keep-ssh.conf
CRON=/etc/cron.d/keep-ssh
PENDING=/etc/cron.d/minas-shell-ssh-activate
SYSTEMCTL=/usr/bin/systemctl
fail() { printf '失败：%s\n' "$*" >&2; exit 1; }

safe() {
    node=$1
    case "$node" in /*) ;; *) fail '恢复路径必须为绝对路径' ;; esac
    while :; do
        [ ! -L "$node" ] || fail "恢复路径包含符号链接：$node"
        if [ -e "$node" ]; then
            info=$(stat -c '%u %a' "$node")
            owner=${info%% *}; path_mode=${info#* }
            [ "$owner" = 0 ] && [ "$((0$path_mode & 0022))" -eq 0 ] || fail "恢复路径权限异常：$node"
        fi
        [ "$node" != / ] || break
        node=${node%/*}; [ -n "$node" ] || node=/
    done
}
private_dir() { safe "$1"; mkdir -p "$1"; chmod 0700 "$1"; }
prepare_layout() {

# BEGIN MINAS AUTO LAYOUT SHELL (generated)
minas_auto_layout() {
    if [ -f /data/minas/migration/ssh/complete ] && { [ ! -e /data/ssh-autostart-fix ] || { [ -f /data/ssh-autostart-fix/restore-ssh.sh ] && grep -q "^# minas-ssh-layout-forwarder$" /data/ssh-autostart-fix/restore-ssh.sh; }; }; then return; fi
    if [ ! -e /data/ssh-autostart-fix ] && [ ! -e /data/.minas-privileged/modules/sshrecovery/var ] && [ ! -e /data/.minas-privileged/sshrecovery ] && [ ! -e /data/minas/migration/ssh/state.json ]; then return; fi
    MINAS_LAYOUT_SHELL_SOURCE="$0" python3 -B - <<'MINAS_AUTO_LAYOUT_PY'
"""Small standalone layout primitives; generated constants select one owner."""
DATA_LAYOUT_VERSION = 1
DATA_LAYOUT_ROOT = '/data/minas'
DATA_LAYOUT_SCOPE = 'ssh'
DATA_LAYOUT_PATHS = {'/data/ssh-autostart-fix': '/data/minas/ssh'}


def _layout_secure(path):
    import os,stat
    from pathlib import Path
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts:raise ValueError('invalid layout path')
    for node in (path,*path.parents):
        s=node.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:
            raise ValueError('unsafe layout directory: '+str(node))
    return path


def _layout_atomic(path,data,mode=0o600):
    import os,tempfile
    from pathlib import Path
    path=Path(path);_layout_secure(path.parent)
    fd,name=tempfile.mkstemp(prefix='.layout-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data if isinstance(data,bytes) else data.encode())
            stream.flush();os.fchmod(stream.fileno(),mode);os.fsync(stream.fileno())
        os.replace(name,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)


def _layout_initialize():
    import json,os,stat
    from pathlib import Path
    if os.geteuid()!=0:raise ValueError('root required for layout')
    root=Path(DATA_LAYOUT_ROOT);_layout_secure(root.parent)
    if not root.exists():root.mkdir(mode=0o755)
    _layout_secure(root)
    if not root.stat().st_mode&0o001:raise ValueError('layout root must allow traversal')
    marker=root/'layout.json'
    if os.path.lexists(marker):
        s=marker.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('unsafe layout marker')
        value=json.loads(marker.read_text())
        if value.get('version')!=DATA_LAYOUT_VERSION or value.get('root')!=DATA_LAYOUT_ROOT:raise ValueError('unsupported layout')
    else:_layout_atomic(marker,json.dumps(dict(version=DATA_LAYOUT_VERSION,root=DATA_LAYOUT_ROOT))+'\n',0o644)


def ensure_data_layout():
    # Shared runtimes initialize their own parents; migration is installer-owned.
    # No plugin, SSH or Docker migration archive is embedded here.
    _layout_initialize()


def remap_data_path(value):
    import os
    from pathlib import Path
    path=Path(value)
    if not path.is_absolute() or '..' in path.parts:raise ValueError('invalid historical path')
    for old,new in DATA_LAYOUT_PATHS.items():
        if path==Path(old) or path.is_relative_to(old):
            target=Path(new)/path.relative_to(old)
            if os.path.lexists(target):
                _layout_secure(DATA_LAYOUT_ROOT)
                if os.path.lexists(path):raise ValueError('conflicting historical file: '+str(path))
                return target
    return path


def rebase_data_text(text):
    for old,new in sorted(DATA_LAYOUT_PATHS.items(),key=lambda pair:len(pair[0]),reverse=True):text=text.replace(old,new)
    return text


"""One tool's transactional directory handover; no packaged foreign programs."""
DATA_LAYOUT_COPY_PATHS = {'/data/.minas-privileged/modules/sshrecovery/var': '/data/minas/privileged/modules/sshrecovery/var', '/data/.minas-privileged/sshrecovery': '/data/minas/privileged/modules/sshrecovery/var'}
DATA_LAYOUT_PROGRAM = 'ssh/restore-ssh.sh'


def _layout_tool_migrate():
    import contextlib,fcntl,hashlib,json,os,stat
    from pathlib import Path
    root=Path(DATA_LAYOUT_ROOT);work=root/'migration'/DATA_LAYOUT_SCOPE
    for directory in (root/'migration',work):
        if not directory.exists():directory.mkdir(mode=0o700)
        _layout_secure(directory)
    pointer=work/'state.json'
    fd=os.open('/run/minas-layout-'+DATA_LAYOUT_SCOPE+'.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    def save(state):_layout_atomic(pointer,json.dumps(state,ensure_ascii=False,indent=2)+'\n')
    def snapshot(state,path):
        key=str(path)
        if key in state['files']:return
        if not os.path.lexists(path):state['files'][key]=None;save(state);return
        s=path.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('unsafe tool entry: '+key)
        name=hashlib.sha256(key.encode()).hexdigest();_layout_atomic(work/name,path.read_bytes())
        state['files'][key]=dict(blob=name,mode=stat.S_IMODE(s.st_mode),owner=[s.st_uid,s.st_gid],times=[s.st_atime_ns,s.st_mtime_ns]);save(state)
    def rollback(state):
        globals().get('_layout_remove_compat',lambda:None)()
        for old,item in reversed(list(state['moves'].items())):
            old,new=Path(old),Path(item['new']);present=new if new.exists() else old
            if [present.stat().st_dev,present.stat().st_ino]!=item['inode']:raise ValueError('tool directory identity changed')
            if present==new:
                if old.exists():raise ValueError('conflicting rollback directory')
                os.rename(new,old)
        for name,item in reversed(list(state['files'].items())):
            path=Path(name)
            if item is None:path.unlink(missing_ok=True);continue
            _layout_atomic(path,(work/item['blob']).read_bytes(),item['mode'])
            os.chown(path,*item['owner']);os.utime(path,ns=tuple(item['times']))
        _layout_run(['systemctl','daemon-reload'])
        callback=globals().get('_layout_runtime_restart')
        if callback and state.get('runtime'):callback(state['runtime'],False)
        for unit,wanted in state.get('services',{}).items():
            if wanted:_layout_run(['systemctl','start',unit])
        state['phase']='rolled-back';save(state)
    locks=contextlib.ExitStack();cooperating=[]
    try:
        s=os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('unsafe tool migration lock')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        for name in globals().get('_layout_lock_paths',lambda:[])():
            path=Path(name)
            if not path.parent.exists():continue
            _layout_secure(path.parent)
            other=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
            locks.callback(os.close,other);s=os.fstat(other)
            if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('unsafe cooperating operation lock')
            if not any(os.fstat(previous).st_dev==s.st_dev and os.fstat(previous).st_ino==s.st_ino for previous in cooperating):
                fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
            cooperating.append(other)
        if pointer.exists():
            previous=json.loads(pointer.read_text())
            if previous.get('scope')!=DATA_LAYOUT_SCOPE:raise ValueError('migration scope mismatch')
            if previous['phase'] not in ('committed','rolled-back'):rollback(previous)
        moves={}
        for old,new in DATA_LAYOUT_PATHS.items():
            if not os.path.lexists(old):continue
            _layout_secure(old)
            if os.path.lexists(new):
                if globals().get('_layout_is_compat',lambda p:False)(old):continue
                raise ValueError('同一工具的新旧目录同时存在，未覆盖：'+old)
            parent=Path(new).parent
            while not parent.exists():parent=parent.parent
            _layout_secure(parent)
            if Path(old).stat().st_dev!=parent.stat().st_dev or os.path.ismount(old):raise ValueError('tool migration requires the same filesystem')
            moves[old]=dict(new=new,inode=[Path(old).stat().st_dev,Path(old).stat().st_ino])
        if not moves:
            _layout_copy_shared_state()
            if DATA_LAYOUT_SCOPE=='ssh':globals().get('_layout_after_commit',lambda:None)()
            return
        state=dict(scope=DATA_LAYOUT_SCOPE,phase='prepared',moves=moves,files={},services={},runtime=None)
        # Journal only programs/configuration referring to this tool's paths.
        entries=[]
        for old in moves:
            if Path(old).name.endswith('backups'):continue
            for p in Path(old).rglob('*'):
                relative=p.relative_to(old)
                if any(part in ('transactions','keys','host','backups','__pycache__') for part in relative.parts):continue
                if p.is_file() and not p.is_symlink() and (p.suffix in ('.py','.sh') or p.name=='daemon.json'):entries.append(p)
        for directory in ('/etc/systemd/system','/etc/cron.d'):
            for p in Path(directory).rglob('*'):
                if not p.is_file() or p.is_symlink():continue
                data=p.read_bytes()
                if any(old.encode() in data for old in moves):
                    entries.append(p)
                    if directory.endswith('system'):
                        unit=p.relative_to(directory).parts[0]
                        if unit.endswith('.d'):unit=unit[:-2]
                        if unit in globals().get('DATA_LAYOUT_SERVICES',()):state['services'][unit]=_layout_run(['systemctl','is-active','--quiet',unit],False).returncode==0
        program=root/DATA_LAYOUT_PROGRAM if DATA_LAYOUT_PROGRAM else None
        if program:
            original=next((Path(old)/program.relative_to(item['new']) for old,item in moves.items() if program.is_relative_to(item['new'])),program)
            snapshot(state,original)
        for p in globals().get('_layout_journal_paths',lambda:[])():snapshot(state,Path(p))
        for p in entries:snapshot(state,p)
        save(state)
        try:
            capture=globals().get('_layout_runtime_capture')
            if capture:state['runtime']=capture(moves);save(state)
            prepare=globals().get('_layout_runtime_pause')
            if prepare and state.get('runtime'):prepare(state['runtime'])
            else:
                for unit in state['services']:_layout_run(['systemctl','stop',unit])
            state['phase']='stopped';save(state)
            for old,item in moves.items():
                target=Path(item['new']);target.parent.mkdir(mode=0o755,parents=True,exist_ok=True);_layout_secure(target.parent)
                os.rename(old,target);save(state)
            state['phase']='moved';save(state)
            for name,item in state['files'].items():
                if item is None:continue
                target=Path(rebase_data_text(name));data=(work/item['blob']).read_bytes()
                if b'\0' not in data:
                    for old,new in DATA_LAYOUT_PATHS.items():data=data.replace(old.encode(),new.encode())
                _layout_atomic(target,data,item['mode']);os.chown(target,*item['owner'])
            # Install only this entry's own current Python worker. No unrelated
            # Docker/network/SSH installer is carried in an archive.
            source=globals().get('__file__','')
            if not source or not Path(source).is_file():source=os.environ.get('MINAS_LAYOUT_SHELL_SOURCE','')
            program=root/DATA_LAYOUT_PROGRAM if DATA_LAYOUT_PROGRAM else None
            if program and source and Path(source).is_file() and any(program.is_relative_to(item['new']) for item in moves.values()):
                s=Path(source).lstat()
                if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('unsafe current tool worker')
                _layout_atomic(program,Path(source).read_bytes(),0o700)
            _layout_copy_shared_state()
            _layout_run(['systemctl','daemon-reload'])
            restart=globals().get('_layout_runtime_restart')
            if restart and state.get('runtime'):restart(state['runtime'],True)
            else:
                for unit,wanted in state['services'].items():
                    if wanted:_layout_run(['systemctl','start',unit])
            globals().get('_layout_after_commit',lambda:None)()
            state['phase']='committed';save(state)
        except BaseException:
            rollback(state);raise
    finally:locks.close();os.close(fd)


def _layout_run(args,check=True):
    import subprocess
    return subprocess.run(args,check=check,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=180)


def _layout_copy_shared_state():
    """Copy required shared state without removing a legacy plugin's files."""
    import os,shutil,stat
    from pathlib import Path
    for old,new in DATA_LAYOUT_COPY_PATHS.items():
        old,new=Path(old),Path(new)
        if not old.exists():continue
        _layout_secure(old)
        for p in old.rglob('*'):
            relative=p.relative_to(old);target=new/relative;s=p.lstat()
            if DATA_LAYOUT_SCOPE=='ssh':
                if relative.parts[0] not in ('keys','host') and relative.as_posix()!='operations.lock':continue
                if relative.parts[0]=='keys' and len(relative.parts)>1 and p.suffix!='.pub':continue
            if stat.S_ISLNK(s.st_mode) or s.st_uid!=0:raise ValueError('unsafe shared SSH state')
            if p.is_dir():
                target.mkdir(mode=stat.S_IMODE(s.st_mode),parents=True,exist_ok=True);_layout_secure(target);continue
            if not stat.S_ISREG(s.st_mode):raise ValueError('unsupported shared SSH state')
            target.parent.mkdir(mode=0o700,parents=True,exist_ok=True);_layout_secure(target.parent)
            if target.exists():continue
            if p.name.endswith('.lock'):os.link(p,target)
            else:
                shutil.copy2(p,target);os.chown(target,s.st_uid,s.st_gid)


def ensure_data_layout():
    _layout_initialize()
    _layout_tool_migrate()


"""SSH-only compatibility while an older SSH plugin is still installed."""
DATA_LAYOUT_SSH_COMPAT = '#!/bin/sh\n# minas-ssh-layout-forwarder\nexec /bin/sh /data/minas/ssh/restore-ssh.sh "$@"\n'


def _layout_is_compat(path):
    from pathlib import Path
    path=Path(path)
    return path.is_dir() and {p.name for p in path.iterdir()}=={'restore-ssh.sh'} and (path/'restore-ssh.sh').read_text()==DATA_LAYOUT_SSH_COMPAT


def _layout_remove_compat():
    from pathlib import Path
    path=Path('/data/ssh-autostart-fix')
    if _layout_is_compat(path):(path/'restore-ssh.sh').unlink();path.rmdir()


def _layout_after_commit():
    import json
    from pathlib import Path
    registry=Path('/data/.minas-privileged/registry.json')
    if Path('/data/minas/ssh/restore-ssh.sh').is_file() and registry.is_file() and any('sshrecovery' in grants for grants in json.loads(registry.read_text()).get('users',{}).values()):
        old=Path('/data/ssh-autostart-fix');old.mkdir(mode=0o700,exist_ok=True);_layout_secure(old)
        _layout_atomic(old/'restore-ssh.sh',DATA_LAYOUT_SSH_COMPAT,0o700)
    _layout_atomic(Path(DATA_LAYOUT_ROOT)/'migration/ssh/complete',b'complete\n')


def _layout_lock_paths():
    from pathlib import Path
    return [Path(p)/'operations.lock' for p in ('/data/minas/privileged/modules/sshrecovery/var',
               '/data/.minas-privileged/modules/sshrecovery/var','/data/.minas-privileged/sshrecovery')]

ensure_data_layout()
MINAS_AUTO_LAYOUT_PY
}
# END MINAS AUTO LAYOUT SHELL

    minas_auto_layout
    safe /data/minas
    mkdir -p /data/minas
    chmod 0755 /data/minas
}
# Atomic replacement in the target filesystem; callers validate the source.
put() {
    source=$1 target=$2 permissions=$3
    safe "$target"
    temp=$(mktemp "${target%/*}/.ssh-shell.XXXXXXXX")
    if ! cat "$source" > "$temp" || ! chmod "$permissions" "$temp" || ! mv -f "$temp" "$target"; then
        rm -f "$temp"; fail "无法写入 $target"
    fi
}
merge_keys() {
    incoming=$1 auth=$2
    safe "$auth"
    merged=$(mktemp "$WORK/.merge.XXXXXXXX")
    if [ -f "$auth" ]; then cat "$auth" > "$merged"; printf '\n' >> "$merged"; fi
    cat "$incoming" >> "$merged"
    awk 'NF && !seen[$0]++' "$merged" > "$merged.unique"
    put "$merged.unique" "$auth" 0600
    rm -f "$merged" "$merged.unique"
}
prepare_account() {
    safe "$PASSWD"
    record=$(awk -F: '$1=="root" {n++; row=$0} END {if(n==1) print row; else exit 1}' "$PASSWD") || fail 'root 记录异常'
    [ "$(printf '%s\n' "$record" | awk -F: '{print NF ":" $3}')" = '7:0' ] || fail 'root 记录异常'
    home=$(printf '%s\n' "$record" | cut -d: -f6)
    case "$home" in /root) home=/home/rootx ;; /home/rootx) ;; *) fail "不支持的 root 主目录：$home" ;; esac
    private_dir "$home"; private_dir "$home/.ssh"
    if [ "$(printf '%s\n' "$record" | cut -d: -f6)" = /root ] && [ -f /root/.ssh/authorized_keys ]; then
        safe /root/.ssh/authorized_keys
        merge_keys /root/.ssh/authorized_keys "$home/.ssh/authorized_keys"
    fi
    next=$(mktemp "$WORK/.passwd.XXXXXXXX")
    awk -F: -v OFS=: -v home="$home" '$1=="root" {$6=home; if($7 ~ /\/mi-shell$/) $7="/bin/sh"} {print}' "$PASSWD" > "$next"
    if ! cmp -s "$next" "$PASSWD"; then
        if [ ! -e "$WORK/passwd.before-shell-repair" ]; then put "$PASSWD" "$WORK/passwd.before-shell-repair" 0600; fi
        put "$next" "$PASSWD" "$(stat -c %a "$PASSWD")"
    fi
    rm -f "$next"
    safe "$home/.ssh/authorized_keys"
    if [ ! -e "$home/.ssh/authorized_keys" ] && [ -f "$WORK/login-authorized-keys" ]; then
        safe "$WORK/login-authorized-keys"
        put "$WORK/login-authorized-keys" "$home/.ssh/authorized_keys" 0600
    fi
    if [ -f "$home/.ssh/authorized_keys" ]; then chmod 0600 "$home/.ssh/authorized_keys"; fi
}
repair_shell() {
    [ -e /usr/sbin/mi-shell ] || return 0
    target_shell=$(readlink -f /usr/sbin/mi-shell)
    normal_shell=$(readlink -f /usr/bin/bash.bash 2>/dev/null || true)
    [ -x "$normal_shell" ] || normal_shell=$(readlink -f /bin/bash 2>/dev/null || true)
    [ -x "$normal_shell" ] || fail 'mi-shell 固件缺少可用 Bash'
    case "$normal_shell:$target_shell" in *[!a-zA-Z0-9_./:-]*) fail 'shell 路径包含不支持字符' ;; esac
    safe "$normal_shell"; safe "$target_shell"; safe "${SHELL_DROPIN%/*}"
    mkdir -p "${SHELL_DROPIN%/*}"
    conf=$(mktemp "$WORK/.shell-conf.XXXXXXXX")
    printf '[Service]\nBindReadOnlyPaths=%s:%s\n' "$normal_shell" "$target_shell" > "$conf"
    safe "$SHELL_DROPIN"
    if [ -e "$SHELL_DROPIN" ] && ! cmp -s "$conf" "$SHELL_DROPIN"; then
        rm -f "$conf"; fail '已有不同的 SSH shell 配置，已停止覆盖'
    fi
    put "$conf" "$SHELL_DROPIN" 0644; rm -f "$conf"
}
repair_host() {
    private_dir "$WORK/host"
    safe /etc/dropbear; mkdir -p /etc/dropbear
    host=/etc/dropbear/dropbear_rsa_host_key
    saved=$WORK/host/dropbear_rsa_host_key
    safe "$host"; safe "$saved"
    if [ ! -e "$host" ]; then
        if [ -s "$saved" ]; then
            put "$saved" "$host" 0600
        elif [ -s "$STATE/host/dropbear_rsa_host_key" ]; then
            safe "$STATE/host/dropbear_rsa_host_key"
            put "$STATE/host/dropbear_rsa_host_key" "$host" 0600
        else
            /usr/sbin/dropbearkey -t rsa -s 2048 -f "$host" >/dev/null 2>&1
            chmod 0600 "$host"
            printf '%s\n' '已生成新的 SSH 主机密钥，下次连接请核对指纹。'
        fi
    fi
    /usr/sbin/dropbearkey -y -f "$host" >/dev/null 2>&1 || fail '现有主机密钥无效，未覆盖'
    if [ ! -e "$saved" ]; then put "$host" "$saved" 0400; fi
}
install_boot() {
    worker=$1
    [ -f "$worker" ] || fail '缺少纯 Shell 开机复查程序'
    /bin/sh -n "$worker"
    safe "${BOOT_DROPIN%/*}"; safe /etc/cron.d
    mkdir -p "${BOOT_DROPIN%/*}" /etc/cron.d
    put "$worker" "$WORK/restore-ssh.sh" 0700
    conf=$(mktemp "$WORK/.boot-conf.XXXXXXXX")
    printf '[Service]\n# Restore SSH after the Xiaomi boot check when this drop-in is loaded.\nExecStartPost=/usr/bin/systemctl start dropbear.socket\n' > "$conf"
    put "$conf" "$BOOT_DROPIN" 0644
    printf 'SHELL=/bin/sh\nPATH=/usr/sbin:/usr/bin:/sbin:/bin\nMAILTO=""\n\n# Wait for the real boot-check completion, then perform bounded SSH rechecks.\n@reboot root /bin/sh /data/minas/ssh/restore-ssh.sh\n' > "$conf"
    put "$conf" "$CRON" 0644; rm -f "$conf"
    if [ -f "$home/.ssh/authorized_keys" ]; then put "$home/.ssh/authorized_keys" "$WORK/login-authorized-keys" 0600; fi
}
activate() {
    timeout 15 "$SYSTEMCTL" daemon-reload
    timeout 15 "$SYSTEMCTL" enable dropbear.socket
    timeout 15 "$SYSTEMCTL" reset-failed dropbear.socket || true
    timeout 15 "$SYSTEMCTL" start dropbear.socket
    timeout 15 "$SYSTEMCTL" reload crond.service || fail '无法重载 crond.service，开机恢复尚未验证'
    timeout 10 "$SYSTEMCTL" is-enabled --quiet dropbear.socket || fail 'SSH 未设置为开机启用'
    timeout 10 "$SYSTEMCTL" is-active --quiet dropbear.socket || fail 'SSH socket 未启动'
    loaded=$(timeout 10 "$SYSTEMCTL" show minas.boot_check.service -p DropInPaths --value)
    case "$loaded" in *"$BOOT_DROPIN"*) ;; *) fail '开机修复配置未被 systemd 加载' ;; esac
    if [ -f "$SHELL_DROPIN" ]; then
        loaded=$(timeout 10 "$SYSTEMCTL" show 'dropbear@minas-ssh-check.service' -p DropInPaths --value)
        case "$loaded" in *"$SHELL_DROPIN"*) ;; *) fail 'mi-shell 修复配置未被 systemd 加载' ;; esac
    fi
    safe "$PENDING"; rm -f "$PENDING"
    printf '%s\n' 'SSH socket 已启用；纯 Shell 开机恢复已安装。'
}
activation_state() {
    status_file=$(mktemp "$WORK/.activation.XXXXXXXX")
    printf '%s\n' "$1" > "$status_file"
    put "$status_file" "$WORK/shell-activation.status" 0600
    rm -f "$status_file"
}

WAIT_SECONDS=300
POLL_SECONDS=2
RECHECK_SECONDS=5
RECHECK_COUNT=12
BOOT_UNIT=minas.boot_check.service
SOCKET_UNIT=dropbear.socket

log() {
    logger -t ssh-autostart-fix -- "$*"
}
seconds() {
    read -r elapsed rest < /proc/uptime
    printf '%s\n' "${elapsed%%.*}"
}
boot_finished() {
    active= sub= start=0 end=0 result= job=
    state=$(timeout 8 /usr/bin/systemctl show "$BOOT_UNIT" \
        -p ActiveState -p SubState -p ExecMainStartTimestampMonotonic \
        -p ExecMainExitTimestampMonotonic -p Result -p Job 2>/dev/null) || return 1
    while IFS='=' read -r key value; do
        case "$key" in
            ActiveState) active=$value ;;
            SubState) sub=$value ;;
            ExecMainStartTimestampMonotonic) start=$value ;;
            ExecMainExitTimestampMonotonic) end=$value ;;
            Result) result=$value ;;
            Job) job=$value ;;
        esac
    done <<EOF_STATE
$state
EOF_STATE
    case "$start:$end" in *[!0-9:]*|:*|*:) return 1 ;; esac
    [ "$start" -gt 0 ] && [ "$end" -ge "$start" ] || return 1
    [ -z "$job" ] || [ "$job" = "0" ] || return 1
    case "$active:$sub" in
        active:exited|inactive:dead|failed:failed) return 0 ;;
        *) return 1 ;;
    esac
}
socket_active() {
    timeout 8 /usr/bin/systemctl is-active --quiet "$SOCKET_UNIT"
}
wait_for_boot() {
    while ! boot_finished; do
        if [ "$(seconds)" -ge "$deadline" ]; then
            log "ERROR: boot check did not finish within ${WAIT_SECONDS}s; SSH recovery aborted"
            return 1
        fi
        sleep "$POLL_SECONDS"
    done
}


shell_repair_main() {
prepare_layout
[ "$(id -u)" = 0 ] || fail '需要已有的 NAS root 执行权限'
for tool in awk stat mktemp flock timeout cmp readlink; do command -v "$tool" >/dev/null || fail "NAS 缺少 $tool"; done
private_dir "$WORK"
safe /data/minas/privileged
mkdir -p /data/minas/privileged
chmod 0755 /data/minas/privileged
private_dir "$STATE"
safe "$STATE/operations.lock"
exec 8>"$STATE/operations.lock"
flock -w 15 8 || fail '另一个 SSH 修复正在执行'
legacy_lock=/data/.minas-privileged/modules/sshrecovery/var/operations.lock
if [ -f "$legacy_lock" ]; then
    safe "$legacy_lock"
    if [ "$(stat -c '%d:%i' "$legacy_lock")" != "$(stat -c '%d:%i' "$STATE/operations.lock")" ]; then
        exec 6>"$legacy_lock"
        flock -w 15 6 || fail '旧版 SSH 恢复正在执行'
    fi
fi
flat_lock=/data/.minas-privileged/sshrecovery/operations.lock
if [ -f "$flat_lock" ]; then
    safe "$flat_lock"
    if [ "$(stat -c '%d:%i' "$flat_lock")" != "$(stat -c '%d:%i' "$STATE/operations.lock")" ] && { [ ! -f "$legacy_lock" ] || [ "$(stat -c '%d:%i' "$flat_lock")" != "$(stat -c '%d:%i' "$legacy_lock")" ]; }; then
        exec 5>"$flat_lock"
        flock -w 15 5 || fail '旧版 SSH 恢复正在执行'
    fi
fi
mode=${1:-}; shift || true
if [ "$mode" = --boot-repair ]; then
    prepare_account; repair_host; repair_shell
    timeout 15 "$SYSTEMCTL" daemon-reload
    exit 0
fi
if [ "$mode" = --activate ]; then
    # Consume the one-shot before waiting; a failed attempt must not become a
    # permanent minute-by-minute recovery loop. Portainer can queue it again.
    safe "$PENDING"; rm -f "$PENDING"
    trap 'result=$?; if [ "$result" -ne 0 ]; then activation_state failed; fi' EXIT
    deadline=$(( $(seconds) + WAIT_SECONDS )); wait_for_boot || fail "等待开机检查超时"
    prepare_account; repair_host; repair_shell; activate
    activation_state ready
    exit 0
fi
offline=0 user= public= worker=
while [ "$#" -gt 0 ]; do
    case "$1" in
        --user) user=$2; shift 2 ;; --public-key) public=$2; shift 2 ;;
        --worker) worker=$2; shift 2 ;; --offline) offline=1; shift ;;
        *) fail "未知参数：$1" ;;
    esac
done
case "$mode" in --recover|--install-boot-only) ;; *) fail '请选择 --recover 或 --install-boot-only' ;; esac
for unit in minas.boot_check.service dropbear.socket 'dropbear@.service' crond.service; do
    if [ "$offline" = 1 ]; then
        [ -f "/usr/lib/systemd/system/$unit" ] || [ -f "/lib/systemd/system/$unit" ] || [ -f "/etc/systemd/system/$unit" ] || fail "固件缺少 $unit"
    else
        timeout 10 "$SYSTEMCTL" cat "$unit" >/dev/null || fail "固件缺少 $unit"
    fi
done
command -v crond >/dev/null || fail 'NAS 缺少 crond'
if [ "$offline" != 1 ]; then deadline=$(( $(seconds) + WAIT_SECONDS )); wait_for_boot || fail "等待开机检查超时"; fi
prepare_account
if [ "$mode" = --recover ]; then
    case "$user" in u*) digits=${user#u}; case "$digits" in ''|*[!0-9]*) fail 'NAS 账户无效' ;; esac ;; *) fail 'NAS 账户无效' ;; esac
    [ -d "/home/$user" ] || fail '指定 NAS 账户不存在'
    [ -f "$public" ] || fail '缺少 SSH 公钥'
    [ "$(awk 'NF {n++} END {print n+0}' "$public")" = 1 ] || fail '一次只能安装一条公钥'
    # Client derives and validates this key with ssh-keygen; reject options here.
    awk 'NF {if(NF!=2 || $1 !~ /^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521))$/ || $2 !~ /^[A-Za-z0-9+\/=]+$/) exit 1}' "$public" || fail '公钥格式无效'
    private_dir "$STATE"; private_dir "$STATE/keys"
    put "$public" "$STATE/keys/$user.pub" 0600
    merge_keys "$public" "$home/.ssh/authorized_keys"
fi
repair_host; repair_shell
install_boot "$worker"
if [ "$offline" = 1 ]; then
    conf=$(mktemp "$WORK/.activate.XXXXXXXX")
    printf 'SHELL=/bin/sh\nPATH=/usr/sbin:/usr/bin:/sbin:/bin\n* * * * * root /bin/sh /data/minas/ssh/restore-ssh.sh --activate 2>&1 | logger -t ssh-autostart-fix\n' > "$conf"
    put "$conf" "$PENDING" 0644; rm -f "$conf"
    activation_state pending
    printf '%s\n' '已准备恢复文件；等待宿主机 Cron 激活，再验证真实 SSH 命令。'
else
    activate
fi

}

boot_main() {
set +e
if [ "${1:-}" = "--check" ]; then
    if boot_finished && socket_active; then
        printf '%s\n' 'boot-check finished; dropbear.socket active'
        exit 0
    fi
    exit 1
fi

prepare_layout
exec 9>/run/ssh-autostart-fix.lock
flock -n 9 || exit 0
deadline=$(( $(seconds) + WAIT_SECONDS ))
log "waiting for $BOOT_UNIT to finish (limit ${WAIT_SECONDS}s)"
wait_for_boot || exit 1
log "boot check finished: state=$active/$sub result=$result; checking SSH"
boot_worker=$0
[ -f "$boot_worker" ] || boot_worker=$WORK/restore-ssh.sh
if ! timeout 30 /bin/sh "$boot_worker" --boot-repair; then
    log 'ERROR: SSH login material or mi-shell compatibility repair failed'
    exit 1
fi

i=0
while [ "$i" -le "$RECHECK_COUNT" ]; do
    # If the vendor check runs again during this short window, wait for it again.
    wait_for_boot || exit 1
    if ! socket_active; then
        if timeout 10 /usr/bin/systemctl start "$SOCKET_UNIT"; then
            log "dropbear.socket restored after boot check (check $i/$RECHECK_COUNT)"
        else
            log "WARNING: could not start dropbear.socket (check $i/$RECHECK_COUNT)"
        fi
    fi
    if [ "$i" -eq "$RECHECK_COUNT" ]; then
        if socket_active; then
            log "SSH recovery verified; bounded rechecks finished"
            exit 0
        fi
        log "ERROR: SSH remains inactive after bounded rechecks"
        exit 1
    fi
    i=$((i + 1))
    sleep "$RECHECK_SECONDS"
done

}

case "${1:-}" in
    --recover|--install-boot-only|--boot-repair|--activate) shell_repair_main "$@" ;;
    ""|--check) boot_main "$@" ;;
    *) fail "未知模式：$1" ;;
esac
MINAS_SSH_REPAIR_SH_SOURCE
}

make_payload() {
    local mode=$1 offline=${2:-}
    [[ $mode != --recover ]] || valid_user "$NAS_USER" || fail 'NAS 账户无效'
    {
        printf '#!/bin/sh\nset -eu\numask 077\nstage=$(mktemp -d /tmp/minas-ssh-shell.XXXXXXXX)\n'
        printf 'trap '\''rm -rf -- "$stage"'\'' EXIT\n'
        printf 'cat > "$stage/restore-ssh.sh" <<'\''MINAS_REMOTE_REPAIR_SOURCE'\''\n'
        cat "$STAGE/restore-ssh.sh"; printf 'MINAS_REMOTE_REPAIR_SOURCE\n'
        printf 'cat > "$stage/public.key" <<'\''MINAS_REMOTE_PUBLIC_KEY'\''\n'
        cat "$STAGE/public.key"; printf '\nMINAS_REMOTE_PUBLIC_KEY\n'
        printf '/bin/sh "$stage/restore-ssh.sh" %s --worker "$stage/restore-ssh.sh"' "$mode"
        if [[ $mode == --recover ]]; then printf ' --user %s --public-key "$stage/public.key"' "$NAS_USER"; fi
        [[ -z $offline ]] || printf ' --offline'
        printf '\n'
    } > "$STAGE/payload.sh"
}
cert_directory() {
    local windows
    [[ -z $CERT_HOME ]] || return 0
    need powershell.exe wslpath
    windows=$(powershell.exe -NoLogo -NoProfile -NonInteractive -Command \
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(\$false); [Console]::Write([Environment]::GetFolderPath('LocalApplicationData'))") || fail '无法查询 Windows LocalAppData；请指定 --cert-dir'
    windows=${windows//$'\r'/}
    [[ $windows =~ ^[A-Za-z]:[\\/] && $windows != *$'\n'* ]] || fail 'Windows LocalAppData 路径异常；请指定 --cert-dir'
    CERT_HOME=$(wslpath -u "$windows")/minasCert
}
prepare_certificates() {
    local file key; local -a certs=() keys=()
    cert_directory
    [[ -d $CERT_HOME && -f $CERT_HOME/ca_chain.pem ]] || fail '缺少客户端 ca_chain.pem，请先登录小米客户端或指定 --cert-dir'
    for file in "$CERT_HOME/"*_cert.pem; do
        [[ -f $file ]] || continue
        key=${file%_cert.pem}_private_key.pem
        if [[ -f $key ]]; then certs+=("$file"); keys+=("$key"); fi
    done
    ((${#certs[@]} == 1)) || fail '证书目录必须只有一对匹配的客户端证书和私钥'
    CERT_ARGS=(--cacert "$CERT_HOME/ca_chain.pem" --cert "${certs[0]}" --key "${keys[0]}")
    # timeout prevents an unresponsive TLS server from hanging before curl.
    CN=$(timeout 15 openssl s_client -connect "$NAS_IP:443" -servername x </dev/null 2>/dev/null |
        openssl x509 -noout -subject -nameopt RFC2253 | sed -n 's/^subject=//p' |
        tr ',' '\n' | sed -n 's/^CN=//p') || fail '无法读取 NAS 证书名称'
    [[ $CN =~ ^[A-Za-z0-9.-]+$ ]] || fail 'NAS 证书名称无效'
}
webdav() {
    curl --silent --show-error --connect-timeout 5 --max-time 30 --globoff \
        "${CERT_ARGS[@]}" --config "$STAGE/webdav.conf" --resolve "$CN:5000:$NAS_IP" "$@"
}
cleanup() {
    local rc=$?
    trap - EXIT
    if [[ -n ${UPLOAD_PATH:-} ]]; then
        webdav -X DELETE "https://$CN:5000$UPLOAD_PATH" -o /dev/null >/dev/null 2>&1 || true
        if [[ -n ${PROBE_PATH:-} ]]; then webdav -X DELETE "https://$CN:5000$PROBE_PATH" -o /dev/null >/dev/null 2>&1 || true; fi
    fi
    [[ -z ${STAGE:-} ]] || rm -rf -- "$STAGE"
    exit "$rc"
}
legacy_bootstrap() {
    need curl jq openssl timeout
    prepare_certificates
    curl --fail --silent --show-error --connect-timeout 5 --max-time 25 "${CERT_ARGS[@]}" \
        --resolve "$CN:443:$NAS_IP" -X POST -H 'Content-Type: application/json' -d '{}' \
        "https://$CN/cgi-bin/luci/filemgr/get_pool_info" -o "$STAGE/pool.json"
    # Parse username/password from the same object, including JSON escapes.
    jq -ce '[.. | objects | select((.username|type)=="string" and (.password|type)=="string")][0] // error("missing account")' \
        "$STAGE/pool.json" > "$STAGE/account.json" || fail '未获取到 WebDAV 账户'
    local account provided=$NAS_USER
    account=$(jq -r '.username' "$STAGE/account.json"); valid_user "$account" || fail 'WebDAV 账户无效'
    [[ -z $provided || $provided == "$account" ]] || fail '指定 --user 与当前客户端证书账户不同，请切换客户端账户'
    NAS_USER=$account
    # curl config quoting handles quotes/backslashes; credentials stay off argv.
    jq -r '"user = " + ((.username + ":" + .password)|@json)' "$STAGE/account.json" > "$STAGE/webdav.conf"
    local name="ssh-repair-${STAGE##*.}-$(date +%s).sh" host escaped trigger status
    UPLOAD_PATH=/pool0/data/$name
    host=/nas/pool0/$NAS_USER/data/$name
    if [[ $PROBE == 1 ]]; then
        PROBE_PATH=$UPLOAD_PATH.probe
        printf '#!/bin/sh\nset -eu\numask 077\nid -u > %s.probe\nchown %s:%s %s.probe\n' \
            "$host" "$NAS_USER" "$NAS_USER" "$host" > "$STAGE/payload.sh"
    else
        make_payload --recover
    fi
    status=$(webdav -T "$STAGE/payload.sh" "https://$CN:5000$UPLOAD_PATH" -o /dev/null -w '%{http_code}')
    case "$status" in 200|201|204) ;; *) fail "恢复脚本上传失败，HTTP $status" ;; esac
    # Octal-encode the validated path just as the upstream protocol expects.
    escaped=$(printf '%s' "$host" | od -vAn -to1 | tr -s '[:space:]' '\n' | sed '/^$/d;s/^/\\/' | tr -d '\n')
    trigger=$(jq -nr --arg command "\";sh \$(printf '$escaped');\"" '$command|@uri')
    printf '%s\n' '已上传临时脚本，尝试旧证书引导；HTTP 返回不代表执行成功。'
    webdav "https://$CN:5000/pool0/video/__X2${trigger}__.ts" -o /dev/null || true
    if [[ $PROBE == 1 ]]; then
        for ((i=0; i<3; i++)); do
            if webdav --fail "https://$CN:5000$PROBE_PATH" -o "$STAGE/probe" 2>/dev/null && [[ $(cat "$STAGE/probe") == 0 ]]; then
                printf '%s\n' '旧引导可执行 root 命令，未修改 SSH 或开机配置。'; return
            fi
            sleep 1
        done
        fail '旧证书引导未通过验证，请使用 SSH 恢复插件或 Portainer'
    fi
}
portainer_call() {
    local path=$1 body=${2:-}
    local -a options=()
    [[ -z $body ]] || options=(-H 'Content-Type: application/json' --data-binary "@$body")
    curl --fail --silent --show-error --connect-timeout 5 --max-time 60 \
        --config "$STAGE/portainer.conf" "${options[@]}" "$PORTAINER_URL$path"
}
portainer_exec() {
    local command=$1 exec_id
    jq -n --slurpfile command "$command" '{AttachStdout:true,AttachStderr:true,Tty:true,User:"0",Cmd:$command[0]}' > "$STAGE/exec.json"
    exec_id=$(portainer_call "$PREFIX/containers/$CONTAINER_ID/exec" "$STAGE/exec.json" | jq -er '.Id')
    [[ $exec_id =~ ^[a-fA-F0-9]+$ ]] || fail 'Portainer 返回了无效 Exec ID'
    printf '{"Detach":false,"Tty":true}\n' > "$STAGE/start.json"
    portainer_call "$PREFIX/exec/$exec_id/start" "$STAGE/start.json" > "$STAGE/exec-output"
    portainer_call "$PREFIX/exec/$exec_id/json" > "$STAGE/exec-state.json"
    jq -e '.Running == false and .ExitCode == 0' "$STAGE/exec-state.json" >/dev/null || {
        tail -c 2000 "$STAGE/exec-output" >&2; fail '容器恢复命令失败';
    }
    cat "$STAGE/exec-output"
}
portainer_bootstrap() {
    need curl jq gzip base64
    [[ -n $PORTAINER_URL ]] || PORTAINER_URL=http://$NAS_IP:9001
    [[ $PORTAINER_URL =~ ^https?://[A-Za-z0-9.:-]+/?$ ]] || fail 'Portainer URL 无效，请使用不含凭据和查询参数的根地址'
    PORTAINER_URL=${PORTAINER_URL%/}
    [[ -n $PORTAINER_USER ]] || read -r -p 'Portainer 用户名：' PORTAINER_USER
    local password token destination raw
    read -r -s -p 'Portainer 密码（不保存）：' password; printf '\n'
    # jq reads credentials through stdin, not process arguments.
    printf '%s\0%s' "$PORTAINER_USER" "$password" | jq -Rs 'split("\u0000")|{Username:.[0],Password:.[1]}' > "$STAGE/auth.json"
    unset password
    : > "$STAGE/portainer.conf"
    token=$(portainer_call /api/auth "$STAGE/auth.json" | jq -er '.jwt')
    [[ $token =~ ^[A-Za-z0-9_.-]+$ ]] || fail 'Portainer 认证令牌无效'
    printf 'header = "Authorization: Bearer %s"\n' "$token" > "$STAGE/portainer.conf"; unset token
    rm -f "$STAGE/auth.json"
    portainer_call /api/endpoints > "$STAGE/endpoints.json"
    [[ -n $ENDPOINT ]] || ENDPOINT=$(jq -er 'if length==1 then .[0].Id else error("use --endpoint") end' "$STAGE/endpoints.json") || fail '请通过 --endpoint 指定 Portainer 环境 ID'
    [[ $ENDPOINT =~ ^[0-9]+$ ]] || fail 'Portainer 环境 ID 无效'
    jq -e --argjson id "$ENDPOINT" 'any(.[]; .Id==$id)' "$STAGE/endpoints.json" >/dev/null || fail '指定 Portainer 环境不存在'
    PREFIX=/api/endpoints/$ENDPOINT/docker
    portainer_call "$PREFIX/containers/json?all=1" > "$STAGE/containers.json"
    CONTAINER_ID=$(jq -er --arg name "$CONTAINER" '[.[]|select(.Id==$name or ((.Names//[])|index("/"+$name))) ] | if length==1 then .[0].Id else error("container not unique") end' "$STAGE/containers.json") || fail '请指定现有运行中应急容器名称'
    [[ $CONTAINER_ID =~ ^[a-fA-F0-9]+$ ]] || fail '容器 ID 无效'
    portainer_call "$PREFIX/containers/$CONTAINER_ID/json" > "$STAGE/container.json"
    destination=$(jq -er 'select(.State.Running==true) | [.Mounts[]|select(.Type=="bind" and .Source=="/" and .RW==true)] | if length==1 then .[0].Destination else error("missing root bind") end' "$STAGE/container.json") || fail '容器必须运行并可写绑定宿主机 /'
    [[ $destination == /* && $destination != *'/../'* && $destination != */.. ]] || fail '宿主机映射目标无效'
    jq -n --arg root "$destination" '["chroot",$root,"/bin/sh","-c","find /home -maxdepth 1 -type d -name '\''u[0-9]*'\''"]' > "$STAGE/command.json"
    raw=$(portainer_exec "$STAGE/command.json")
    select_user "$(printf '%s\n' "$raw" | tr -d '\r' | sed -n 's@^/home/\(u[0-9][0-9]*\)$@\1@p')"
    make_payload --recover offline
    # Keep the single chroot argument below Linux MAX_ARG_STRLEN even with the
    # offline migration coordinator embedded in the shell worker.
    {
        printf 'set -eu\numask 077\nstage=$(mktemp -d /run/minas-ssh-transport.XXXXXXXX)\ntrap '\''rm -rf -- "$stage"'\'' EXIT\n'
        printf 'base64 -d <<'\''MINAS_SSH_COMPRESSED_PAYLOAD'\'' | gzip -d > "$stage/payload.sh"\n'
        gzip -c "$STAGE/payload.sh" | base64
        printf 'MINAS_SSH_COMPRESSED_PAYLOAD\n/bin/sh "$stage/payload.sh"\n'
    } > "$STAGE/transport.sh"
    [[ $(wc -c < "$STAGE/transport.sh") -lt 130000 ]] || fail '恢复载荷超过容器单参数限制'
    jq -n --arg root "$destination" --rawfile script "$STAGE/transport.sh" '["chroot",$root,"/bin/sh","-c",$script]' > "$STAGE/command.json"
    portainer_exec "$STAGE/command.json"
    printf '%s\n' '等待宿主机 Cron 激活，随后验证真实 SSH 命令。'
}
main() {
    NAS_IP= NAS_USER= CERT_HOME= IDENTITY=$HOME/.ssh/id_ed25519
    LEGACY=0 PROBE=0 CHECK=0 BOOT_ONLY=0 PORTAINER=0
    PORTAINER_URL= PORTAINER_USER= ENDPOINT= CONTAINER=test
    while (($#)); do
        case $1 in
            -h|--help) usage; return ;;
            --user|--identity|--cert-dir|--portainer-url|--portainer-user|--endpoint|--container)
                (($#>=2)) && [[ -n $2 ]] || fail "$1 缺少参数"
                case $1 in --user) NAS_USER=$2 ;; --identity) IDENTITY=$2 ;; --cert-dir) CERT_HOME=$2 ;;
                    --portainer-url) PORTAINER_URL=$2 ;; --portainer-user) PORTAINER_USER=$2 ;; --endpoint) ENDPOINT=$2 ;; --container) CONTAINER=$2 ;; esac
                shift 2 ;;
            --legacy-bootstrap) LEGACY=1; shift ;; --probe-bootstrap) PROBE=1; shift ;;
            --check) CHECK=1; shift ;; --install-boot-only) BOOT_ONLY=1; shift ;; --portainer) PORTAINER=1; shift ;;
            -*) fail "未知选项：$1" ;; *) [[ -z $NAS_IP ]] || fail '只能指定一个 NAS IP'; NAS_IP=$1; shift ;;
        esac
    done
    ((CHECK+PROBE+BOOT_ONLY+PORTAINER<=1 && LEGACY+PORTAINER<=1 && LEGACY+BOOT_ONLY<=1)) || fail '检查、探测、仅安装开机修复和 Portainer 选项不可混用'
    [[ -z $NAS_USER ]] || valid_user "$NAS_USER" || fail '--user 应为 u数字'
    [[ -n $NAS_IP ]] || read -r -p 'NAS IPv4 地址：' NAS_IP
    ipv4 "$NAS_IP" || fail 'NAS IPv4 地址无效'
    need ssh ssh-keygen mktemp awk sort cat
    SSH_OPTIONS=(-i "$IDENTITY" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 -o ServerAliveInterval=5 -o ServerAliveCountMax=2)
    if [[ $CHECK == 1 ]]; then ssh_ready || fail 'SSH 未通过真实 root 命令检查'; printf '%s\n' 'SSH 公钥认证与 root 系统命令均正常。'; return; fi
    STAGE=$(mktemp -d /tmp/minas-ssh-client.XXXXXXXX)
    trap cleanup EXIT; trap 'exit 130' INT; trap 'exit 143' HUP TERM
    write_repair_sources
    if [[ $PROBE == 1 ]]; then legacy_bootstrap; return; fi
    if [[ $BOOT_ONLY == 1 ]]; then
        ssh_ready || fail '请先恢复可执行真实 root 命令的 SSH'
        : > "$STAGE/public.key"; make_payload --install-boot-only
        ssh "${SSH_OPTIONS[@]}" "root@$NAS_IP" /bin/sh -s < "$STAGE/payload.sh"
    else
        prepare_identity
        if [[ $PORTAINER == 1 ]]; then portainer_bootstrap
        elif [[ $LEGACY == 0 ]] && ssh_ready; then
            local raw
            raw=$(ssh "${SSH_OPTIONS[@]}" "root@$NAS_IP" "find /home -maxdepth 1 -type d -name 'u[0-9]*'")
            select_user "$(printf '%s\n' "$raw" | tr -d '\r' | sed -n 's@^/home/\(u[0-9][0-9]*\)$@\1@p')"
            make_payload --recover
            ssh "${SSH_OPTIONS[@]}" "root@$NAS_IP" /bin/sh -s < "$STAGE/payload.sh"
        else legacy_bootstrap; fi
    fi
    local attempts=20 i ready_seen=0 audit
    audit='test -s /data/minas/ssh/restore-ssh.sh && test -s /data/minas/ssh/login-authorized-keys && grep -Fq "@reboot root /bin/sh /data/minas/ssh/restore-ssh.sh" /etc/cron.d/keep-ssh && systemctl is-enabled --quiet dropbear.socket && systemctl is-active --quiet dropbear.socket && systemctl show minas.boot_check.service -p DropInPaths --value | grep -Fq /etc/systemd/system/minas.boot_check.service.d/90-keep-ssh.conf'
    if [[ $PORTAINER == 1 ]]; then audit+=' && test "$(cat /data/minas/ssh/shell-activation.status)" = ready'; fi
    [[ $PORTAINER != 1 ]] || attempts=50
    for ((i=0; i<attempts; i++)); do
        if ssh_ready; then
            ready_seen=1
            if ssh "${SSH_OPTIONS[@]}" "root@$NAS_IP" "$audit" >/dev/null 2>&1; then
                printf '%s\n' 'SSH 已开启，纯 Shell 开机修复已安装。请保留登录私钥。'; return
            fi
        fi
        sleep 2
    done
    [[ $ready_seen == 0 ]] || fail 'SSH 已可用，但开机修复文件或加载状态未通过验证，请查看 ssh-autostart-fix 日志'
    fail '未通过真实 SSH 命令验证。固件可能已封堵旧证书入口，请使用 SSH 恢复插件或 Portainer。'
}
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main --install-boot-only "$@"; fi
