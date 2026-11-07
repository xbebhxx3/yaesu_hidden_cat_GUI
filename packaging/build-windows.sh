#!/usr/bin/env bash
# Build the Windows onefile executable on Linux through Wine.
#
# PyInstaller cannot cross-compile: it must run on the target OS.  This script
# installs a Windows Python (with Tkinter) into a Wine prefix and runs
# PyInstaller there, producing dist/yaesu-hedden-cat.exe.
#
# Everything the build downloads or caches (installer, Wine prefix, staged
# sources and intermediates) stays inside the project's build/ directory.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
PREFIX="${WINEPREFIX:-$BUILD/wineprefix}"
PYVER="${PYVER:-3.12.10}"
PYDIR='C:\Python312'

export WINEPREFIX="$PREFIX"
export WINEARCH=win64
export WINEDEBUG="${WINEDEBUG:--all}"
export TMPDIR="$BUILD/tmp"
mkdir -p "$BUILD/windows" "$TMPDIR"

if ! command -v wine >/dev/null 2>&1 || ! command -v winepath >/dev/null 2>&1; then
    echo "error: wine/winepath not found" >&2
    exit 1
fi

# Reuse the downloaded installer when it is already in build/windows/.
INST="$BUILD/windows/python-$PYVER-amd64.exe"
if [ ! -f "$INST" ]; then
    curl -sSL -o "$INST" \
        "https://www.python.org/ftp/python/$PYVER/python-$PYVER-amd64.exe"
fi

if [ ! -f "$PREFIX/drive_c/Python312/python.exe" ]; then
    wine "$INST" /quiet InstallAllUsers=0 "TargetDir=$PYDIR" \
        Include_tcltk=1 Include_pip=1 Include_test=0 PrependPath=0 Shortcuts=0
fi

if ! wine "$PYDIR\\python.exe" -c "import PyInstaller" >/dev/null 2>&1; then
    wine "$PYDIR\\python.exe" -m pip install -q --no-warn-script-location \
        pyinstaller pyserial
fi

# Stage the sources under the project; intermediates never leave build/.
STAGE="$BUILD/win-stage"
rm -rf "$STAGE"
mkdir -p "$STAGE/src"
rsync -a --exclude .git --exclude build --exclude dist \
    --exclude __pycache__ --exclude '*backup*.json' "$ROOT/" "$STAGE/src/"

STAGE_WIN="$(winepath -w "$STAGE")"
wine "$PYDIR\\python.exe" -m PyInstaller --noconfirm --clean \
    --distpath "$STAGE_WIN\\dist" --workpath "$STAGE_WIN\\work" \
    "$STAGE_WIN\\src\\packaging\\yaesu_hedden_cat.spec"

mkdir -p "$ROOT/dist"
cp "$STAGE/dist/yaesu-hedden-cat.exe" "$ROOT/dist/"
rm -rf "$STAGE"
echo "built: $ROOT/dist/yaesu-hedden-cat.exe"
