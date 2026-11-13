#!/bin/sh
# The only supported installer entry point, for NAS and Linux/WSL.
set -eu
umask 077
BASE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$BASE/components/manager/cli.py" ]; then
    ENTRY="$BASE/components/manager/cli.py"
else
    ENTRY="$BASE/plugins/common/manager/cli.py"
fi
command -v python3 >/dev/null 2>&1 || { echo '需要 Python 3.9+，请在 WSL/Linux 或 NAS root 终端运行。' >&2; exit 2; }
exec python3 -B "$ENTRY" "$@"
