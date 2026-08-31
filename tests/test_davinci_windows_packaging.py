from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "packaging" / "secsgem_simulator"


def test_windows_installer_is_self_contained_and_operator_friendly():
    installer = (PACKAGE_DIR / "SecsGemSimulator.iss").read_text(encoding="utf-8")
    build = (PACKAGE_DIR / "build_windows.ps1").read_text(encoding="utf-8")

    assert "PrivilegesRequired=lowest" in installer
    assert r"DefaultDirName={localappdata}\Programs\SecsGemSimulator" in installer
    assert r'Source: "{#AppSource}\*"' in installer
    assert "recursesubdirs" in installer
    assert "onlyifdoesntexist" in installer
    # Every shipped YAML holds the operator's own IPs and role choice, so
    # each one must be laid down once and survive an upgrade. Counting the
    # files rather than a fixed number keeps this honest when one is added.
    shipped_configs = sorted(
        path.name for path in PACKAGE_DIR.glob("*.yaml")
    )
    assert shipped_configs, "the package ships no configuration at all"
    for name in shipped_configs:
        assert (
            f'Source: "{{#AppSource}}\\{name}"; DestDir: "{{app}}"; '
            "Flags: onlyifdoesntexist uninsneveruninstall" in installer
        ), f"{name} would be overwritten on upgrade"
        assert name in installer.split("Excludes:", 1)[1].split(";", 1)[0], (
            f"{name} is not excluded from the bulk copy, so the bulk rule "
            "overwrites it before the preserve rule runs"
        )
    assert installer.count("onlyifdoesntexist uninsneveruninstall") == len(
        shipped_configs
    )

    # Shortcuts must name the SECS role, not just the HSMS direction: an
    # operator cannot tell a tool from an EAP by the word "passive".
    assert "Run as EQUIPMENT (listen, HSMS passive)" in installer
    assert "Run as EQUIPMENT (dial out, HSMS active)" in installer
    assert "Run as HOST (dial out, HSMS active)" in installer
    # The control panel is the primary entry point of the package.
    assert "AstarSimulatorGui.exe" in installer
    assert "AstarSimulatorGui.exe" in build

    assert "SecsGemSimulator-Setup-$Version-win-x64.exe" in build
    assert "ISCC.exe" in build
    assert "Get-FileHash -Algorithm SHA256 $InstallerPath" in build


def test_windows_ci_smoke_tests_the_installed_application():
    smoke = (PACKAGE_DIR / "smoke_installer.ps1").read_text(encoding="utf-8")
    workflow = (
        ROOT / ".github" / "workflows" / "secsgem-simulator-windows.yml"
    ).read_text(encoding="utf-8")

    assert "/VERYSILENT" in smoke
    assert "SecsGemSimulator.exe" in smoke
    assert "smoke_packaged_exe.py" in smoke
    assert "unins000.exe" in smoke
    assert "operator-edited-config-marker" in smoke
    assert "operator-retained.log" in smoke
    assert "Uninstall removed preserved operator data" in smoke
    assert "upgrade did not preserve" in smoke
    assert "smoke_installer.ps1" in workflow
    assert "SecsGemSimulator-Setup-1.0.0-win-x64.exe" in workflow
