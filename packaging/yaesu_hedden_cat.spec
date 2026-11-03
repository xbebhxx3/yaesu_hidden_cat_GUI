# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Yaesu Hedden CAT (Linux and Windows onefile builds).

Model profiles and research data are external files.  They are bundled into the
executable and resolved relative to the package directory at runtime.
"""
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "radio_cat" / "profiles"), "radio_cat/profiles"),
        (str(ROOT / "radio_cat" / "data"), "radio_cat/data"),
    ],
    hiddenimports=["serial", "serial.tools.list_ports"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "numpy", "PIL"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="yaesu-hedden-cat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
