# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Yaesu Hedden CAT (Linux and Windows onefile builds).

Source lives in src/; model profiles and research data are external files that
are bundled into the executable and resolved relative to the package directory
at runtime.
"""
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"

# stdlib the application never imports. Excluding it only shrinks the archive;
# anything the transport, protocol or UI depends on stays out of this list.
UNUSED_STDLIB = [
    "asyncio", "concurrent", "multiprocessing",
    "decimal", "_decimal",
    "doctest", "lib2to3", "pydoc", "pydoc_data", "unittest", "test",
    "distutils", "ensurepip", "setuptools", "pkg_resources", "pip", "venv",
    "sqlite3",
    "ssl", "_ssl", "hashlib", "_hashlib", "hmac", "secrets",
    "bz2", "_bz2", "lzma", "_lzma",
    "email", "http", "xml", "xmlrpc",
]

a = Analysis(
    [str(SRC / "main.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[
        (str(SRC / "radio_cat" / "profiles"), "radio_cat/profiles"),
        (str(SRC / "radio_cat" / "data"), "radio_cat/data"),
    ],
    hiddenimports=["serial", "serial.tools.list_ports"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=UNUSED_STDLIB,
    noarchive=False,
    optimize=1,
)

# Tk's optional BLT extension is pulled in by the tkinter hook but never loaded;
# dropping it saves ~600 kB compressed without affecting the UI.
a.binaries = [entry for entry in a.binaries if not entry[0].startswith("libBLT")]

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
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
