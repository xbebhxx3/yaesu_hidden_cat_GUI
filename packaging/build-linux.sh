#!/usr/bin/env bash
# Build the Linux onefile executable into dist/.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3}"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/yaesu-hedden-cat"
VENV="${VENV:-$CACHE_DIR/venv-linux}"

if [ ! -x "$VENV/bin/python" ]; then
    mkdir -p "$CACHE_DIR"
    "$PY" -m venv "$VENV"
    "$VENV/bin/python" -m pip install -q --upgrade pip
    "$VENV/bin/python" -m pip install -q pyinstaller pyserial
fi

"$VENV/bin/python" -m PyInstaller --noconfirm --clean \
    --distpath "$ROOT/dist" --workpath "$ROOT/build" \
    "$ROOT/packaging/yaesu_hedden_cat.spec"

echo "built: $ROOT/dist/yaesu-hedden-cat"
