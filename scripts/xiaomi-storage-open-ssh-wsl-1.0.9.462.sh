#!/usr/bin/env bash
# SSH 2.2.6; authenticated UCI entry researched by lvmaple/minas_plugin.
set -Eeuo pipefail
umask 077
fail() { printf '失败：%s\n' "$*" >&2; exit 1; }
verbose() { if [[ $VERBOSE == 1 ]]; then printf '[诊断] %s\n' "$*" >&2; fi; }
usage() {
    cat <<'HELP'
小米智能存储开启 SSH 2.2.6（1.0.9.462 新入口，WSL / Linux）
用法：bash xiaomi-storage-open-ssh-wsl-1.0.9.462.sh [NAS IPv4] [--identity 私钥] [--cert-dir 证书目录] [-v]
客户端证书 → 临时 SMB /etc 共享 → 一次性 Cron → Bash 映射 → 验证 root SSH。
验证成功后询问是否调用同目录 install-ssh-autostart-fix.sh；默认不安装。
SSH 私钥存在则复用，不存在则生成 Ed25519 密钥；小米客户端 TLS 证书须已有。
-v 显示阶段和验证结果，不输出密码、私钥或网络认证调试。
NAS 分步骤执行日志保留在当前用户 pool0/data 的 ssh-open-执行编号.log，电脑端也会尝试保存副本。
首次使用在 WSL Ubuntu 安装依赖（不是在 NAS 上执行）：
  sudo apt update
  sudo apt install openssh-client curl jq openssl coreutils smbclient
上游及本改写均未在纯原厂设备完成首次开启端到端验收。
HELP
}
ipv4() {
    local part; local -a parts
    [[ $1 =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    IFS=. read -r -a parts <<< "$1"
    for part in "${parts[@]}"; do
        [[ $part == 0 || $part != 0* ]] && ((10#$part <= 255)) || return 1
    done
}
NAS_IP= CERT_HOME= VERBOSE=0
IDENTITY=$HOME/.ssh/id_ed25519
while (($#)); do
    case $1 in
        --identity|--cert-dir)
            (($# >= 2)) && [[ -n $2 ]] || fail "$1 缺少参数"
            if [[ $1 == --identity ]]; then IDENTITY=$2; else CERT_HOME=$2; fi
            shift 2 ;;
        -v) VERBOSE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) fail "未知选项：$1" ;;
        *) [[ -z $NAS_IP ]] || fail '只能指定一个 NAS IP'; NAS_IP=$1; shift ;;
    esac
done
usage
for tool in curl jq openssl ssh ssh-keygen smbclient timeout mktemp cmp; do
    command -v "$tool" >/dev/null || fail "命令 '$tool' 未找到，请按上面的命令安装依赖"
done
[[ -n $NAS_IP ]] || read -r -p 'NAS IPv4 地址：' NAS_IP || fail '未输入 NAS IPv4 地址'
ipv4 "$NAS_IP" || fail 'NAS IPv4 地址无效'
[[ $IDENTITY == /* ]] || IDENTITY=$PWD/$IDENTITY
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
STAGE=$(mktemp -d /tmp/minas-first-ssh.XXXXXXXX)
SMB_READY=0 SHARE_ATTEMPTED=0 SHARE_CONFIRMED=0 DIRECTORY_CREATED=0 CLEANED=0
SMB_DISARMED=0 CRON_ATTEMPTED=0 WORKER_SETTLED=0 DIRECTORY_UNCERTAIN=0
LOG_COLLECTED=0
WDV_PATH= SHARE_ID= NAS_USER= CN= REMOTE= JOB=
CERT_ARGS=()
SSH_OPTIONS=(-i "$IDENTITY" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new
    -o ConnectTimeout=5 -o ServerAliveInterval=5 -o ServerAliveCountMax=2)
api() {
    # Bodies and credentials stay off process arguments and error messages.
    curl --silent --show-error --fail --noproxy '*' --connect-timeout 5 --max-time 45 \
        "${CERT_ARGS[@]}" --resolve "$CN:443:$NAS_IP" -H 'Content-Type: application/json' \
        --data-binary @- "https://$CN/cgi-bin/luci/$1"
}
accounts() {
    printf '{}' | api netshare/get_samba_account > "$STAGE/accounts.json" || return 1
    jq -e '(.code == null or .code == 0) and (.data.account|type == "array") and
        all(.data.account[]; ((.id|tostring) == "1" or (.id|tostring) == "2") and
            (.username|type == "string") and (.share_dir|type == "array")) and
        ([.data.account[].id|tostring] | length == (unique|length))' "$STAGE/accounts.json" >/dev/null
}
dav() {
    curl --silent --show-error --noproxy '*' --connect-timeout 5 --max-time 20 \
        "${CERT_ARGS[@]}" --config "$STAGE/webdav.conf" --resolve "$CN:5000:$NAS_IP" "$@"
}
smb() {
    timeout 25 smbclient "//$NAS_IP/etc-${NAS_USER#u}" -A "$STAGE/smb.auth" \
        --option='client min protocol=SMB2' -c "$1" > "$STAGE/smb.log" 2>&1
}
remove_smb_file() {
    smb "del $1" || true
    # smbclient can exit 0 even when allinfo reports a missing file.
    smb "allinfo $1" || true
    # A transport/authentication failure is not proof that the file is absent.
    grep -Eq 'NT_STATUS_(OBJECT_NAME_NOT_FOUND|OBJECT_PATH_NOT_FOUND|NO_SUCH_FILE)' "$STAGE/smb.log"
}
read_receipt() {
    RECEIPT=
    smb "get $REMOTE.status $STAGE/receipt" || return 1
    [[ -f $STAGE/receipt ]] || return 1
    RECEIPT=$(cat "$STAGE/receipt")
    case $RECEIPT in
        "$TOKEN:ready"|"$TOKEN:failed") WORKER_SETTLED=1 ;;
        *) return 1 ;;
    esac
}
collect_execution_log() {
    [[ $LOG_COLLECTED != 1 && $CRON_ATTEMPTED == 1 ]] || return 0
    smb "get $REMOTE.log $STAGE/nas-execution.log" || return 0
    [[ -s $STAGE/nas-execution.log ]] || return 0
    local target=$SCRIPT_DIR/ssh-open-$TOKEN.log summary
    if (set -o noclobber; cat "$STAGE/nas-execution.log" > "$target") 2>/dev/null; then
        LOG_COLLECTED=1
        printf '电脑端执行日志副本：%s\n' "$target" >&2
        summary=$(grep -E ' (RESULT status=|STEP )' "$target" | tail -n 1) || summary=
        [[ -z $summary ]] || printf 'NAS 最后执行记录：%s\n' "$summary" >&2
    fi
}
cleanup_remote() {
    local ok=1 code quiesce_deadline
    if [[ $SMB_READY == 1 && $SMB_DISARMED != 1 ]]; then
        remove_smb_file "cron.d/$JOB" || ok=0
        remove_smb_file "cron.d/.$JOB" || ok=0
        remove_smb_file "$REMOTE.sh" || ok=0
        # Keep the authenticated recovery channel until disarming is confirmed.
        [[ $ok == 1 ]] || return 1
        if [[ $CRON_ATTEMPTED == 1 && $WORKER_SETTLED != 1 ]]; then
            # Removing Cron does not stop a worker that has already started.
            # Its timeout is 50 seconds, followed by a 5-second kill grace.
            echo '任务已撤销，等待可能仍在执行的任务退出后清理回执...'
            quiesce_deadline=$((SECONDS + 60))
            while ((SECONDS < quiesce_deadline)); do
                read_receipt && break
                sleep 2
            done
        fi
        collect_execution_log
        remove_smb_file "$REMOTE.log" || return 1
        remove_smb_file "$REMOTE.status" || return 1
        SMB_DISARMED=1
    fi
    if [[ $SHARE_ATTEMPTED == 1 ]]; then
        if accounts; then
            if jq -e --arg id "$SHARE_ID" --arg user "$NAS_USER" --rawfile password "$STAGE/smb.password" \
                '.data.account[] | select((.id|tostring)==$id and .username==$user and .password==$password)' "$STAGE/accounts.json" >/dev/null; then
                if jq -n --arg id "$SHARE_ID" '{id:$id}' | api netshare/del_samba_account > "$STAGE/delete.json"; then
                    code=$(jq -r '.code // -1' "$STAGE/delete.json")
                    if [[ $code != 0 ]]; then verbose "撤销临时 SMB 账户返回业务码 $code"; ok=0; fi
                else ok=0; fi
            elif [[ $SHARE_CONFIRMED != 1 ]]; then
                # An interrupted add can leave a UCI share without an account.
                ok=0
            fi
            accounts && jq -e --arg id "$SHARE_ID" \
                'all(.data.account[]; (.id|tostring)!=$id)' "$STAGE/accounts.json" >/dev/null || ok=0
        else ok=0; fi
    fi
    # Preserve the directory and recovery channel when account removal is uncertain.
    [[ $ok == 1 ]] || return 1
    if [[ $DIRECTORY_CREATED == 1 ]]; then
        code=$(dav -X DELETE -o /dev/null -w '%{http_code}' "https://$CN:5000$WDV_PATH") || code=000
        case $code in 200|204|404) DIRECTORY_CREATED=0 ;; *) ok=0 ;; esac
    fi
    [[ $DIRECTORY_UNCERTAIN == 0 ]] || ok=0
    [[ $ok == 1 ]] && CLEANED=1
    [[ $ok == 1 ]]
}
cleanup() {
    local result=$?
    trap - EXIT INT TERM HUP
    if [[ $CLEANED != 1 ]] && ! cleanup_remote; then
        printf '临时状态未确认清理完成。恢复材料保留在 %s（含临时凭据，请勿公开）。\n' "$STAGE" >&2
        printf '请按其中 recovery.txt 撤销一次性任务和本次共享；本次未报告完整成功。\n' >&2
        result=1
    else
        rm -rf -- "$STAGE"
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
TOKEN=$(openssl rand -hex 8)
[[ $TOKEN =~ ^[0-9a-f]{16}$ ]] || fail '无法生成事务编号'
REMOTE=.minas-ssh-$TOKEN JOB=minas-first-ssh-$TOKEN

if [[ -z $CERT_HOME ]] && command -v cmd.exe >/dev/null && command -v wslpath >/dev/null; then
    WIN_LOCAL=$(cmd.exe /d /c 'chcp 65001 >nul & echo %LOCALAPPDATA%' 2>/dev/null | tr -d '\r') || WIN_LOCAL=
    [[ -z $WIN_LOCAL ]] || CERT_HOME=$(wslpath "$WIN_LOCAL")/minasCert
fi
CERT_HOME=${CERT_HOME:-$HOME/.local/share/minasCert}
certs=()
for file in "$CERT_HOME/"*_cert.pem; do [[ ! -f $file ]] || certs+=("$file"); done
((${#certs[@]} == 1)) || fail '证书目录须只有当前账户的一对证书；请用 --cert-dir 指定'
CA_FILE=$CERT_HOME/ca_chain.pem CERT_FILE=${certs[0]}
KEY_FILE=${CERT_FILE%_cert.pem}_private_key.pem
[[ -f $CA_FILE && -f $KEY_FILE ]] || fail '缺少 ca_chain.pem 或配对的 _private_key.pem'
CERT_ARGS=(--cacert "$CA_FILE" --cert "$CERT_FILE" --key "$KEY_FILE")
verbose '已选择已有的小米客户端 TLS 证书，开始认证 NAS'
CN=$(timeout 12 openssl s_client -connect "$NAS_IP:443" -servername x </dev/null 2>/dev/null |
    openssl x509 -noout -subject -nameopt RFC2253 | sed -n 's/.*CN *= *\([^,]*\).*/\1/p') || fail '无法读取 NAS 证书'
[[ $CN =~ ^[A-Za-z0-9.-]+$ ]] || fail 'NAS 证书 CN 无效'
printf '{"selector":["webDAV","internal_pool"]}' | api filemgr/get_pool_info > "$STAGE/pool.json" || fail '证书认证或 WebDAV 凭据获取失败'
jq -e '.code == 0 and (.data.webDAV.username|type == "string") and
    (.data.webDAV.password|type == "string" and length>0)' "$STAGE/pool.json" >/dev/null || fail 'WebDAV 响应缺少成功码或有效凭据'
NAS_USER=$(jq -r '.data.webDAV.username' "$STAGE/pool.json")
[[ $NAS_USER =~ ^u[0-9]+$ ]] || fail 'WebDAV 用户格式无效'
DAV_DIR=$(jq -er '.data.internal_pool | if type=="array" and length==1 then .[0].data_dir else error("unsupported pools") end' "$STAGE/pool.json") || fail '无法确认单池目录布局，未修改共享'
[[ $DAV_DIR == /pool0/data ]] || fail '当前仅支持 /pool0/data 单池布局，未修改共享'
printf 'NAS 执行日志：当前用户 pool0/data/ssh-open-%s.log\n' "$TOKEN"
jq -r '"user = "+((.data.webDAV.username+":"+.data.webDAV.password)|@json)' "$STAGE/pool.json" > "$STAGE/webdav.conf"
rm -f "$STAGE/pool.json"
mkdir -p -- "$(dirname -- "$IDENTITY")"
if [[ ! -e $IDENTITY ]]; then
    [[ ! -L $IDENTITY ]] || fail '私钥路径是失效符号链接，请用 --identity 指定有效路径'
    [[ ! -e $IDENTITY.pub && ! -L $IDENTITY.pub ]] || fail '仅有公钥，缺少私钥；请用 --identity 指定现有私钥'
    printf '生成新的 SSH 登录密钥：%s\n' "$IDENTITY"
    ssh-keygen -q -t ed25519 -N '' -C '' -f "$IDENTITY" </dev/null
else
    [[ -f $IDENTITY ]] || fail 'SSH 私钥路径不是普通文件'
    printf '复用已有 SSH 登录私钥：%s\n' "$IDENTITY"
fi
ssh-keygen -y -f "$IDENTITY" > "$STAGE/public.raw" || fail '无法从私钥读取公钥'
awk '{print $1 " " $2}' "$STAGE/public.raw" > "$STAGE/public.key"
ssh-keygen -lf "$STAGE/public.key" >/dev/null || fail '公钥校验失败'
verbose "SSH 公钥指纹：$(ssh-keygen -lf "$STAGE/public.key")"
PUB_KEY=$(cat "$STAGE/public.key")
[[ $PUB_KEY =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521))\ [A-Za-z0-9+/=]+$ ]] || fail '不支持的公钥类型'
accounts || fail '无法确认已有 SMB 账户，未修改共享'
cp "$STAGE/accounts.json" "$STAGE/accounts.before.json"
if jq -e '.data.account[] | select((.id|tostring)=="1")' "$STAGE/accounts.json" >/dev/null; then SHARE_ID=2; else SHARE_ID=1; fi
jq -e --arg id "$SHARE_ID" --arg user "$NAS_USER" \
    'all(.data.account[]; (.id|tostring)!=$id and (.username|ascii_downcase)!=($user|ascii_downcase))' "$STAGE/accounts.json" >/dev/null ||
    fail '没有安全的空闲 SMB 槽位或同名账户已存在；请先在客户端自行调整，本脚本不会覆盖'
SECTION=${NAS_USER}_$SHARE_ID
verbose "已确认空闲 SMB 槽位 $SHARE_ID"
SMB_PASSWORD=$(openssl rand -hex 20)
printf 'username = %s\npassword = %s\n' "$NAS_USER" "$SMB_PASSWORD" > "$STAGE/smb.auth"
PREFIX=minasssh${TOKEN}000000
# Firmware's global force create mode=0760 is OR'ed after create mask.
# _smb_add_share emits this multiline value within this share. Real newlines
# are required: WebDAV rejects backslashes in directory names.
CREATE_MODE="'0644
force create mode=0644
map archive=no'"
printf -v WEIRD '%s\n"\nset sambashare.%s.user_force=root\nset sambashare.%s.create_mask=%s\nset sambashare.%s.path=etc\nset sambashare.%s.comment=' \
    "$PREFIX" "$SECTION" "$SECTION" "$CREATE_MODE" "$SECTION" "$SECTION"
ENCODED=$(printf '%s' "$WEIRD" | jq -sRr @uri)
WDV_PATH=/pool0/data/$ENCODED/
REAL_DIR=/home/$NAS_USER/pool0/data/$WEIRD
cat > "$STAGE/recovery.txt" <<EOF
NAS: $NAS_IP; 本次 SMB 槽位: $SHARE_ID; 账户: $NAS_USER
若不能自动清理，在已有 root 终端执行：
rm -f /etc/cron.d/$JOB /etc/cron.d/.$JOB /etc/$REMOTE.sh /etc/$REMOTE.status
撤销任务与脚本后至少等待 60 秒，再次删除 /etc/$REMOTE.status，避免仍在执行的任务重建回执。
反馈日志保存在 /home/$NAS_USER/pool0/data/ssh-open-$TOKEN.log；删除临时共享前也可下载 /etc/$REMOTE.log，再删除这个临时日志。
仅撤销本次创建的 $SECTION（不要删除其他共享或模糊删除 passwd/shadow）：
nas call smb_mgr.sh del_account one $NAS_USER $SHARE_ID
nas call smb_mgr.sh restart
在客户端删除本次以 $PREFIX 开头的临时目录；保留其他目录和原有 SSH 公钥。
本目录 smb.auth 含本次临时 SMB 密码。确认清理后删除本目录。
没有 root SSH 时，用 smbclient //$NAS_IP/etc-${NAS_USER#u} -A "$STAGE/smb.auth"
先删除 cron.d/$JOB、cron.d/.$JOB、$REMOTE.sh、$REMOTE.status，再从客户端移除本次 SMB 账户。
若添加中断导致没有 sambauser 记录，请在 root 终端核对本次 $SECTION 后仅删除该段：
uci -q delete sambashare.$SECTION; uci commit sambashare
EOF
write_bootstrap_sources() {
    cat > "$STAGE/first-ssh-nas.sh" <<'MINAS_FIRST_SSH_SOURCE'
# Embedded by build.py; variables above are validated and generated by the client.
set +x
set -eu
umask 077
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
status=/etc/$REMOTE.status
job=/etc/cron.d/$JOB
script=/etc/$REMOTE.sh
root_log=/etc/$REMOTE.log
log_active=0 user_log_ready=0 current_step=bootstrap
user_log=
note() {
    printf '[%s] %s\n' "$(/usr/bin/date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || printf unknown-time)" "$*"
}
publish_log() {
    [ "$user_log_ready" = 1 ] || return 0
    # The root process never opens a user-controlled destination. The account
    # writes a same-directory temporary file and atomically publishes the log.
    if ! /usr/bin/timeout -k 1 3 /usr/sbin/runuser -u "$NAS_USER" -- /bin/sh -c '
        set -eu
        [ "$(/usr/bin/id -u)" -gt 0 ]
        umask 077
        next=$(mktemp "${1%/*}/.ssh-open-log.XXXXXXXX")
        trap '\''rm -f "$next"'\'' EXIT
        trap '\''exit 143'\'' TERM HUP
        trap '\''exit 130'\'' INT
        cat > "$next"
        chmod 0600 "$next"
        mv -f "$next" "$1"
    ' log-writer "$user_log" < "$root_log"; then
        user_log_ready=0
        note 'WARN 无法保存用户目录日志；电脑端将尝试保留临时执行日志'
    fi
}
step() {
    current_step=$1; shift
    note "STEP $current_step | $*"
    publish_log
}
receipt() {
    next=$(mktemp "/etc/$REMOTE.status.XXXXXXXX") || return 1
    if printf '%s:%s\n' "$TOKEN" "$value" > "$next" && chmod 0600 "$next" && mv -f "$next" "$status"; then return 0; fi
    rm -f "$next"; return 1
}
finish() {
    result=$?
    trap - EXIT INT TERM HUP
    set +e
    value=failed
    [ "$result" != 0 ] || value=ready
    rm -f "$script"
    note "RESULT status=$value step=$current_step exit=$result"
    if [ "$log_active" = 1 ]; then publish_log; fi
    # Publish the receipt only after logging and all other worker writes.
    if ! receipt; then
        note 'RESULT status=failed step=receipt exit=1'
        if [ "$log_active" = 1 ]; then publish_log; fi
        [ "$result" != 0 ] || result=1
    fi
    exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
safe() {
    node=$1
    while :; do
        [ ! -L "$node" ] || { echo "路径包含符号链接：$node" >&2; exit 1; }
        if [ -e "$node" ]; then
            [ "$(stat -c %u "$node")" = 0 ] || { echo "路径不是 root 所有：$node" >&2; exit 1; }
            mode=$(stat -c %a "$node")
            [ "$((0$mode & 0022))" = 0 ] || { echo "路径可被其他用户写入：$node" >&2; exit 1; }
        fi
        [ "$node" != / ] || break
        node=${node%/*}; [ -n "$node" ] || node=/
    done
}
put() {
    target=$1 permissions=$2
    safe "$target"
    next=$(mktemp "${target%/*}/.minas-first-ssh.XXXXXXXX")
    cat > "$next"
    chmod "$permissions" "$next"
    mv -f "$next" "$target"
}
prepare_shell_dropin_dir() {
    # Repair only this managed root-owned directory; keep strict checks on
    # every ancestor and on the config file itself.
    dropin_dir=/etc/systemd/system/dropbear@.service.d
    safe "${dropin_dir%/*}"
    [ ! -L "$dropin_dir" ] || { echo "路径包含符号链接：$dropin_dir" >&2; exit 1; }
    if [ -e "$dropin_dir" ]; then
        [ -d "$dropin_dir" ] || { echo "SSH 配置目录不是目录：$dropin_dir" >&2; exit 1; }
        [ "$(stat -c %u "$dropin_dir")" = 0 ] || { echo "路径不是 root 所有：$dropin_dir" >&2; exit 1; }
        dropin_mode=$(stat -c %a "$dropin_dir")
        if [ "$((0$dropin_mode & 0022))" != 0 ]; then
            chmod go-w "$dropin_dir"
            note "已收紧 SSH 配置目录写权限：$dropin_dir（原权限 $dropin_mode）"
        fi
    else
        mkdir -p -m 0755 "$dropin_dir"
    fi
    safe "$dropin_dir"
}
[ "$(id -u)" = 0 ] || exit 1
rm -f "$job"
safe "$root_log"
next=$(mktemp "/etc/$REMOTE.log.XXXXXXXX")
chmod 0600 "$next"; mv -f "$next" "$root_log"
exec >> "$root_log" 2>&1
log_active=1
note '小米 NAS SSH 开启 2.2.6 执行日志（不记录密码、密钥内容或认证参数）'
case ${NAS_USER:-} in
    u*) digits=${NAS_USER#u}; case $digits in ''|*[!0-9]*) ;; *)
        user_log=/home/$NAS_USER/pool0/data/ssh-open-$TOKEN.log
        user_log_ready=1
        ;; esac ;;
esac
step acquire-lock '取得本次 SSH 开启操作锁'
exec 8>/run/minas-first-ssh.lock
flock -n 8 || exit 1
step check-tools '检查 NAS 所需命令'
for tool in flock stat awk mktemp cmp readlink timeout cut grep systemctl /usr/sbin/dropbearkey; do
    command -v "$tool" >/dev/null || { note "ERROR 缺少命令：$tool"; exit 127; }
done
step root-account '检查 root 账户和主目录'
safe /etc/passwd
record=$(awk -F: '$1=="root" {n++; row=$0} END {if(n==1) print row; else exit 1}' /etc/passwd)
[ "$(printf '%s\n' "$record" | awk -F: '{print NF ":" $3}')" = 7:0 ]
home=$(printf '%s\n' "$record" | cut -d: -f6)
case $home in /root|/home/rootx) ;; *) echo '不支持的 root 主目录' >&2; exit 1 ;; esac
# Same service-private bind as the current NAS; vendor mi-shell stays intact.
# BEGIN MINAS BASH PROBE
bash_path_safe() (
    node=$1
    while :; do
        [ ! -L "$node" ] || exit 1
        info=$(/usr/bin/stat -c '%u:%a' "$node") || exit 1
        owner=${info%%:*}; mode=${info#*:}
        [ "$owner" = 0 ] && [ "$((0$mode & 0022))" = 0 ] || exit 1
        [ "$node" != / ] || break
        node=${node%/*}; [ -n "$node" ] || node=/
    done
)
find_bash() (
    limited=$1
    # Fixed absolute paths only; never select a command from the caller's PATH.
    for candidate in /usr/bin/bash.bash /bin/bash.bash /usr/local/bin/bash /usr/bin/bash /bin/bash /opt/bin/bash; do
        normal=$(/usr/bin/readlink -f "$candidate" 2>/dev/null) || continue
        [ -f "$normal" ] && [ -x "$normal" ] || continue
        [ "$normal" != "$limited" ] || continue
        if [ "$normal" -ef "$limited" ]; then
            # Existing SSH sessions inherit the service-private Bash bind.
            /usr/bin/mountpoint -q "$limited" || continue
        fi
        case $normal in *[!a-zA-Z0-9_./-]*) continue ;; esac
        # Resolve aliases, but require the real executable and all its parents
        # to be root-owned and not writable by other users before executing it.
        bash_path_safe "$normal" 2>/dev/null || continue
        # Disable startup files and inherited shell settings. An executable
        # filename alone does not prove that it is a working, unrestricted Bash.
        reply=$(/usr/bin/timeout -k 1 2 /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C \
            "$normal" --noprofile --norc -p -c \
            '[[ -n ${BASH_VERSION-} && $- != *r* ]] && builtin printf "%s\n" __MINAS_BASH_OK__' \
            </dev/null 2>/dev/null) || continue
        [ "$reply" = __MINAS_BASH_OK__ ] || continue
        printf '%s\n' "$normal"
        exit 0
    done
    printf '%s\n' '未找到通过执行验证的 Bash（已检查固定绝对路径）' >&2
    exit 1
)
# END MINAS BASH PROBE
conf=/etc/systemd/system/dropbear@.service.d/90-normal-shell.conf
if [ -e /usr/sbin/mi-shell ]; then
    step bash-probe '按绝对路径查找并执行验证 Bash'
    limited=$(/usr/bin/readlink -f /usr/sbin/mi-shell)
    normal=$(find_bash "$limited") || exit 1
    note "Bash 已验证：$normal"
    step shell-bind '设置 Dropbear 专用 Bash 映射'
    case $normal:$limited in *[!a-zA-Z0-9_./:-]*) exit 1 ;; esac
    safe "$normal"; safe "$limited"
    prepare_shell_dropin_dir
    safe "$conf"
    expected=$(mktemp "${conf%/*}/.minas-first-ssh.XXXXXXXX")
    printf '[Service]\nBindReadOnlyPaths=%s:%s\n' "$normal" "$limited" > "$expected"
    if [ -e "$conf" ] && ! cmp -s "$conf" "$expected"; then
        rm -f "$expected"; echo '已有不同的 shell 配置，未覆盖' >&2; exit 1
    fi
    put "$conf" 0644 < "$expected"; rm -f "$expected"
fi
step login-keys '保留并合并 SSH 登录公钥'
safe /home/rootx; safe /home/rootx/.ssh; safe /home/rootx/.ssh/authorized_keys
mkdir -p /home/rootx/.ssh
chmod 0700 /home/rootx /home/rootx/.ssh
auth=/home/rootx/.ssh/authorized_keys
if [ "$home" = /root ] && [ -f /root/.ssh/authorized_keys ]; then safe /root/.ssh/authorized_keys; fi
merged=$(mktemp /home/rootx/.ssh/.minas-first-ssh.XXXXXXXX)
{
    if [ -f "$auth" ]; then cat "$auth"; printf '\n'; fi
    if [ "$home" = /root ] && [ -f /root/.ssh/authorized_keys ]; then
        cat /root/.ssh/authorized_keys; printf '\n'
    fi
    printf '%s\n' "$PUB_KEY"
} > "$merged"
unique=$(mktemp /home/rootx/.ssh/.minas-first-ssh.XXXXXXXX)
awk 'NF && !seen[$0]++' "$merged" > "$unique"
put "$auth" 0600 < "$unique"
rm -f "$merged" "$unique"
step account-backup '保存首次修改前的账户配置'
safe /data/minas
mkdir -p /data/minas
chmod 0755 /data/minas
safe /data/minas/backups
mkdir -p /data/minas/backups
chmod 0700 /data/minas/backups
safe /data/minas/backups/ssh-bootstrap
mkdir -p /data/minas/backups/ssh-bootstrap
chmod 0700 /data/minas/backups/ssh-bootstrap
if [ ! -e /data/minas/backups/ssh-bootstrap/passwd.before ]; then
    put /data/minas/backups/ssh-bootstrap/passwd.before 0600 < /etc/passwd
fi
step account-write '更新 root 主目录和登录 Shell'
next_passwd=$(mktemp /etc/.minas-first-ssh.XXXXXXXX)
awk -F: -v OFS=: '$1=="root" {$6="/home/rootx"; if($7 ~ /\/mi-shell$/) $7="/bin/sh"} {print}' /etc/passwd > "$next_passwd"
put /etc/passwd "$(stat -c %a /etc/passwd)" < "$next_passwd"
rm -f "$next_passwd"
step profile '补充 root 的 Docker 命令路径'
safe /home/rootx/.profile
if ! grep -Fqx 'export PATH="$PATH:/data/docker"' /home/rootx/.profile 2>/dev/null; then
    { if [ -f /home/rootx/.profile ]; then cat /home/rootx/.profile; printf '\n'; fi
      printf '%s\n' 'export PATH="$PATH:/data/docker"'; } | put /home/rootx/.profile 0600
fi
# Keep existing Dropbear arguments and host identity; do not enable persistence.
step host-key '检查 SSH 主机密钥，缺失时才生成'
safe /etc/dropbear; mkdir -p /etc/dropbear
host=/etc/dropbear/dropbear_rsa_host_key
safe "$host"
if [ ! -e "$host" ]; then
    /usr/sbin/dropbearkey -t rsa -s 2048 -f "$host" >/dev/null 2>&1
    chmod 0600 "$host"
fi
/usr/sbin/dropbearkey -y -f "$host" >/dev/null 2>&1 || { note 'ERROR SSH 主机密钥验证失败，未覆盖现有文件'; exit 1; }
step daemon-reload '加载 systemd 配置'
timeout 15 systemctl daemon-reload
step socket-reset '清理 SSH socket 的失败状态'
timeout 10 systemctl reset-failed dropbear.socket || true
step socket-start '启动 SSH socket'
timeout 10 systemctl --no-block start dropbear.socket
step complete 'SSH 开启步骤已完成，等待电脑端验证真实登录'
MINAS_FIRST_SSH_SOURCE
}

write_bootstrap_sources
{
    printf '#!/bin/sh\nTOKEN=%s\nJOB=%s\nREMOTE=%s\n' "$TOKEN" "$JOB" "$REMOTE"
    printf 'NAS_USER=%s\n' "$NAS_USER"
    printf "PUB_KEY='%s'\n" "$PUB_KEY"
    cat "$STAGE/first-ssh-nas.sh"
} > "$STAGE/payload.sh"
printf 'SHELL=/bin/sh\nPATH=/usr/sbin:/usr/bin:/sbin:/bin\n* * * * * root /usr/bin/timeout -k 5 50 /bin/sh /etc/%s.sh\n' "$REMOTE" > "$STAGE/cron"
echo '创建本次临时共享，保留已有 SMB 账户...'
DIRECTORY_UNCERTAIN=1
CODE=$(dav -X MKCOL -o /dev/null -w '%{http_code}' "https://$CN:5000$WDV_PATH") || fail '创建临时目录请求失败；结果不明，保留恢复材料供核对'
DIRECTORY_UNCERTAIN=0
[[ $CODE == 201 ]] || fail "创建临时目录失败，HTTP $CODE（未复用已有目录）"
DIRECTORY_CREATED=1
accounts || fail '无法再次核对 SMB 槽位'
[[ $(jq -cS '.data.account' "$STAGE/accounts.before.json") == "$(jq -cS '.data.account' "$STAGE/accounts.json")" ]] || fail 'SMB 配置已变化，请稍后重试'
SHARE_ATTEMPTED=1
printf '%s' "$SMB_PASSWORD" > "$STAGE/smb.password"
jq -n --arg user "$NAS_USER" --rawfile password "$STAGE/smb.password" --arg dir "$REAL_DIR" \
    '{username:$user,password:$password,share_dir:[$dir]}' | api netshare/add_samba_account > "$STAGE/add.json" || fail '添加共享请求失败，开始核对清理'
CODE=$(jq -r '.code // -1' "$STAGE/add.json")
case $CODE in 0|2502) ;; *) fail "添加共享失败，业务码 $CODE" ;; esac
accounts || fail '无法核对新建 SMB 账户'
jq -e --arg id "$SHARE_ID" --arg user "$NAS_USER" --arg dir "$REAL_DIR" --rawfile password "$STAGE/smb.password" \
    'any(.data.account[]; (.id|tostring)==$id and .username==$user and .password==$password and .share_dir==[$dir])' "$STAGE/accounts.json" >/dev/null || fail '新建共享与本次事务不匹配'
SHARE_CONFIRMED=1
jq -n --arg id "$SHARE_ID" --arg user "$NAS_USER" --rawfile password "$STAGE/smb.password" \
    '{id:$id,username:$user,password:$password,share_dir:["/etc"]}' | api netshare/set_samba_account > "$STAGE/set.json" || fail '修复临时共享路径失败'
jq -e '.code == 0' "$STAGE/set.json" >/dev/null || fail '修复共享未返回明确成功码'
unset SMB_PASSWORD
for ((i=0; i<5; i++)); do
    if smb 'ls'; then SMB_READY=1; break; fi
    sleep 2
done
[[ $SMB_READY == 1 ]] || fail 'SMB /etc 共享不可用'
smb "put $STAGE/payload.sh $REMOTE.sh; get $REMOTE.sh $STAGE/payload.verify" &&
    cmp -s "$STAGE/payload.sh" "$STAGE/payload.verify" || fail '上传脚本或回读校验失败'
smb "put $STAGE/cron cron.d/.$JOB; get cron.d/.$JOB $STAGE/cron.verify" &&
    cmp -s "$STAGE/cron" "$STAGE/cron.verify" || fail '上传一次性任务或回读校验失败'
CRON_ATTEMPTED=1
smb "rename cron.d/.$JOB cron.d/$JOB" || fail '一次性任务激活失败'
verbose '载荷与 Cron 回读一致，一次性任务已激活'
echo '等待一次性任务执行并验证本次 root SSH...'
READY=0
DEADLINE=$((SECONDS + 130))
while ((SECONDS < DEADLINE)); do
    if read_receipt && [[ $RECEIPT == "$TOKEN:failed" ]]; then
        collect_execution_log
        fail 'NAS 一次性任务返回失败回执；未安装开机修复'
    fi
    RESULT=$(timeout 7 ssh "${SSH_OPTIONS[@]}" "root@$NAS_IP" \
        "test \"\$(cat /etc/$REMOTE.status 2>/dev/null)\" = '$TOKEN:ready' && printf '__MINAS_${TOKEN}__\\n' && id -u" 2>"$STAGE/ssh.log") || RESULT=
    if [[ ${RESULT//$'\r'/} == "__MINAS_${TOKEN}__"$'\n0' ]]; then
        READY=1 WORKER_SETTLED=1
        verbose '本次任务回执、SSH 命令标记与 UID 0 均已验证'
        break
    fi
    if grep -Eq 'REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed' "$STAGE/ssh.log"; then
        fail 'SSH 主机密钥校验失败，请先核实设备身份及 known_hosts；脚本不会自动删除已有主机记录'
    fi
    sleep 2
done
if [[ $READY != 1 ]]; then
    if [[ $WORKER_SETTLED == 1 ]]; then
        fail 'NAS 任务已完成，但 SSH 未通过本次执行回执和 UID 0 验证；未安装开机修复'
    fi
    fail '未收到 NAS 一次性任务的完成回执，请检查 Cron 任务是否执行及文件权限；未安装开机修复'
fi
cleanup_remote || fail 'SSH 已验证，但临时任务/共享未确认清理完成'
echo 'SSH 已验证成功；本次临时共享和任务已清理。请保留登录私钥。'
printf 'SSH 登录命令：ssh -i %q root@%s\n' "$IDENTITY" "$NAS_IP"
ANSWER=
read -r -p '是否安装开机 SSH 修复？[y/N] ' ANSWER || ANSWER=
case $ANSWER in
    y|Y|yes|YES|是)
        INSTALLER=$SCRIPT_DIR/install-ssh-autostart-fix.sh
        if [[ -f $INSTALLER ]] && bash "$INSTALLER" "$NAS_IP" --identity "$IDENTITY"; then
            echo '开机 SSH 修复已安装并通过安装器验证。'
        else
            echo 'SSH 已可用，但开机 SSH 修复调用失败，不能保证重启后仍可用。' >&2
            printf '请将仓库 scripts/install-ssh-autostart-fix.sh 放到开启脚本同一目录：%s\n' "$SCRIPT_DIR" >&2
            printf '保留文件名 install-ssh-autostart-fix.sh，用 LF 换行；然后在 WSL / Linux 重试：\nbash %q %q --identity %q\n' "$INSTALLER" "$NAS_IP" "$IDENTITY" >&2
            exit 1
        fi ;;
    *) echo '已跳过开机 SSH 修复；本次 SSH 可用性已验证，重启后的恢复未安装。' ;;
esac
