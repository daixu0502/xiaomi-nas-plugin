#!/bin/sh
set -eu
status=${1:-running}; enabled=${2:-true}; plugin_user=${3:-${PLUG_USER:-}}
case "$plugin_user" in u[0-9]*) ;; *) exit 1 ;; esac
SRC_DIR=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
LIST_FILE="/data/plugin/$plugin_user.list"; INFO="/home/$plugin_user/plugin/quickshare/INFO"; FRONT="$SRC_DIR/ui/config"; LOCK="/data/plugin/.$plugin_user.plugins.lock"
[ -f "$LIST_FILE" ] && [ -f "$INFO" ] && [ -f "$FRONT" ] || exit 1
exec 9>"$LOCK"; flock -x 9
tmp="$LIST_FILE.quickshare.$$"
jq --slurpfile f "$FRONT" --arg mpk "$INFO" --slurpfile i "$INFO" --arg status "$status" --argjson enabled "$enabled" --argjson now "$(date +%s)" '
 .quickshare=((.quickshare//{})+{resource:{mpk:$mpk,icon:"/icon/quickshare.icon",preview:[]},status:$status,install:true,upgrade:false,enable:$enabled,changetime:$now,icon:"/icon/quickshare.icon",progress:"100",frontend:$f[0],info:$i[0],online:true})' "$LIST_FILE" > "$tmp"
jq empty "$tmp"; chmod --reference="$LIST_FILE" "$tmp" 2>/dev/null || chmod 0644 "$tmp"; chown --reference="$LIST_FILE" "$tmp" 2>/dev/null || true; mv -f "$tmp" "$LIST_FILE"; flock -u 9
