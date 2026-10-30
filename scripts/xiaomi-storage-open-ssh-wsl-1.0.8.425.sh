#!/bin/bash
# Based on Scruel's minas-enable-ssh_wsl.sh (Enhanced, 2026.08):
# https://github.com/scruel/xiaomi-storage-minas-tool/blob/main/minas-enable-ssh_wsl.sh
# Compatibility patch 2.1.3: original certificate/WebDAV bootstrap first,
# mi-shell support, then the exact same boot worker as the plugin/installer.

echo "################################################"
echo "#   __  __    ___    _   _      _      ____    #"
echo "#  |  \/  |  |_ _|  | \ | |    / \    / ___|   #"
echo "#  | |\/| |   | |   |  \| |   / _ \   \___ \   #"
echo "#  | |  | |   | |   | |\  |  / ___ \   ___) |  #"
echo "#  |_|  |_|  |___|  |_| \_| /_/   \_\ \____/   #"
echo "################################################"
echo "小米智能存储开启 SSH    原作 @Scruel / 兼容补丁 2.1.3"
printf '%s\n' '' \
    '首次使用请先在 WSL Ubuntu 中安装依赖（不是在 NAS 上执行）：' \
    '  sudo apt update' \
    '  sudo apt install openssh-client curl jq openssl coreutils dnsutils iproute2' \
    '依赖已安装可忽略此提示；本脚本不会自动安装。' ''

VERBOSE=0
NAS_IP=
IDENTITY=$HOME/.ssh/id_ed25519
CERT_HOME=
while [ $# -gt 0 ]; do
    case "$1" in
        -v) VERBOSE=1; shift ;;
        --identity|--cert-dir)
            [ $# -ge 2 ] && [ -n "$2" ] || { echo "$1 缺少参数" >&2; exit 1; }
            if [ "$1" = --identity ]; then IDENTITY=$2; else CERT_HOME=$2; fi
            shift 2 ;;
        -h|--help)
            echo '2.1.3 用法：bash xiaomi-storage-open-ssh-wsl.sh [NAS IPv4] [-v] [--identity 私钥] [--cert-dir 证书目录]'
            echo '未开启 SSH：客户端证书 → WebDAV 上传/触发 → 验证 SSH → 安装同源开机修复。'
            exit 0 ;;
        -*) echo "不支持的选项：$1；已有 SSH 请使用 install-ssh-autostart-fix.sh。" >&2; exit 1 ;;
        *) [ -z "$NAS_IP" ] || { echo '只能指定一个 NAS IP' >&2; exit 1; }; NAS_IP=$1; shift ;;
    esac
done
umask 077

# ---------- 清理临时文件 ----------
STAGE=$(mktemp -d /tmp/minas-enable-ssh.XXXXXXXX) || exit 1
UPLOAD_PATH=
cleanup() {
    if [ -n "$UPLOAD_PATH" ] && [ -f "$STAGE/webdav.conf" ]; then
        curl -s --connect-timeout 5 --max-time 10 "${CERT_ARGS[@]}" --config "$STAGE/webdav.conf" \
            --resolve "$CN:5000:$NAS_IP" -X DELETE "https://$CN:5000$UPLOAD_PATH" -o /dev/null || true
    fi
    case "$STAGE" in /tmp/minas-enable-ssh.*) rm -rf -- "$STAGE" ;; esac
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
check_commands() {
    for cmd in openssl curl ssh ssh-keygen jq timeout; do
        command -v "$cmd" >/dev/null 2>&1 || { echo "命令 '$cmd' 未找到，请安装后再试。" >&2; exit 1; }
    done
}
check_commands

# ---------- 获取 NAS IP ----------
valid_ip() {
    local part; local -a parts
    [[ $1 =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    IFS=. read -r -a parts <<< "$1"
    for part in "${parts[@]}"; do ((10#$part <= 255)) || return 1; done
}
read_nas_ip() {
    while true; do
        read -r -p '请输入小米智能存储的内网 IP 地址: ' NAS_IP || return 1
        if valid_ip "$NAS_IP"; then printf '%s\n' "$NAS_IP"; return; fi
        echo 'IP 地址无效，请重新输入。' >&2
    done
}
if [[ -z "$NAS_IP" ]]; then
    echo "尝试自动获取小米智能存储的内网 IP..."
    if command -v dig >/dev/null && command -v ip >/dev/null; then
        PREFIX=$(ip -4 addr show | grep -v 127.0.0 | grep inet | head -1 | awk '{print $2}' | cut -d/ -f1 | cut -d. -f1-3)
        if valid_ip "$PREFIX.1"; then
            for i in {1..254}; do
                name=$(dig +time=1 +tries=1 -x "$PREFIX.$i" +short 2>/dev/null)
                [[ "$name" =~ minas ]] && { NAS_IP="$PREFIX.$i"; break; }
            done
        fi
    fi
    if [[ -z "$NAS_IP" ]]; then
        echo "无法自动找到设备，请手动输入。" >&2
        NAS_IP=$(read_nas_ip) || exit 1
    fi
fi
valid_ip "$NAS_IP" || { echo 'NAS IP 无效' >&2; exit 1; }
echo "小米智能存储 IP: $NAS_IP"

# ---------- 获取 CN (证书通用名) ----------
echo "正在从 NAS 获取证书 CN..."
CN=$(timeout 15 openssl s_client -connect "$NAS_IP:443" -servername x </dev/null 2>/dev/null |
    openssl x509 -noout -subject -nameopt RFC2253 | sed -n 's/.*CN *= *\([^,]*\).*/\1/p')
[[ "$CN" =~ ^[A-Za-z0-9.-]+$ ]] || { echo '无法提取有效证书 CN，请检查设备状态。' >&2; exit 1; }
echo "CN: $CN"

# ---------- 准备证书文件 ----------
check_cert_files() { [[ -f "$CA_FILE" && -f "$CERT_FILE" && -f "$KEY_FILE" ]]; }
prep_certfiles() {
    local given=$CERT_HOME file; local -a certs
    TRY_CNT=0
    while true; do
        if [[ -n "$given" ]]; then
            CERT_HOME=$given
        elif command -v cmd.exe >/dev/null && command -v wslpath >/dev/null && [ $TRY_CNT -lt 3 ]; then
            LOCALAPPDATA=$(wslpath "$(cmd.exe /d /c 'chcp 65001 >nul & echo %LOCALAPPDATA%' 2>/dev/null | tr -d '\r')" 2>/dev/null)
            CERT_HOME="$LOCALAPPDATA/minasCert"
        else
            CERT_HOME=/tmp/minascert
            mkdir -p "$CERT_HOME"
        fi
        CA_FILE="$CERT_HOME/ca_chain.pem"
        certs=()
        for file in "$CERT_HOME/"*_cert.pem; do [[ ! -f "$file" ]] || certs+=("$file"); done
        [[ ${#certs[@]} -le 1 ]] || { echo '证书目录有多对证书，请用 --cert-dir 指定当前登录账户的一对证书。' >&2; exit 1; }
        CERT_FILE=${certs[0]:-}
        KEY_FILE=${CERT_FILE%_cert.pem}_private_key.pem
        if check_cert_files; then echo "证书文件已就绪，目录：$CERT_HOME"; break; fi
        echo "证书文件缺失，请登录小米客户端或将证书放到 $CERT_HOME，再按回车继续。" >&2
        read -r || exit 1
        TRY_CNT=$((TRY_CNT + 1))
    done
}
prep_certfiles
CERT_ARGS=(--cacert "$CA_FILE" --cert "$CERT_FILE" --key "$KEY_FILE")

# ---------- 获取 WebDAV 凭证 ----------
echo "获取 WebDAV 凭证..."
RESP=$(curl -fsS --connect-timeout 5 --max-time 30 "${CERT_ARGS[@]}" --resolve "$CN:443:$NAS_IP" \
    -X POST -H "Content-Type: application/json" -d '{}' "https://$CN/cgi-bin/luci/filemgr/get_pool_info") || exit 1
ACCOUNT=$(printf '%s' "$RESP" | jq -ce '[..|objects|select((.username|type)=="string" and (.password|type)=="string")][0] // error("missing account")') || { echo '无法解析 WebDAV 凭证。' >&2; exit 1; }
WDV_USER=$(printf '%s' "$ACCOUNT" | jq -r .username)
WDV_PWD=$(printf '%s' "$ACCOUNT" | jq -r .password)
[[ "$WDV_USER" =~ ^u[0-9]+$ && -n "$WDV_PWD" ]] || { echo 'WebDAV 账户格式无效。' >&2; exit 1; }
echo "WebDAV 用户凭证解析成功: $WDV_USER"
# Passwords stay off stdout and curl argv, including verbose mode.
printf '%s\0%s' "$WDV_USER" "$WDV_PWD" | jq -Rrs 'split("\u0000")|"user = "+((.[0]+":"+.[1])|@json)' > "$STAGE/webdav.conf"
unset RESP ACCOUNT WDV_PWD

# ---------- 生成 SSH 密钥 ----------
mkdir -p "$(dirname "$IDENTITY")"
if [[ -f "$IDENTITY" ]]; then
    echo '密钥文件已存在，使用原私钥对应的公钥。'
elif [[ -e "$IDENTITY.pub" ]]; then
    echo '仅有公钥而没有对应私钥，已停止以免覆盖。请用 --identity 指定现有私钥。' >&2; exit 1
else
    echo '生成 SSH 密钥对 (ed25519)...'
    ssh-keygen -q -t ed25519 -N '' -f "$IDENTITY" -C '' </dev/null || exit 1
fi
ssh-keygen -y -f "$IDENTITY" | awk '{print $1 " " $2}' > "$STAGE/public.key"
ssh-keygen -lf "$STAGE/public.key" >/dev/null || { echo '公钥校验失败' >&2; exit 1; }
PUB_KEY=$(cat "$STAGE/public.key")

# ---------- 准备 enable-ssh.sh 脚本 ----------
REMOTE_NAME=enable-ssh-${STAGE##*.}.sh
REMOTE_FILE=/nas/pool0/$WDV_USER/data/$REMOTE_NAME
cat > "$STAGE/enable-ssh.sh" << EOF
#!/bin/sh
set -eu
umask 077
[ ! -f /etc/passwd.bak ] && cp /etc/passwd /etc/passwd.bak || true
mkdir -p /home/rootx/.ssh
touch /home/rootx/.profile
[ -e /home/rootx/models ] || ln -s /firmware/models /home/rootx/models
# Retain old firmware login authorization when moving the root home.
if grep -q '^root:[^:]*:0:0:[^:]*:/root:' /etc/passwd && [ -f /root/.ssh/authorized_keys ]; then
    merged=\$(mktemp /tmp/minas-auth-merge.XXXXXXXX)
    { cat /home/rootx/.ssh/authorized_keys 2>/dev/null || true; printf '\n'; cat /root/.ssh/authorized_keys; } | awk 'NF && !seen[\$0]++' > "\$merged"
    cat "\$merged" > /home/rootx/.ssh/authorized_keys; rm -f "\$merged"
fi
sed -i '/^root:/s|:/root:|:/home/rootx:|' /etc/passwd
DOCKER_CMD='export PATH="\$PATH:/data/docker"'
if ! grep -Fqx "\$DOCKER_CMD" /home/rootx/.profile; then printf '%s\n' "\$DOCKER_CMD" >> /home/rootx/.profile; fi
# Keep existing login authorization instead of replacing it.
grep -Fqx '$PUB_KEY' /home/rootx/.ssh/authorized_keys 2>/dev/null || printf '\n%s\n' '$PUB_KEY' >> /home/rootx/.ssh/authorized_keys
chmod 700 /home/rootx/.ssh
chmod 600 /home/rootx/.ssh/authorized_keys
echo 'DROPBEAR_EXTRA_ARGS=" -s"' > /etc/default/dropbear
usermod -s /bin/sh root

# New firmware ignores root's shell and starts mi-shell. Scope the Bash bind
# to Dropbear services, preserving the vendor binary and existing custom config.
if [ -e /usr/sbin/mi-shell ]; then
    normal=/usr/bin/bash.bash
    [ -x "\$normal" ] || normal=/bin/bash
    [ -x "\$normal" ] || { echo '新固件缺少可用 Bash，未报告成功' >&2; exit 1; }
    normal=\$(readlink -f "\$normal"); limited=\$(readlink -f /usr/sbin/mi-shell)
    case "\$normal:\$limited" in *[!a-zA-Z0-9_./:-]*) exit 1 ;; esac
    mkdir -p /etc/systemd/system/dropbear@.service.d
    conf=/etc/systemd/system/dropbear@.service.d/90-normal-shell.conf
    candidate=\$(mktemp /tmp/minas-shell-conf.XXXXXXXX)
    printf '[Service]\nBindReadOnlyPaths=%s:%s\n' "\$normal" "\$limited" > "\$candidate"
    if [ -e "\$conf" ] && ! cmp -s "\$candidate" "\$conf"; then rm -f "\$candidate"; echo '已有不同的 SSH shell 配置，未覆盖' >&2; exit 1; fi
    cp "\$candidate" "\$conf"; chmod 644 "\$conf"; rm -f "\$candidate"
    systemctl daemon-reload
fi
systemctl enable dropbear.socket
systemctl reset-failed dropbear.socket || true
systemctl start dropbear.socket
# Do not write RPMB: persistence uses the shared boot-repair worker below.
rm -f '$REMOTE_FILE'
EOF
sed -i 's/\r$//' "$STAGE/enable-ssh.sh"

# ---------- 上传 enable-ssh.sh 到 NAS ----------
echo '上传 enable-ssh.sh 到 NAS ...'
UPLOAD_PATH=/pool0/data/$REMOTE_NAME
HTTP_CODE=$(curl -sS --connect-timeout 5 --max-time 30 -w '%{http_code}' -o /dev/null \
    --config "$STAGE/webdav.conf" "${CERT_ARGS[@]}" --resolve "$CN:5000:$NAS_IP" \
    -T "$STAGE/enable-ssh.sh" "https://$CN:5000$UPLOAD_PATH") || exit 1
case "$HTTP_CODE" in 200|201|204) ;; *) echo "上传脚本失败，HTTP $HTTP_CODE" >&2; exit 1 ;; esac
echo "上传脚本成功 (HTTP $HTTP_CODE)"

# ---------- 触发执行 enable-ssh.sh ----------
echo '请求执行 enable-ssh.sh 脚本...'
ESCAPED=$(printf '%s' "$REMOTE_FILE" | od -vAn -to1 | tr -s '[:space:]' '\n' | sed '/^$/d;s/^/\\/' | tr -d '\n')
TRIGGER=$(jq -nr --arg command "\";sh \$(printf '$ESCAPED');\"" '$command|@uri')
P="/pool0/video/__X2${TRIGGER}__.ts"
curl -sS -g --connect-timeout 5 --max-time 30 --config "$STAGE/webdav.conf" "${CERT_ARGS[@]}" --resolve "$CN:5000:$NAS_IP" "https://$CN:5000$P" -o /dev/null || true
sleep 3

# ---------- 测试 SSH 连接 ----------
echo '测试 SSH 连接与真实 root 命令...'
SSH_OPTIONS=(-i "$IDENTITY" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 -o ServerAliveInterval=5 -o ServerAliveCountMax=2)
READY=0
for ((i=0; i<10; i++)); do
    RESULT=$(ssh "${SSH_OPTIONS[@]}" "root@$NAS_IP" 'printf "__MINAS_ROOT_COMMAND_OK__\n"; id -u' 2>/dev/null) || RESULT=
    if [[ ${RESULT//$'\r'/} == $'__MINAS_ROOT_COMMAND_OK__\n0' ]]; then READY=1; break; fi
    sleep 2
done
if [[ $READY != 1 ]]; then
    echo 'SSH 未通过验证。固件可能封堵了上游证书触发入口；HTTP 成功不代表已执行，未安装开机修复。' >&2
    echo '请使用预先安装的 SSH 恢复插件或已有管理入口；本脚本不会自动切换其他引导方式。' >&2
    exit 1
fi

# ---------- 开启成功后，复用插件/独立安装器的开机修复 ----------
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



private_dir() {
    case "$1" in /data/minas/*)

        safe /data/minas; mkdir -p /data/minas; chmod 0755 /data/minas ;;
    esac
    safe "$1"; mkdir -p "$1"; chmod 0700 "$1"
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

exec 9>/run/ssh-autostart-fix.lock
flock -n 9 || exit 0
deadline=$(( $(seconds) + WAIT_SECONDS ))
log "waiting for $BOOT_UNIT to finish (limit ${WAIT_SECONDS}s)"
wait_for_boot || exit 1
log "boot check finished: state=$active/$sub result=$result; checking SSH"
if ! timeout 30 /bin/sh "$0" --boot-repair; then
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

write_repair_sources || exit 1
echo 'SSH 已开启，正在安装同源开机修复...'
{
    printf '#!/bin/sh\nset -eu\numask 077\nstage=$(mktemp -d /tmp/minas-boot-install.XXXXXXXX)\n'
    printf 'trap '\''rm -rf -- "$stage"'\'' EXIT\n'
    printf 'cat > "$stage/restore-ssh.sh" <<'\''MINAS_REMOTE_WORKER'\''\n'
    cat "$STAGE/restore-ssh.sh"
    printf 'MINAS_REMOTE_WORKER\n'
    printf '/bin/sh "$stage/restore-ssh.sh" --install-boot-only --worker "$stage/restore-ssh.sh"\n'
} > "$STAGE/install-boot.sh"
if ! ssh "${SSH_OPTIONS[@]}" "root@$NAS_IP" /bin/sh -s < "$STAGE/install-boot.sh"; then
    echo 'SSH 已开启，但开机修复安装失败。请稍后运行独立安装脚本，或使用插件安装开机修复。' >&2; exit 1
fi
AUDIT='test -s /data/minas/ssh/restore-ssh.sh && test -s /data/minas/ssh/login-authorized-keys && grep -Fq "@reboot root /bin/sh /data/minas/ssh/restore-ssh.sh" /etc/cron.d/keep-ssh && systemctl is-active --quiet dropbear.socket && systemctl show minas.boot_check.service -p DropInPaths --value | grep -Fq /etc/systemd/system/minas.boot_check.service.d/90-keep-ssh.conf'
if ! ssh "${SSH_OPTIONS[@]}" "root@$NAS_IP" "$AUDIT"; then
    echo 'SSH 已开启，但开机修复未通过验证，不能保证重启后仍可用。' >&2; exit 1
fi
echo 'SSH 连接成功，开机修复已安装！请保留电脑上的登录私钥。'
printf 'SSH 登录命令：ssh -i "%s" root@%s\n' "$IDENTITY" "$NAS_IP"
