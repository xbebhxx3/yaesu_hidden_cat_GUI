#!/usr/bin/env bash
# Build the Linux onefile executable into dist/.
#
# Everything the build downloads or caches (virtualenv, pip cache, PyInstaller
# cache and intermediates) stays inside the project's build/ directory.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
VENV="${VENV:-$BUILD/venv-linux}"

export PIP_CACHE_DIR="$BUILD/pip-cache"
export PYINSTALLER_CONFIG_DIR="$BUILD/pyinstaller"
export TMPDIR="$BUILD/tmp"
mkdir -p "$BUILD" "$PIP_CACHE_DIR" "$PYINSTALLER_CONFIG_DIR" "$TMPDIR"

PY="${PYTHON:-python3}"
if [ ! -x "$VENV/bin/python" ]; then
    "$PY" -m venv "$VENV"
    "$VENV/bin/python" -m pip install -q --upgrade pip
    "$VENV/bin/python" -m pip install -q pyinstaller pyserial
fi

"$VENV/bin/python" -m PyInstaller --noconfirm --clean \
    --distpath "$ROOT/dist" --workpath "$BUILD/pyinstaller-work" \
    "$ROOT/packaging/yaesu_hedden_cat.spec"

echo "built: $ROOT/dist/yaesu-hedden-cat"
