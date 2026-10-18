#!/bin/sh
# Fixed root-owned systemd broker; all daemon code still runs as the NAS user.
minas_service_control() {
    minas_plugin=$1; minas_user=$2; minas_action=$3
    [ "${MINAS_SERVICE_MANAGED:-}" != 1 ] || return 1
    [ -f "/etc/minas-plugin-services/$minas_user-$minas_plugin" ] || return 1
    case "$minas_action" in
        status) minas_operation=status;;
        start|restart|stop) minas_operation=$minas_action;;
        enable|postinstall|postupgrade) minas_operation=lifecycle-start;;
        disable|preuninstall|preupgrade) minas_operation=lifecycle-stop;;
        *) return 1;;
    esac
    if [ "$(id -u)" = 0 ]; then
        /usr/bin/python3 -B /data/.minas-plugin-recovery/recovery.py service --user "$minas_user" --plugin "$minas_plugin" --operation "$minas_operation"
    else
        sudo -n /usr/bin/python3 -B /data/.minas-plugin-recovery/recovery.py service --user "$minas_user" --plugin "$minas_plugin" --operation "$minas_operation"
    fi
    exit $?
}
