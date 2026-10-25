#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR/.."
bash -n mihomo/manage.sh
for file in mihomo/src/files/*.sh mihomo/src/ui/mihomo.cgi; do
    sh -n "$file"
done
for plugin in downloadcenter quickshare taskcenter nascenter dockermanager sshrecovery mihomo; do
    printf '%s\n' "$plugin"
    if [ -d "$plugin/tests" ]; then
        python3 -B -m unittest discover -s "$plugin/tests" -p 'test_*.py'
    else
        printf '%s\n' 'No unit-test directory; source paths audited separately.'
    fi
done
