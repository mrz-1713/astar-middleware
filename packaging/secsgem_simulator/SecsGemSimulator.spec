# -*- mode: python ; coding: utf-8 -*-
#
# Builds two executables into one application folder:
#   SecsGemSimulator.exe  - console, runs a YAML headless (service/CI use)
#   AstarSimulatorGui.exe - windowed control panel that edits that YAML
#
# They share the same simulator code, so a link proven in the panel behaves
# identically when the console executable runs the saved file.

from pathlib import Path

from PyInstaller.utils.hooks import collect_all


PROJECT_ROOT = Path(SPECPATH).parent.parent
PACKAGE_DIR = PROJECT_ROOT / "packaging" / "secsgem_simulator"
SECSGEM_DATAS, SECSGEM_BINARIES, SECSGEM_HIDDEN = collect_all("secsgem")

# Reached through the profile registry, the role dispatch in
# simulator.runner, or lazily inside gateway.host - PyInstaller sees none
# of them by following imports.
SHARED_HIDDEN = SECSGEM_HIDDEN + [
    "eap_middleware.profiles",
    "eap_middleware.models",
    "gateway.identity",
    "gateway.secsgem_compat",
    "simulator.cli",
    "simulator.config",
    "simulator.runner",
    "simulator.profile_simulator",
    "simulator.secsgem_equipment",
    "simulator.nexgen_mg_simulator",
    "simulator.secs_data_types",
    # connection.role: host builds these instead of an equipment.
    "simulator.host_simulator",
    "gateway.host",
    "gateway.e40",
    "gateway.event_subscription",
]

# The profiles' default EventSubscription.json files are what a host role
# subscribes to; without them a packaged host connects and links nothing.
SHARED_DATAS = SECSGEM_DATAS + [
    (str(PROJECT_ROOT / "output" / "davinci200_mc4_hc1"), "output/davinci200_mc4_hc1"),
    (str(PROJECT_ROOT / "output" / "nexgen_mg_series"), "output/nexgen_mg_series"),
    (str(PROJECT_ROOT / "config" / "EventSubscription.json"), "config"),
]

console_analysis = Analysis(
    [str(PACKAGE_DIR / "entrypoint.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=SECSGEM_BINARIES,
    datas=SHARED_DATAS,
    hiddenimports=SHARED_HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tests"],
    noarchive=False,
    optimize=1,
)

gui_analysis = Analysis(
    [str(PACKAGE_DIR / "gui_entrypoint.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=SECSGEM_BINARIES,
    datas=SHARED_DATAS,
    hiddenimports=SHARED_HIDDEN + ["simulator_gui.app", "simulator_gui.model"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tests"],
    noarchive=False,
    optimize=1,
)

console_pyz = PYZ(console_analysis.pure)
gui_pyz = PYZ(gui_analysis.pure)

console_exe = EXE(
    console_pyz,
    console_analysis.scripts,
    [],
    exclude_binaries=True,
    name="SecsGemSimulator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="AstarSimulatorGui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Windowed: no console window behind the panel. A startup crash still
    # raises PyInstaller's traceback dialog, so failures are not silent.
    console=False,
    disable_windowed_traceback=False,
)

# One COLLECT for both: identical shared files (secsgem, the profile data,
# the Python runtime) are written once instead of duplicating the runtime
# in two folders.
coll = COLLECT(
    console_exe,
    console_analysis.binaries,
    console_analysis.datas,
    gui_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SecsGemSimulator",
)
