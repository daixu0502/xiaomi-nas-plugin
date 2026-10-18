#!/bin/sh


ACTION=$1
CONFIG="/etc/config/plugin"
PLUG_POOL_DIR="${PLUG_POOL_DIR}/${PLUG_USER}/plugin"

CODE_FULL="11"

log() {
    logger -t "plugin.sh" "[$PLUG_USER] [$PLUG_NAME]: $*"
    return 0
}

_lockup() {
    local plug_src_real writable_dir

    # / rx
    chown root:"$PLUG_USER" "$PLUG_HOME_DIR"
    chmod 750 "$PLUG_HOME_DIR"

    # src rwx
    plug_src_real=$(readlink -f "$PLUG_SRC_DIR")
    if [ -d "$plug_src_real" ]; then
        chown -R "$PLUG_USER":"$PLUG_USER" "$plug_src_real"
        chmod -R u+rwx "$plug_src_real"
    fi

    # scripts rwx
    if [ -d "$PLUG_HOME_DIR/scripts" ]; then
        chown -R "$PLUG_USER":"$PLUG_USER" "$PLUG_HOME_DIR/scripts"
        chmod -R u+rwx "$PLUG_HOME_DIR/scripts"
    fi

    # etc tmp var rwx
    for writable_dir in "$PLUG_ETC_DIR" "$PLUG_TMP_DIR" "$PLUG_VAR_DIR"; do
        writable_dir=$(readlink -f "$writable_dir")
        [ -d "$writable_dir" ] || continue
        chown root:"$PLUG_USER" "$writable_dir"
        chmod 770 "$writable_dir"
    done

    # reserve rwx
    chown root:"$PLUG_USER" "$PLUG_RESERVE_DIR"
    chmod 710 "$PLUG_RESERVE_DIR"
    chown "$PLUG_USER":"$PLUG_USER" "$PLUG_RESERVE_PLUG_DIR"
    chmod 700 "$PLUG_RESERVE_PLUG_DIR"
}

_lockdown() {
    local plug_src_real writable_dir

    # / rx
    chown root:"$PLUG_USER" "$PLUG_HOME_DIR"
    chmod 550 "$PLUG_HOME_DIR"

    # INFO 0
    chown root:root "$PLUG_HOME_DIR/INFO"
    chmod 600 "$PLUG_HOME_DIR/INFO"

    # scripts rx
    if [ -d "$PLUG_HOME_DIR/scripts" ]; then
        chown -R root:"$PLUG_USER" "$PLUG_HOME_DIR/scripts"
        find "$PLUG_HOME_DIR/scripts" -type d -exec chmod 550 {} \;
        find "$PLUG_HOME_DIR/scripts" -type f -exec chmod 550 {} \;
    fi

    # src rx
    plug_src_real=$(readlink -f "$PLUG_SRC_DIR")
    if [ -d "$plug_src_real" ]; then
        chown -R root:"$PLUG_USER" "$plug_src_real"
        find "$plug_src_real" -type d -exec chmod 550 {} \;
        find "$plug_src_real" -type f -exec chmod ug+r,g+X,o-rwx,go-w {} \;
    fi

    # etc tmp var rwx
    for writable_dir in "$PLUG_ETC_DIR" "$PLUG_TMP_DIR" "$PLUG_VAR_DIR"; do
        writable_dir=$(readlink -f "$writable_dir")
        [ -d "$writable_dir" ] || continue
        chown root:"$PLUG_USER" "$writable_dir"
        chmod 770 "$writable_dir"
    done

    # reserve rx
    chown root:"$PLUG_USER" "$PLUG_RESERVE_PLUG_DIR"
    chmod 550 "$PLUG_RESERVE_PLUG_DIR"
    chown root:"$PLUG_USER" "$PLUG_RESERVE_DIR"
    chmod 550 "$PLUG_RESERVE_DIR"
}

# topic: /sys/{mid}/{mid}/plugin
# payload: {"action":"install|uninstall","plugin":"plugin_name","timestamp":"timestamp"}
_notice() {
    local mid timestamp
    local topic payload msg

    mid="${PLUG_USER#u}"
    timestamp="$(date +%s%3N)"

    topic="/sys/$mid/$mid/plugin"
    payload=$(jq -c -n --arg action "$PLUG_ACTION" --arg plugin "$PLUG_NAME" --arg timestamp "$timestamp" \
      '{action:$action,plugin:$plugin,timestamp:$timestamp}')

    msg=$(jq -c -n --arg topic "$topic" --argjson payload "$payload" \
      '{topic:$topic,payload:$payload}')

    msg=$(echo "$msg" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')

    log "[$PLUG_USER-$PLUG_NAME-$PLUG_ACTION] publish msg: topic=$topic, payload=$payload"
    gdbus call --system --dest com.minas.msgcenter \
        --object-path /com/minas/msgcenter \
        --method com.minas.msgcenter.interface.publish "\"$msg\""
}

plugin_verify() {
    local file list_file
    local file_digest_tmp file_info file_info_tmp
    local res=0

    [ ! -d "$PLUG_SRC_DIR" ] && return 1

    file_info="$PLUG_HOME_DIR/INFO"
    [ ! -f "$file_info" ] && return 1

    set -e

    file_info_tmp="$PLUG_TMP_DIR/INFO"
    rm -rf "$file_info_tmp"
    file_digest_tmp="$PLUG_TMP_DIR/digest"
    rm -rf "$file_digest_tmp"

    list_file=$(find "$PLUG_SRC_DIR/" -type f | LC_COLLATE=C sort)
	for file in $list_file; do
		sha256sum "$file" | cut -d ' ' -f 1 >> "$file_digest_tmp"
	done
    abstract=$(sha256sum "$file_digest_tmp" | cut -d ' ' -f 1)

    if [ "installing" = "$PLUG_STATUS" ]; then
        jq --arg k "abstract" --arg v "$abstract" '.[$k] = $v' "$file_info" > "$file_info_tmp"
        mv -f "$file_info_tmp" "$file_info"
        log "verify: calc abstract $abstract"
        res=0
    else
        abstract_read=$(jq -r '.abstract' "$file_info")
        if [ "$abstract" = "$abstract_read" ]; then
            res=0
        else
            log "verify failed"
            res=1
        fi
        log "verify: check abstract, read=$abstract_read, calc=$abstract"
    fi

    rm -rf "$file_digest_tmp"
    rm -rf "$file_info_tmp"

    set +e
    log "verify: return $res"
    return $res
}

plugin_preinstall()
{
    set -e

    if [ "false" = "$PLUG_SYSTEM" ]; then
        [ ! -d "$PLUG_POOL_DIR" ] && return 1
    fi

    # todo
    # compare vol1 free size with plugin size

    # create dir
    if [ "upgrade" = "$PLUG_ACTION" ]; then
        rm -rf "$PLUG_TMP_DIR"
        rm -rf "$PLUG_SRC_DIR"
    else
        rm -rf "$PLUG_HOME_DIR" && mkdir -p "$PLUG_HOME_DIR"
        rm -rf "$PLUG_VAR_DIR" && mkdir -p "$PLUG_VAR_DIR"
        rm -rf "$PLUG_ETC_DIR" && mkdir -p "$PLUG_ETC_DIR"

        if [ ! -d "$PLUG_RESERVE_DIR" ]; then
            mkdir -p "$PLUG_RESERVE_DIR"
        fi

        if [ ! -d "$PLUG_RESERVE_PLUG_DIR" ]; then
            mkdir -p "$PLUG_RESERVE_PLUG_DIR"
        fi
    fi

    if [ "true" = "$PLUG_SYSTEM" ]; then
        mkdir -p "$PLUG_TMP_DIR"
        mkdir -p "$PLUG_SRC_DIR"
    else
        rm -rf "$PLUG_POOL_DIR/pluginsrc/$PLUG_NAME"
        mkdir -p "$PLUG_POOL_DIR/pluginsrc/$PLUG_NAME" || exit "$CODE_FULL"
        rm -rf "$PLUG_POOL_DIR/plugintmp/$PLUG_NAME"
        mkdir -p "$PLUG_POOL_DIR/plugintmp/$PLUG_NAME" || exit "$CODE_FULL"
        ln -s "$PLUG_POOL_DIR/plugintmp/$PLUG_NAME" "$PLUG_TMP_DIR"
        ln -s "$PLUG_POOL_DIR/pluginsrc/$PLUG_NAME" "$PLUG_SRC_DIR"
    fi

    _lockup

    set +e
    log "preinstall: return 0"
    return 0
}

plugin_install() {
    local dir_nginx_plugin dir_link

    set -e

    dir_nginx_plugin=$(jq -r '.settings.nginx_plugin' "$CONFIG")

    # move plugin files
    cp -r "$PLUG_DOWNLOAD_EXTRACT"/* "$PLUG_HOME_DIR"
    tar -xzvf "$PLUG_HOME_DIR"/plugin.tgz -C "$PLUG_SRC_DIR" >/dev/null 2>&1 || exit "$CODE_FULL"
    rm -rf "$PLUG_HOME_DIR"/plugin.tgz

    _lockup
    log "install: plugin files install success"

    # link ui files
    mkdir -p "$dir_nginx_plugin"/"$PLUG_USER"
    chown "$PLUG_USER":"$PLUG_USER" "$dir_nginx_plugin"/"$PLUG_USER"

    dir_link="$dir_nginx_plugin"/"$PLUG_USER"/"$PLUG_NAME"
    rm -rf "$dir_link"
    ln -s "$PLUG_SRC_DIR"/ui "$dir_link"
    chown -h "$PLUG_USER":"$PLUG_USER" "$dir_link"

    set +e
    log "install: return 0"
    return 0
}

plugin_postinstall() {
    systemctl reload dbus &

    rm -rf "$PLUG_DOWNLOAD_DIR"

    _lockdown || res=1

    [ "false" = "$PLUG_SYSTEM" ] && {
        # do sync
        dsync_path "$PLUG_USER" /nas/pool0/"$PLUG_USER"/plugin 1
    }
    return 0
}

plugin_preuninstall() {
    # lockup reserve dir
    chown root:"$PLUG_USER" "$PLUG_RESERVE_DIR"
    chmod 710 "$PLUG_RESERVE_DIR"
    chown "$PLUG_USER":"$PLUG_USER" "$PLUG_RESERVE_PLUG_DIR"
    chmod 700 "$PLUG_RESERVE_PLUG_DIR"
}

plugin_postuninstall() {
    local dir_nginx_plugin
    local dir_user_nginx_plugin

    set -e

    # Remove the plugin UI link.
    dir_nginx_plugin=$(jq -r '.settings.nginx_plugin' "$CONFIG")
    dir_user_nginx_plugin="$dir_nginx_plugin"/"$PLUG_USER"/"$PLUG_NAME"
    [ -d "$dir_user_nginx_plugin" ] && rm -rf "$dir_user_nginx_plugin"

    # Remove plugin files.
    if [ "upgrade" = "$PLUG_ACTION" ]; then
        # for upgrade, save plugin's var and etc dir
        [ -d "$PLUG_TMP_DIR" ] && rm -rf "$PLUG_TMP_DIR"
        [ -d "$PLUG_SRC_DIR" ] && rm -rf "$PLUG_SRC_DIR"
    else
        [ -d "$PLUG_HOME_DIR" ] && rm -rf "$PLUG_HOME_DIR"

    fi

    if [ "false" = "$PLUG_SYSTEM" ]; then
        rm -rf "$PLUG_POOL_DIR/pluginsrc/$PLUG_NAME"
        rm -rf "$PLUG_POOL_DIR/plugintmp/$PLUG_NAME"
    fi

    if [ "upgrade" != "$PLUG_ACTION" ]; then
        rm -rf "$PLUG_DOWNLOAD_DIR"
    fi

    # Lock the reserve directories.
    chown root:"$PLUG_USER" "$PLUG_RESERVE_PLUG_DIR"
    chmod 550 "$PLUG_RESERVE_PLUG_DIR"
    chown root:"$PLUG_USER" "$PLUG_RESERVE_DIR"
    chmod 550 "$PLUG_RESERVE_DIR"

    set +e
    log "postuninstall: return 0"
}

plugin_predownload()
{
    if [ -z "$PLUG_DOWNLOAD_DIR" ] || [ -z "$PLUG_DOWNLOAD_EXTRACT" ]; then
        return 1
    fi

    [ -d "$PLUG_DOWNLOAD_DIR" ] && rm -rf "$PLUG_DOWNLOAD_DIR"

    mkdir -p "$PLUG_DOWNLOAD_DIR"
    mkdir -p "$PLUG_DOWNLOAD_EXTRACT"
    return 0
}

plugin_test()
{
    return 0
}

plugin_abinstall()
{
    [ "install" = "$PLUG_ACTION" ] && _notice > /dev/null 2>&1 &
    return 0
}

plugin_abuninstall()
{
    [ "uninstall" = "$PLUG_ACTION" ] && _notice > /dev/null 2>&1 &
    return 0
}

shift
log "do $ACTION"
case $ACTION in
    boot)
        plugin_boot "$@"
        ;;
    predownload)
        plugin_predownload "$@"
        ;;
    preinstall)
        plugin_preinstall "$@"
        ;;
    install)
        plugin_install "$@"
        ;;
    abinstall)
        plugin_abinstall "$@"
        ;;
    verify)
        plugin_verify "$@"
        ;;
    postinstall)
        plugin_postinstall "$@"
        ;;
    preuninstall)
        plugin_preuninstall "$@"
        ;;
    postuninstall)
        plugin_postuninstall "$@"
        ;;
    abuninstall)
        plugin_abuninstall "$@"
        ;;
    hotplug)
        plugin_hotplug "$@"
        ;;
    test)
        plugin_test "$@"
        ;;
    *)
        exit 0
        ;;
esac