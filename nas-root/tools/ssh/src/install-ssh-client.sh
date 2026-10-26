#!/usr/bin/env bash
# Xiaomi NAS SSH @SSH_RECOVERY_VERSION@. Protocol adapted from Scruel's minas-enable-ssh_wsl.sh:
# https://github.com/scruel/xiaomi-storage-minas-tool/blob/main/minas-enable-ssh_wsl.sh
# Requires existing administrator authority; never modifies RPMB or root passwords.
set -Eeuo pipefail
umask 077
fail() { printf '失败：%s\n' "$*" >&2; exit 1; }
need() { local tool; for tool in "$@"; do command -v "$tool" >/dev/null || fail "缺少命令：$tool"; done; }
usage() {
    if [[ ${BOOT_ONLY:-0} == 1 ]]; then
        cat <<'INSTALL_HELP'
小米 NAS 纯 Shell 开机修复安装器 @SSH_RECOVERY_VERSION@
用法：bash install-ssh-autostart-fix.sh [NAS IPv4] [--identity 私钥路径]
需要已能通过公钥执行真实 root SSH 命令；不新增登录公钥。
安装与 SSH 恢复插件相同的开机 worker，不重启 NAS。
  -h, --help              显示帮助
INSTALL_HELP
        return
    fi
    cat <<'HELP'
小米 NAS SSH 开启与开机修复 @SSH_RECOVERY_VERSION@（纯 Shell）
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
默认安装开机修复；常规恢复使用 Shell，直接使用 /data/minas/ssh/。
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
    # Replaced by build.py in the standalone installation entry point.
    cp "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/restore-ssh.sh" "$STAGE/restore-ssh.sh"
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
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
