#!/usr/bin/env bash
# FlightLogger setup for Ubuntu 22.04 / 24.04 (and derivatives).
#   ./install.sh            system packages (asks for sudo if any are missing),
#                           Python venv, dependencies, app-menu launcher
#   ./install.sh --no-apt   skip the system package step
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
cd "$HERE"
NO_APT=0
[[ "${1:-}" == "--no-apt" ]] && NO_APT=1

# --- system packages ---------------------------------------------------------
# libmpv: package name differs between releases (22.04: libmpv1, 24.04+: libmpv2)
if apt-cache show libmpv2 >/dev/null 2>&1; then MPV_PKG=libmpv2; else MPV_PKG=libmpv1; fi
PKGS=(python3-venv "$MPV_PKG" libxcb-cursor0 libgl1 libegl1)
missing=()
for p in "${PKGS[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
done
if ((${#missing[@]})); then
    if ((NO_APT)); then
        echo "Warning: missing system packages: ${missing[*]}" >&2
    else
        echo "Installing system packages: ${missing[*]}"
        sudo apt-get install -y "${missing[@]}"
    fi
fi

# --- Python ------------------------------------------------------------------
python3 - <<'EOF'
import sys
if sys.version_info < (3, 10):
    sys.exit(f"FlightLogger needs Python 3.10 or newer, found {sys.version.split()[0]}")
EOF
if [[ ! -x .venv/bin/python ]]; then
    if ! python3 -m venv .venv 2>/dev/null; then
        # python3-venv missing (ensurepip unavailable): venv without pip, then add pip
        rm -rf .venv
        python3 -m venv --without-pip .venv
        python3 -m pip --python .venv/bin/python install --quiet pip
    fi
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e ".[dev]"

# --- launcher ------------------------------------------------------------------
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
cat > "$APPS/flightlogger.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=FlightLogger
Comment=PX4 flight log viewer with Flight Review graphs and video sync
Exec="$HERE/flightlogger.sh" %f
Path=$HERE
Icon=applications-science
Terminal=false
Categories=Science;Engineering;
EOF
chmod +x "$APPS/flightlogger.desktop" "$HERE/flightlogger.sh"

echo
echo "FlightLogger is installed."
echo "  Run:  $HERE/flightlogger.sh [log.ulg]"
echo "  or pick FlightLogger from the applications menu."
