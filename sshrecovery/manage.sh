#!/usr/bin/env bash
# Single-file install/uninstall entry point. NAS backends are emitted into a temporary bundle.
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_NAME='sshrecovery'
PLUGIN_LABEL='SSH 恢复'
PLUGIN_VERSION='1.1.34'
UNINSTALL_NOTE='仅删除所选用户的插件及恢复材料；不关闭 SSH，不移除已安装的登录公钥。'

# Shared frontend; keep this section consistent across the four manage.sh files.
set -Eeuo pipefail

installer_error() { printf '[错误] %s\n' "$*" >&2; exit 2; }
installer_info() { printf '[信息] %s\n' "$*"; }
installer_usage() {
    cat <<EOF
$PLUGIN_LABEL — $ACTION_LABEL

用法：
  bash $ENTRY [设备IP] [用户1,用户2]
  bash $ENTRY [--ip 192.168.31.100] [--users u123456789,u987654321]
  bash $ENTRY [--ip 192.168.31.100] --all-users

选项：
  --ip IP        远程 NAS 的 IPv4 地址；未填写时交互输入
  --users LIST   指定一个或多个用户，以逗号分隔
  --all-users    选择扫描到的全部符合条件的用户
  --list-users   仅显示用户及安装状态
  --dry-run      校验环境和用户，显示计划，不执行安装卸载
  --yes, -y      跳过卸载确认；非交互卸载必须明确提供
  --help, -h     显示此说明

NAS 本机自动直接执行，必须以 root 运行；WSL/Linux 自动使用 root SSH。
不指定用户时列出用户，支持序号多选（如 1,3）或输入 all。
仅扫描到一个用户时自动选择；非交互多用户场景必须指定用户。
安装会保留已有配置；卸载范围与保留数据会在执行前显示。
EOF
}
installer_ipv4() {
    local ip=$1 part
    [[ $ip =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    local -a parts
    IFS=. read -r -a parts <<< "$ip"
    for part in "${parts[@]}"; do ((10#$part <= 255)) || return 1; done
}
installer_valid_user() { [[ $1 =~ ^u[0-9]+$ ]]; }
installer_require() {
    local command_name
    for command_name in "$@"; do
        command -v "$command_name" >/dev/null 2>&1 || installer_error "缺少命令：$command_name"
    done
}
installer_target_script() {
    if [[ $DIRECT == true ]]; then
        installer_emit_probe | /bin/sh -s -- "$@"
    else
        installer_emit_probe | ssh "${SSH_OPTIONS[@]}" "root@$NAS_IP" /bin/sh -s -- "$@"
    fi
}
installer_cleanup() {
    local rc=$?
    trap - EXIT
    if [[ -n ${REMOTE_DIR:-} && $REMOTE_DIR =~ ^/tmp/xiaomi-plugin\.[a-z]+\.[a-zA-Z0-9]+$ ]]; then
        ssh "${SSH_OPTIONS[@]}" -o BatchMode=yes "root@$NAS_IP" "rm -rf '$REMOTE_DIR'" >/dev/null 2>&1 ||
            printf '[提示] 远端暂存目录未清理，可手动删除：%s\n' "$REMOTE_DIR" >&2
    fi
    if [[ -n ${WORK_DIR:-} && $WORK_DIR =~ ^/tmp/xiaomi-plugin\.[a-z]+\.[a-zA-Z0-9]+$ ]]; then
        rm -rf -- "$WORK_DIR"
    fi
    exit "$rc"
}
installer_select_users() {
    local raw=$1 row user state token found index
    USERS=(); STATES=(); SELECTED=()
    while IFS=$'\t' read -r user state; do
        [[ -n $user ]] || continue
        installer_valid_user "$user" || installer_error "设备返回了无效用户；请检查 SSH 输出。"
        [[ $state == installed || $state == available ]] || installer_error "设备返回了无效安装状态。"
        USERS+=("$user"); STATES+=("$state")
    done <<< "$raw"
    printf '\n可%s的用户：\n' "$ACTION_LABEL"
    for ((index=0; index<${#USERS[@]}; index++)); do
        state=未安装; [[ ${STATES[index]} != installed ]] || state=已安装
        printf '  %d) %s  [%s]\n' "$((index+1))" "${USERS[index]}" "$state"
    done
    if (("${#USERS[@]}" == 0)); then
        [[ $ACTION != uninstall ]] || { installer_info "没有已安装$PLUGIN_LABEL的用户，无需卸载。"; exit 0; }
        installer_error "没有可安装用户，请先在小米客户端创建用户并初始化存储池。"
    fi
    [[ $LIST_ONLY != true ]] || return 0
    if [[ $ALL_USERS == true ]]; then
        SELECTED=("${USERS[@]}")
        return
    fi
    if [[ -z $USER_SPEC ]]; then
        if (("${#USERS[@]}" == 1)); then
            USER_SPEC="${USERS[0]}"
            installer_info "自动选择唯一用户：$USER_SPEC"
        else
            [[ -t 0 ]] || installer_error "存在多个用户，请使用 --users 或 --all-users。"
            read -r -p '请选择用户序号（可用逗号多选，all 表示全部，q 退出）：' USER_SPEC || installer_error "输入已结束。"
            [[ $USER_SPEC != q ]] || exit 0
        fi
    fi
    if [[ $USER_SPEC == all ]]; then SELECTED=("${USERS[@]}"); return; fi
    [[ -n $USER_SPEC && $USER_SPEC != ,* && $USER_SPEC != *, && $USER_SPEC != *,,* ]] || installer_error "用户列表不能为空或包含空项。"
    local -a tokens
    IFS=', ' read -r -a tokens <<< "$USER_SPEC"
    for token in "${tokens[@]}"; do
        if [[ $token =~ ^[0-9]+$ && ${#token} -le 6 ]]; then
            index=$((10#$token))
            ((index >= 1 && index <= ${#USERS[@]})) || installer_error "用户序号超出范围：$token"
            user=${USERS[index-1]}
        else
            installer_valid_user "$token" || installer_error "无效用户：$token"
            user=$token
        fi
        found=false
        for row in "${USERS[@]}"; do [[ $row != "$user" ]] || found=true; done
        [[ $found == true ]] || installer_error "用户 $user 不在本次可$ACTION_LABEL列表中。"
        found=false
        for row in "${SELECTED[@]}"; do [[ $row != "$user" ]] || found=true; done
        [[ $found == true ]] || SELECTED+=("$user")
    done
    (("${#SELECTED[@]}" > 0)) || installer_error "未选择用户。"
}
installer_stage_package() {
    local target=$1 package_item
    [[ -f $SCRIPT_DIR/INFO && -d $SCRIPT_DIR/src/ui && -f $SCRIPT_DIR/scripts/control && -d $SCRIPT_DIR/installer ]] || installer_error "安装包目录结构不完整。"
    mkdir "$target/$PLUGIN_NAME"
    for package_item in INFO src scripts etc installer; do
        [[ ! -e $SCRIPT_DIR/$package_item ]] || cp -R "$SCRIPT_DIR/$package_item" "$target/$PLUGIN_NAME/"
    done
}

installer_main() {
    ACTION=$1; shift
    case $ACTION in install) ACTION_LABEL=安装; ENTRY="manage.sh install";; uninstall) ACTION_LABEL=卸载; ENTRY="manage.sh uninstall";; *) installer_error "无效操作。";; esac
    NAS_IP=; USER_SPEC=; ALL_USERS=false; LIST_ONLY=false; DRY_RUN=false; ASSUME_YES=false
    DIRECT=false; WORK_DIR=; REMOTE_DIR=
    local arg raw answer user rc failures=0
    while (($#)); do
        arg=$1; shift
        case $arg in
            -h|--help) installer_usage; return;;
            --ip|--users)
                (($#)) && [[ -n $1 && $1 != --* ]] || installer_error "$arg 缺少参数。"
                if [[ $arg == --ip ]]; then
                    [[ -z $NAS_IP ]] || installer_error "设备 IP 重复指定。"; NAS_IP=$1
                else
                    [[ -z $USER_SPEC ]] || installer_error "用户重复指定。"; USER_SPEC=$1
                fi
                shift;;
            --all-users) ALL_USERS=true;;
            --list-users) LIST_ONLY=true;;
            --dry-run) DRY_RUN=true;;
            --yes|-y) ASSUME_YES=true;;
            --*) installer_error "未知选项：$arg";;
            *)
                if [[ $arg == *.* && -z $NAS_IP ]]; then NAS_IP=$arg
                elif [[ -z $USER_SPEC ]]; then USER_SPEC=$arg
                else installer_error "多余参数：$arg"; fi;;
        esac
    done
    [[ $ALL_USERS != true || -z $USER_SPEC ]] || installer_error "--all-users 不能与用户列表同时使用。"
    [[ -z $NAS_IP ]] || installer_ipv4 "$NAS_IP" || installer_error "无效 IPv4 地址：$NAS_IP"
    printf '\n%s — %s\n' "$PLUGIN_LABEL" "$ACTION_LABEL"
    printf '[1/5] 识别运行环境\n'
    if [[ -f /etc/config/plugin ]] && command -v plugincenter >/dev/null 2>&1; then
        [[ $(id -u) == 0 ]] || installer_error "已识别为 NAS 本机，请使用 root 运行。"
        DIRECT=true; installer_info "小米智能存储本机，直接执行。"
        [[ -z $NAS_IP ]] || installer_error "本机模式无需设备 IP；请移除 IP，避免误操作目标。"
    else
        if [[ -n ${WSL_DISTRO_NAME:-} ]] || grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
            installer_info "WSL，通过 root SSH 连接 NAS。"
        else
            installer_info "Linux/兼容终端，通过 root SSH 连接 NAS。"
        fi
        installer_require ssh
        if [[ -z $NAS_IP ]]; then
            [[ -t 0 ]] || installer_error "非交互环境请使用 --ip 指定设备 IP。"
            read -r -p '请输入小米智能存储 IPv4 地址：' NAS_IP || installer_error "输入已结束。"
        fi
        installer_ipv4 "$NAS_IP" || installer_error "无效 IPv4 地址：$NAS_IP"
    fi
    SSH_OPTIONS=(-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)
    [[ -t 0 ]] || SSH_OPTIONS+=(-o BatchMode=yes)
    printf '[2/5] 扫描用户与安装状态\n'
    if ! raw=$(installer_target_script --scan "$PLUGIN_NAME" "$ACTION"); then
        installer_error "设备检查或用户扫描失败；请检查 SSH 连接、root 权限及上方错误。"
    fi
    installer_select_users "$raw"
    [[ $LIST_ONLY != true ]] || return 0
    printf '\n[3/5] 核对执行计划\n'
    printf '  插件：%s\n  操作：%s\n  目标：%s\n' "$PLUGIN_LABEL" "$ACTION_LABEL" "${NAS_IP:-NAS 本机}"
    printf '  用户：%s\n' "${SELECTED[*]}"
    if [[ $ACTION == uninstall ]]; then
        printf '  范围：仅删除所选用户的本插件；配置及记录先备份到 /data/minas/backups/plugins/sshrecovery/。\n'
        printf '  说明：%s\n' "$UNINSTALL_NOTE"
    else
        printf '  说明：保留已有导航配置；多用户独立注册，无后台常驻进程、无额外端口。\n'
    fi
    installer_target_script --check "$PLUGIN_NAME" "$ACTION" "${SELECTED[@]}" ||
        installer_error "预检查失败，尚未执行$ACTION_LABEL。"
    if [[ $DRY_RUN == true ]]; then installer_info "预检查通过。仅显示计划，未执行$ACTION_LABEL。"; return; fi
    if [[ $ACTION == uninstall && $ASSUME_YES != true ]]; then
        [[ -t 0 ]] || installer_error "非交互卸载请检查计划后添加 --yes。"
        read -r -p '确认卸载以上用户的插件？输入 yes 继续：' answer || installer_error "输入已结束。"
        [[ $answer == yes ]] || { installer_info "已取消卸载。"; return; }
    fi
    installer_require mktemp tar cp
    [[ $DIRECT == true ]] || installer_require scp
    WORK_DIR=$(mktemp -d "/tmp/xiaomi-plugin.$PLUGIN_NAME.XXXXXX")
    trap installer_cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' HUP TERM
    mkdir "$WORK_DIR/bundle"
    if [[ $ACTION == install ]]; then installer_emit_install > "$WORK_DIR/bundle/remote-install.sh"
    else installer_emit_uninstall > "$WORK_DIR/bundle/remote-uninstall.sh"; fi
    if [[ $ACTION == install ]]; then
        installer_stage_package "$WORK_DIR/bundle"
        if declare -F installer_prepare_package >/dev/null; then installer_prepare_package "$WORK_DIR/bundle"; fi
    fi
    printf '\n[4/5] 执行%s\n' "$ACTION_LABEL"
    if [[ $DIRECT != true ]]; then
        REMOTE_DIR=$(ssh "${SSH_OPTIONS[@]}" "root@$NAS_IP" "mktemp -d /tmp/xiaomi-plugin.$PLUGIN_NAME.XXXXXX") ||
            installer_error "无法创建 NAS 暂存目录。"
        [[ $REMOTE_DIR =~ ^/tmp/xiaomi-plugin\.[a-z]+\.[a-zA-Z0-9]+$ ]] || installer_error "NAS 返回了异常暂存路径。"
        tar -czf "$WORK_DIR/bundle.tgz" -C "$WORK_DIR" bundle
        scp "${SSH_OPTIONS[@]}" "$WORK_DIR/bundle.tgz" "root@$NAS_IP:$REMOTE_DIR/bundle.tgz"
        ssh "${SSH_OPTIONS[@]}" "root@$NAS_IP" "tar -xzf '$REMOTE_DIR/bundle.tgz' -C '$REMOTE_DIR'"
    fi
    local -a successes=() failed=()
    for user in "${SELECTED[@]}"; do
        printf '\n[用户 %s] 正在%s%s……\n' "$user" "$ACTION_LABEL" "$PLUGIN_LABEL"
        # Each backend runs in its own sh process: an error cannot be hidden by this if.
        if [[ $DIRECT == true ]]; then
            if /bin/sh "$WORK_DIR/bundle/remote-$ACTION.sh" "$user"; then rc=0; else rc=$?; fi
        else
            if ssh "${SSH_OPTIONS[@]}" "root@$NAS_IP" "/bin/sh '$REMOTE_DIR/bundle/remote-$ACTION.sh' '$user'"; then rc=0; else rc=$?; fi
        fi
        if ((rc == 0)); then successes+=("$user")
        else failed+=("$user"); failures=$((failures+1)); printf '[错误] %s %s失败（退出码 %s），请查看上方日志。\n' "$user" "$ACTION_LABEL" "$rc" >&2; fi
    done
    printf '\n[5/5] 执行结果\n'
    printf '  成功（%s）：%s\n' "${#successes[@]}" "${successes[*]:-无}"
    printf '  失败（%s）：%s\n' "${#failed[@]}" "${failed[*]:-无}"
    if ((failures > 0)); then
        printf '[提示] 已成功用户不回滚；请解决错误后只重试失败用户。\n' >&2
        return 1
    fi
    installer_info "$ACTION_LABEL完成。请重新打开手机 APP 或电脑客户端的插件列表。"
}

installer_emit_metadata() {
    printf '%s\n' '#!/bin/sh' 'set -eu' "PLUGIN_NAME='$PLUGIN_NAME'" "PLUGIN_LABEL='$PLUGIN_LABEL'" "PLUGIN_VERSION='$PLUGIN_VERSION'" "UNINSTALL_NOTE='$UNINSTALL_NOTE'"
}
installer_emit_nas_common() {
    cat <<'NAS_COMMON_SCRIPT'
#!/bin/sh
# NAS checks and Shell workflow, matching the other plugin installers.
nas_fail() { printf '[错误] %s\n' "$*" >&2; exit 1; }
nas_log() { printf '[信息] %s\n' "$*"; }
nas_valid_user() {
    case "$1" in u*) ;; *) return 1;; esac
    case "${1#u}" in ''|*[!0-9]*) return 1;; esac
}
nas_metadata() {
    NAS_PLUGIN=$1
    case "$NAS_PLUGIN" in
        mihomo) NAS_LABEL=Mihomo;;
        dockermanager) NAS_LABEL=docker;;
        quickshare) NAS_LABEL=文件快传;;
        sshrecovery) NAS_LABEL="SSH 恢复";;
        *) nas_fail "无效插件名称。";;
    esac
}
nas_plain() {
    case "$1" in /*) ;; *) nas_fail "路径必须为绝对路径";; esac
    case "$1/" in */../*|*/./*) nas_fail "路径包含相对分量";; esac
    nas_node=$1
    while [ "$nas_node" != / ]; do
        [ ! -L "$nas_node" ] || nas_fail "路径存在意外重定向：$nas_node"
        nas_node=${nas_node%/*}; [ -n "$nas_node" ] || nas_node=/
    done
}
nas_atomic() (
    set -eu
    nas_target=$2
    nas_plain "$nas_target"
    nas_stage=$(mktemp "${nas_target%/*}/.sshrecovery-file.XXXXXXXX")
    trap 'rm -f -- "$nas_stage"' 0
    cp "$1" "$nas_stage"
    chmod "$3" "$nas_stage"; chown "$4" "$nas_stage"
    mv -f -- "$nas_stage" "$nas_target"
)
nas_require() {
    for nas_cmd in "$@"; do command -v "$nas_cmd" >/dev/null 2>&1 || nas_fail "NAS 缺少命令：$nas_cmd"; done
}
nas_environment() {
    [ "$(id -u)" = 0 ] || nas_fail "请以 root 身份在小米智能存储上执行。"
    [ -f /etc/config/plugin ] || nas_fail "未检测到小米智能存储配置。"
    nas_require jq plugincenter sort readlink mountpoint flock python3 runuser
}
nas_is_installed() {
    [ -d "/home/$1/plugin/$NAS_PLUGIN" ] || [ -L "/home/$1/plugin/$NAS_PLUGIN" ] ||
        { [ -f "/data/plugin/$1.list" ] && jq -e --arg p "$NAS_PLUGIN" '.[$p].install == true' "/data/plugin/$1.list" >/dev/null 2>&1; }
}
nas_scan() {
    nas_metadata "$1"; nas_action=$2
    nas_environment
    # Include accounts whose registration or home exists; never rely on find following symlinks.
    {
        for nas_item in /data/plugin/u*.list; do
            [ -f "$nas_item" ] || continue
            nas_candidate=${nas_item##*/}; nas_candidate=${nas_candidate%.list}
            nas_valid_user "$nas_candidate" && printf '%s\n' "$nas_candidate"
        done
        for nas_item in /home/u*; do
            [ -d "$nas_item" ] || continue
            nas_candidate=${nas_item##*/}
            nas_valid_user "$nas_candidate" && printf '%s\n' "$nas_candidate"
        done
    } | LC_ALL=C sort -u | while IFS= read -r nas_candidate; do
        nas_state=available
        if nas_is_installed "$nas_candidate"; then nas_state=installed; fi
        if [ "$nas_action" = uninstall ]; then
            [ "$nas_state" = installed ] || continue
        else
            id "$nas_candidate" >/dev/null 2>&1 || continue
            [ -d "/home/$nas_candidate" ] && [ -f "/data/plugin/$nas_candidate.list" ] || continue
        fi
        printf '%s\t%s\n' "$nas_candidate" "$nas_state"
    done
}
nas_pool_path() {
    # Strictly validate the whole path before creating, moving or removing anything.
    nas_path=$1; nas_kind=$2
    nas_pool=${nas_path#/nas/}; nas_pool=${nas_pool%%/*}
    case "$nas_pool" in pool*) ;; *) nas_fail "异常存储池路径：$nas_path";; esac
    case "${nas_pool#pool}" in ''|*[!0-9]*) nas_fail "异常存储池路径：$nas_path";; esac
    [ "$nas_path" = "/nas/$nas_pool/$NAS_USER/plugin/$nas_kind/$NAS_PLUGIN" ] || nas_fail "异常插件路径：$nas_path"
    mountpoint -q "/nas/$nas_pool" || nas_fail "存储池 /nas/$nas_pool 尚未挂载，请恢复硬盘挂载后重试。"
    [ -d "/nas/$nas_pool/$NAS_USER" ] || nas_fail "存储池内不存在用户 $NAS_USER。"
    nas_real=$(readlink -f "$nas_path" 2>/dev/null || true)
    [ -z "$nas_real" ] || [ "$nas_real" = "$nas_path" ] || nas_fail "插件路径重定向到其他位置：$nas_path"
}
nas_check_user() {
    NAS_USER=$1
    nas_valid_user "$NAS_USER" || nas_fail "无效用户：$NAS_USER（应为 u 后接数字）"
    [ -d "/home/$NAS_USER" ] || nas_fail "用户主目录不存在：$NAS_USER"
    id "$NAS_USER" >/dev/null 2>&1 || nas_fail "系统账户不存在：$NAS_USER"
    NAS_HOME="/home/$NAS_USER/plugin/$NAS_PLUGIN"
    nas_plain "$NAS_HOME"
    nas_plain "/data/plugin/$NAS_USER.list"
    [ ! -L "$NAS_HOME" ] || nas_fail "插件主目录不能为符号链接：$NAS_HOME"
    NAS_LIST="/data/plugin/$NAS_USER.list"
    if [ -f "$NAS_LIST" ]; then
        jq -e 'type == "object"' "$NAS_LIST" >/dev/null 2>&1 || nas_fail "插件清单损坏：$NAS_LIST"
    elif [ "$NAS_ACTION" = install ]; then
        nas_fail "插件清单不存在：$NAS_LIST"
    fi
    NAS_SRC="/nas/pool0/$NAS_USER/plugin/pluginsrc/$NAS_PLUGIN"
    NAS_TMP="/nas/pool0/$NAS_USER/plugin/plugintmp/$NAS_PLUGIN"
    if [ -L "$NAS_HOME/src" ]; then NAS_SRC=$(readlink "$NAS_HOME/src"); fi
    if [ -L "$NAS_HOME/tmp" ]; then NAS_TMP=$(readlink "$NAS_HOME/tmp"); else NAS_TMP="${NAS_SRC%/pluginsrc/*}/plugintmp/$NAS_PLUGIN"; fi
    nas_pool_path "$NAS_SRC" pluginsrc
    nas_pool_path "$NAS_TMP" plugintmp
    for nas_link in src tmp; do
        [ ! -e "$NAS_HOME/$nas_link" ] || [ -L "$NAS_HOME/$nas_link" ] || nas_fail "插件入口必须为官方目录链接：$nas_link"
    done
    if [ "$NAS_ACTION" = uninstall ]; then nas_is_installed "$NAS_USER" || nas_fail "$NAS_USER 尚未安装$NAS_LABEL。"; fi
    NAS_WEB_ROOT=$(jq -r '.settings.nginx_plugin // "/data/plugin/www"' /etc/config/plugin)
    case "$NAS_WEB_ROOT" in /*) ;; *) nas_fail "插件网页根目录配置无效。";; esac
    nas_plain "$NAS_WEB_ROOT/$NAS_USER"
    NAS_WEB="$NAS_WEB_ROOT/$NAS_USER/$NAS_PLUGIN"
    [ ! -e "$NAS_WEB" ] || [ -L "$NAS_WEB" ] || nas_fail "网页入口被其他文件占用"
    if [ -L "$NAS_WEB" ]; then
        [ "$(readlink -f "$NAS_WEB")" = "$NAS_SRC/ui" ] || nas_fail "网页入口目标异常"
    fi
    for nas_data in etc var; do
        if [ -L "$NAS_HOME/$nas_data" ]; then
            [ "$(readlink "$NAS_HOME/$nas_data")" = "/data/minas/plugins/data/$NAS_USER/$NAS_PLUGIN/$nas_data" ] || nas_fail "插件数据链接异常"
        fi
    done
    nas_plain "$NAS_HOME/scripts/control"; nas_plain "$NAS_HOME/INFO"
    [ ! -e "$NAS_HOME/scripts" ] || [ -d "$NAS_HOME/scripts" ] || nas_fail "插件脚本目录异常"
    for nas_file in INFO scripts/control; do
        [ ! -e "$NAS_HOME/$nas_file" ] || [ -f "$NAS_HOME/$nas_file" ] || nas_fail "插件文件类型异常：$nas_file"
    done
}
nas_prepare() {
    NAS_ACTION=$1; nas_metadata "$PLUGIN_NAME"
    nas_environment
    [ -n "${2:-}" ] || nas_fail "请显式指定插件用户；推荐运行 manage.sh install 或 manage.sh uninstall。"
    # Serialize installer operations across users and plugins that share helpers and icons.
    nas_plain /data/plugin/.local-plugin-installer.lock
    nas_plain "/data/plugin/.$2.plugins.lock"
    exec 7>/data/plugin/.local-plugin-installer.lock
    flock -w 60 -x 7 || nas_fail "另一个安装或卸载任务仍在执行，请稍后重试。"
    trap 'flock -u 7 2>/dev/null || true' 0
    nas_check_user "$2"
    NAS_HOME_WAS_PRESENT=false; [ ! -d "$NAS_HOME" ] || NAS_HOME_WAS_PRESENT=true
    if [ "$NAS_ACTION" = install ]; then
        python3 -B "$INSTALLER_DIR/$PLUGIN_NAME/installer/recovery/recovery.py" --help >/dev/null
        python3 -B - "$INSTALLER_DIR/$PLUGIN_NAME/installer/recovery/recovery.py" "$NAS_USER" "$NAS_PLUGIN" "$NAS_HOME" <<'MINAS_DIRECT_STATE'
import importlib.util,os,pwd,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('direct_state',sys.argv[1]);r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
user,plugin,home=sys.argv[2],sys.argv[3],Path(sys.argv[4]);owner=pwd.getpwnam(user)
r.ensure_data_layout()
r.plain(home);home.mkdir(mode=0o700,parents=True,exist_ok=True)
r.persist_state(user,plugin,home,None,(owner.pw_uid,owner.pw_gid),False)
MINAS_DIRECT_STATE
    fi
    nas_log "$NAS_LABEL：已校验用户 $NAS_USER、插件目录和存储池。"
}
nas_remove_entry() {
    [ -f "$NAS_LIST" ] || return 0
    exec 9>"/data/plugin/.$NAS_USER.plugins.lock"
    flock -x 9
    nas_next="$NAS_LIST.$NAS_PLUGIN-uninstall.$$"
    cp -p "$NAS_LIST" "$NAS_BACKUP/plugin-list.json"
    jq --arg p "$NAS_PLUGIN" 'del(.[$p])' "$NAS_LIST" > "$nas_next"
    chmod --reference="$NAS_LIST" "$nas_next"
    chown --reference="$NAS_LIST" "$nas_next"
    mv -f "$nas_next" "$NAS_LIST"
    flock -u 9
}
nas_other_users() {
    for nas_d in /home/u*/plugin/"$NAS_PLUGIN"; do
        [ ! -d "$nas_d" ] && [ ! -L "$nas_d" ] || return 0
    done
    for nas_f in /data/plugin/u*.list; do
        [ -f "$nas_f" ] || continue
        # If another user's registry is unreadable, preserve shared files.
        jq empty "$nas_f" >/dev/null 2>&1 || return 0
        if jq -e --arg p "$NAS_PLUGIN" '.[$p].install == true' "$nas_f" >/dev/null 2>&1; then return 0; fi
    done
    return 1
}
nas_uninstall() {
    nas_prepare uninstall "${1:-}"
    nas_require cp date chmod chown rm runuser
    NAS_BACKUP="/data/minas/backups/plugins/sshrecovery/$NAS_USER/$(date +%Y%m%d-%H%M%S)-$$"
    nas_plain "$NAS_BACKUP"; mkdir -p "$NAS_BACKUP"; chmod 0700 "$NAS_BACKUP"
    for nas_data in etc var; do
        nas_saved="/data/minas/plugins/data/$NAS_USER/$NAS_PLUGIN/$nas_data"
        nas_plain "$nas_saved"
        if [ -d "$nas_saved" ]; then
            mkdir "$NAS_BACKUP/$nas_data"
            cp -a "$nas_saved/." "$NAS_BACKUP/$nas_data/"
        fi
    done
    [ ! -f "$NAS_HOME/INFO" ] || cp -p "$NAS_HOME/INFO" "$NAS_BACKUP/INFO"
    cp -p "$NAS_LIST" "$NAS_BACKUP/plugin-list.json"
    # The shared component refuses removal while another privileged operation is active.
    # Do this before changing the registration or removing the UI.
    python3 -B /data/minas/privileged/component_admin.py remove --plugin "$NAS_PLUGIN" --user "$NAS_USER"
    if [ -f /data/minas/plugins/recovery/recovery.py ]; then
        python3 -B /data/minas/plugins/recovery/recovery.py forget --user "$NAS_USER" --plugin "$NAS_PLUGIN"
    fi
    nas_remove_entry
    [ ! -L "$NAS_WEB" ] || rm -f "$NAS_WEB"
    nas_pool_path "$NAS_SRC" pluginsrc; nas_pool_path "$NAS_TMP" plugintmp
    nas_plain "$NAS_HOME"
    # User cache contents must be removed with the owning account's permissions.
    runuser -u "$NAS_USER" -- rm -rf -- "$NAS_TMP"
    rm -rf -- "$NAS_SRC" "$NAS_HOME"
    if ! nas_other_users; then rm -f /data/plugin/www/icon/sshrecovery.icon; fi
    nas_log "$NAS_USER 的 SSH 恢复已卸载。配置备份：$NAS_BACKUP"
    nas_log "$UNINSTALL_NOTE"
}
# Direct read-only entry points used by the frontend over SSH.
case "${1:-}" in
    --scan)
        set -eu
        [ "$#" = 3 ] || nas_fail "扫描参数错误。"
        case "$3" in install|uninstall) ;; *) nas_fail "无效操作。";; esac
        nas_scan "$2" "$3";;
    --check)
        set -eu
        nas_metadata "$2"; NAS_ACTION=$3; shift 3
        case "$NAS_ACTION" in install|uninstall) ;; *) nas_fail "无效操作。";; esac
        nas_environment
        for nas_selected in "$@"; do nas_check_user "$nas_selected"; done;;
esac
NAS_COMMON_SCRIPT
}
installer_emit_probe() {
    installer_emit_metadata
    installer_emit_nas_common
}
installer_emit_install() {
    installer_emit_probe
    cat <<'NAS_INSTALL_SCRIPT'
INSTALLER_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
P="$INSTALLER_DIR/$PLUGIN_NAME"
nas_environment
nas_require find sha256sum cut cp mv chmod chown mktemp du awk date
nas_plain "$P"
for item in INFO src/files src/ui scripts/control installer/common installer/recovery/recovery.py; do
    [ -e "$P/$item" ] || nas_fail "安装包缺少 $item"
done
[ -z "$(find "$P/src" "$P/scripts" -type l -print -quit)" ] || nas_fail "安装资源不允许符号链接"
# A small format check; installation control stays in Shell.
python3 -B - "$P/src/ui/icon.png" <<'SSH_ICON_CHECK'
from pathlib import Path
import sys
data=Path(sys.argv[1]).read_bytes()
if data[:8]!=b'\x89PNG\r\n\x1a\n' or data[16:24]!=(300).to_bytes(4,'big')*2:
    raise SystemExit('安装包图标必须为 300×300 PNG')
SSH_ICON_CHECK
nas_prepare install "${1:-}"
owner="$NAS_USER:$(id -gn "$NAS_USER")"
src=$NAS_SRC; home=$NAS_HOME; icon=/data/plugin/www/icon/sshrecovery.icon
nas_plain "$icon"
mkdir -p "${src%/*}" "$NAS_TMP" "$home/scripts" "${NAS_WEB%/*}" "${icon%/*}"
chown "$owner" "$home" "$home/scripts" "$NAS_TMP"
chmod 0700 "$home" "$home/scripts" "$NAS_TMP"
stage=$(mktemp -d "${src%/*}/.sshrecovery-new.XXXXXXXX")
backup=$(mktemp -d "${src%/*}/.sshrecovery-old.XXXXXXXX")
switched=false; moved=false; committed=false
web_existed=false; [ ! -L "$NAS_WEB" ] || web_existed=true
for item in INFO scripts/control; do
    if [ -f "$home/$item" ]; then mkdir -p "$(dirname "$backup/$item")"; cp -p "$home/$item" "$backup/$item"; fi
done
[ ! -f "$icon" ] || cp -p "$icon" "$backup/icon"
exec 9>"/data/plugin/.$NAS_USER.plugins.lock"; flock -x 9
cp -p "$NAS_LIST" "$backup/plugin-list.json"
was_enabled=$(jq -c --arg p "$PLUGIN_NAME" '.[$p] as $e | if ($e.enable|type)=="boolean" then $e.enable else true end' "$NAS_LIST")
was_status=$(jq -r --arg p "$PLUGIN_NAME" '.[$p].status // "running"' "$NAS_LIST")
ssh_install_finish() {
    rc=$1; trap - 0 HUP INT TERM; set +e
    if [ "$committed" != true ]; then
        flock -x 9
        nas_log "安装未完成，正在恢复原插件文件与登记。"
        if [ "$switched" = true ]; then rm -rf -- "$src"; fi
        if [ "$moved" = true ]; then mv "$backup/src" "$src"; fi
        for item in INFO scripts/control; do
            if [ -f "$backup/$item" ]; then
                mode=0644; [ "$item" != scripts/control ] || mode=0755
                nas_atomic "$backup/$item" "$home/$item" "$mode" "$owner"
            else rm -f "$home/$item"; fi
        done
        restore=$(mktemp "${NAS_LIST%/*}/.sshrecovery-registry.XXXXXXXX")
        cp -p "$backup/plugin-list.json" "$restore" && mv -f "$restore" "$NAS_LIST"
        if [ -f "$backup/icon" ]; then nas_atomic "$backup/icon" "$icon" 0644 root:root; else rm -f "$icon"; fi
        if [ "$web_existed" != true ]; then rm -f "$NAS_WEB"; fi
        if [ "$NAS_HOME_WAS_PRESENT" != true ]; then rm -rf -- "$home"; fi
        [ "$rc" != 0 ] || rc=1
    fi
    rm -rf -- "$stage" "$backup"
    flock -u 9; flock -u 7
    exit "$rc"
}
trap 'ssh_install_finish $?' 0
trap 'exit 130' INT; trap 'exit 143' HUP TERM
cp -R "$P/src/files" "$stage/files"; cp -R "$P/src/ui" "$stage/ui"
find "$stage" -type d -exec chmod 0755 {} +
find "$stage" -type f -exec chmod 0644 {} +
find "$stage" -name '*.cgi' -type f -exec chmod 0755 {} +
chown -R "$owner" "$stage"; chmod 0700 "$stage"
if [ -d "$src" ]; then mv "$src" "$backup/src"; moved=true; fi
mv "$stage" "$src"; switched=true
nas_atomic "$P/scripts/control" "$home/scripts/control" 0755 "$owner"
for item in src tmp; do
    target=$src; [ "$item" != tmp ] || target=$NAS_TMP
    if [ ! -L "$home/$item" ]; then ln -s "$target" "$home/$item"; fi
    chown -h "$owner" "$home/$item"
done
find "$src" -type f | LC_ALL=C sort | while IFS= read -r file; do sha256sum "$file" | cut -d' ' -f1; done > "$backup/digests"
abstract=$(sha256sum "$backup/digests" | cut -d' ' -f1)
size=$(du -sk "$src" | awk '{print $1*1024}'); now=$(date +%s)
jq --arg v "$PLUGIN_VERSION" --arg a "$abstract" --argjson n "$now" --argjson s "$size" \
    '.version=$v | .abstract=$a | .timestamp=$n | .size=$s | .hotplug=[]' "$P/INFO" > "$backup/new-info"
nas_atomic "$backup/new-info" "$home/INFO" 0644 "$owner"
if [ ! -L "$NAS_WEB" ]; then ln -s "$home/src/ui" "$NAS_WEB"; fi
chown -h "$owner" "$NAS_WEB"
nas_atomic "$P/src/ui/icon.png" "$icon" 0644 root:root
jq -n --arg mpk "$home/INFO" --slurpfile f "$src/ui/config" --slurpfile i "$home/INFO" \
    --argjson now "$now" --argjson enabled "$was_enabled" --arg status "$was_status" \
    '{resource:{mpk:$mpk,icon:"/icon/sshrecovery.icon",preview:[]},status:$status,install:true,upgrade:false,enable:$enabled,changetime:$now,icon:"/icon/sshrecovery.icon",progress:"100",frontend:$f[0],info:$i[0],online:true}' > "$backup/entry"
next=$(mktemp "${NAS_LIST%/*}/.sshrecovery-registry.XXXXXXXX")
jq --slurpfile e "$backup/entry" '.sshrecovery=$e[0]' "$NAS_LIST" > "$next"
chmod --reference="$NAS_LIST" "$next"; chown --reference="$NAS_LIST" "$next"; mv -f "$next" "$NAS_LIST"
runuser -u "$NAS_USER" -- env PLUG_USER="$NAS_USER" PLUG_NAME="$PLUGIN_NAME" PLUG_HOME_DIR="$home" \
    PLUG_SRC_DIR="$src" PLUG_TMP_DIR="$NAS_TMP" PLUG_STATUS=unverified /usr/bin/plugin.sh verify || nas_fail "系统源码校验未通过"
python3 -B "$P/installer/common/component_admin.py" install --plugin "$PLUGIN_NAME" --user "$NAS_USER" --payload "$P" --plugin-version "$PLUGIN_VERSION"
# Reuse the SSH module's root path validation and atomic writer for the private key copy.
python3 -B - <<'SSH_HOST_SNAPSHOT'
import runpy
from pathlib import Path
m=runpy.run_path('/data/minas/privileged/modules/sshrecovery/ssh-recovery-helper',run_name='installer_host_snapshot')
host=Path('/etc/dropbear/dropbear_rsa_host_key');saved=m['STATE']/'host/dropbear_rsa_host_key'
m['safe'](host);m['safe'](saved)
if host.exists():
    if not host.is_file() or not 0<host.stat().st_size<=65536:raise RuntimeError('主机密钥异常')
    if not saved.exists():m['atomic'](saved,host.read_bytes(),0o400)
SSH_HOST_SNAPSHOT
# Updating an existing recovery tool must never enable SSH or add a boot job.
python3 -B /data/minas/privileged/modules/sshrecovery/ssh-recovery-helper --refresh-installed
committed=true
flock -u 9
python3 -B "$P/installer/recovery/recovery.py" capture --user "$NAS_USER" --plugin "$PLUGIN_NAME" --package "$P"
nas_log "SSH 恢复 $PLUGIN_VERSION 已安装；保留原启用状态、SSH 服务与登录公钥。"

NAS_INSTALL_SCRIPT
}
installer_emit_uninstall() {
    installer_emit_probe
    printf '%s\n' 'nas_uninstall "${1:-}"'
}
manage_usage() {
    cat <<EOF
$PLUGIN_LABEL — 安装与卸载
  bash manage.sh                         交互菜单
  bash manage.sh install [选项]           安装或更新
  bash manage.sh uninstall [选项]         卸载
  bash manage.sh install --help           查看安装参数
  bash manage.sh uninstall --help         查看卸载参数

NAS 本机自动直接执行；WSL/Linux 自动通过 root SSH 连接。
支持 --ip、--users、--all-users、--list-users、--dry-run 和 --yes。
EOF
}
if (($# == 0)); then
    [[ -t 0 ]] || { manage_usage; installer_error "非交互运行请指定 install 或 uninstall。"; }
    printf '\n%s — 安装与卸载\n  1) 安装 / 更新\n  2) 卸载\n  0) 退出\n' "$PLUGIN_LABEL"
    read -r -p '请选择操作：' choice || installer_error "输入已结束。"
    case $choice in 1) set -- install;; 2) set -- uninstall;; 0|q) exit 0;; *) installer_error "无效选项。";; esac
fi
case $1 in
    install|uninstall) operation=$1; shift; installer_main "$operation" "$@";;
    -h|--help) manage_usage;;
    *) manage_usage; installer_error "未知操作：$1";;
esac
