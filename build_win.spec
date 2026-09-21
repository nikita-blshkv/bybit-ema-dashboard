# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

block_cipher = None
PROJECT_ROOT = Path.cwd()

a = Analysis(
    ["server.py"],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[
        (str(PROJECT_ROOT / "dashboard"), "dashboard"),
    ],
    hiddenimports=[
        "pandas",
        "numpy",
        "flask",
        "werkzeug",
        "requests",
        "core",
        "core.config",
        "core.data_store",
        "core.bybit_client",
        "core.live_engine",
        "core.trade_journal",
        "core.backtest_engine",
        "core.bybit_trade_client",
        "core.bybit_keys",
        "core.signal_engine",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "numba",
        "llvmlite",
        "pyarrow",
        "fastparquet",
        "sqlalchemy",
        "matplotlib",
        "IPython",
        "jedi",
        "parso",
        "nbformat",
        "jsonschema",
        "pytest",
        "PIL",
        "zmq",
        "tkinter",
        "_tkinter",
    ],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BybitEmaDashboard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="BybitEmaDashboard",
)
