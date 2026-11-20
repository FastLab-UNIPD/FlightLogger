#!/usr/bin/env bash
# Launch FlightLogger. Optional argument: a .ulg log to open.
#   ./flightlogger.sh                 -> empty window
#   ./flightlogger.sh flight.ulg      -> opens the log
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
PY="$HERE/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
    msg="FlightLogger: Python environment not found in $HERE/.venv (see README.md, Setup)."
    echo "$msg" >&2
    command -v notify-send >/dev/null && notify-send "FlightLogger" "$msg"
    exit 1
fi
# Resolve a log path given relative to the caller's directory before changing dir
args=()
for a in "$@"; do
    [[ -e "$a" ]] && a="$(readlink -f "$a")"
    args+=("$a")
done
cd "$HERE"
exec "$PY" -m flightlogger "${args[@]}"
