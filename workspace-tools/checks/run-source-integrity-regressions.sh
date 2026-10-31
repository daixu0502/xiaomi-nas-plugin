#!/bin/sh
# Local regression suites require Linux, root isolation, and a built signed suite.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR/../.."
sh -n manage.sh
for file in scripts/*.sh; do bash -n "$file"; done
if [ ! -f workspace-tools/tests/test_unified_manager.py ]; then
    echo '本地测试未提供；tests 不随源码或安装包发布。' >&2
    exit 2
fi
for suite in test_unified_manager.py test_manager_transactions.py test_manager_components.py test_manager_recovery.py test_manager_updates.py test_manager_user_io.py test_manager_scheduler.py test_verification_locale.py test_service_controls.py test_manager_shell_entry.py test_manager_component_cleanup.py test_manager_broken_uninstall.py test_manager_cache_descriptor.py; do
    python3 -B -m unittest discover -s workspace-tools/tests -p "$suite"
done
