# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all


PROJECT_ROOT = Path(SPECPATH).parent.parent
SECSGEM_DATAS, SECSGEM_BINARIES, SECSGEM_HIDDEN = collect_all("secsgem")

a = Analysis(
    [str(PROJECT_ROOT / "packaging" / "mg_simulator" / "entrypoint.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=SECSGEM_BINARIES,
    datas=SECSGEM_DATAS,
    hiddenimports=SECSGEM_HIDDEN + [
        "eap_middleware.profiles",
        "gateway.identity",
        # gateway.host is imported lazily inside simulator mains (to avoid a
        # circular-import window), so PyInstaller cannot see it - but the
        # standalone mains read DEFAULT_HSMS_TIMERS from it at runtime.
        "gateway.host",
        "gateway.e40",
        "gateway.secsgem_compat",
        "simulator.equipment",
        "simulator.nexgen_mg_simulator",
        "simulator.secs_data_types",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tests"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MGSimulator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MGSimulator",
)
