# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all


PROJECT_ROOT = Path(SPECPATH).parent.parent
SECSGEM_DATAS, SECSGEM_BINARIES, SECSGEM_HIDDEN = collect_all("secsgem")

a = Analysis(
    [str(PROJECT_ROOT / "gui" / "app.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=SECSGEM_BINARIES,
    # Ship the template so a fresh install opens on a real config instead of
    # an empty window; gui.app looks for it under sys._MEIPASS.
    datas=SECSGEM_DATAS + [
        (str(PROJECT_ROOT / "config" / "production.yaml"), "config"),
    ],
    hiddenimports=SECSGEM_HIDDEN + [
        # Imported lazily at connect time, so PyInstaller cannot see them.
        "gateway.host",
        "gateway.e40",
        "gateway.event_subscription",
        "gateway.identity",
        "gateway.secsgem_compat",
        "paho.mqtt.client",
        # Reached through the profile registry / simulator factory.
        "eap_middleware.profiles",
        "simulator.secsgem_equipment",
        "simulator.profile_simulator",
        "simulator.nexgen_mg_simulator",
        "simulator.equipment",
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
    name="AstarEapGui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Windowed app: no console. A startup crash still raises the PyInstaller
    # traceback dialog, so failures are not silent.
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AstarEapGui",
)
