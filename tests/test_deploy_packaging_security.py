import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_release_lock_hashes_every_offline_wheel_exactly_once():
    lock = (ROOT / "requirements-release.lock").read_text(encoding="utf-8")
    hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", lock)
    wheels = sorted((ROOT / "deploy" / "wheels").glob("*.whl"))

    assert len(hashes) == len(wheels)
    actual = {
        hashlib.sha256(path.read_bytes()).hexdigest() for path in wheels
    }
    assert set(hashes) == actual
    assert "aiohttp" not in lock
    assert "cryptography==50.0.0" in lock


def test_build_requires_reviewed_public_config_and_emits_hashes():
    script = (ROOT / "scripts" / "build_deploy_package.sh").read_text()

    assert 'diff --quiet HEAD -- config/production.yaml' in script
    assert "RELEASE_MANIFEST.sha256" in script
    # Hashing goes through the sha256 shim so the same script runs under
    # git-bash (sha256sum, no shasum) and macOS (shasum, no sha256sum); -b on
    # both keeps Windows text mode from hashing a CR-stripped copy.
    assert 'sha256sum -b "$@"' in script
    assert 'shasum -a 256 -b "$@"' in script
    assert 'sha256 "${MANIFEST_FILES[@]}"' in script
    assert 'sha256 "${PKG_NAME}.zip"' in script


def test_bash_manifest_covers_every_wheel_and_source_file():
    """install.ps1 pip-installs straight from wheels/ (no per-package check
    of its own) and copies source/ verbatim into the running app, so the
    manifest it verifies before either happens must cover both trees, not
    just the three bootstrap files. Regression: MANIFEST_FILES used to stop
    at SETUP.bat/Setup.ps1/install.ps1/PYTHON_VERSION.txt/python/*.exe,
    leaving every third-party wheel and the entire application source
    uncovered in this project's own air-gapped USB/network-share
    distribution model."""
    script = (ROOT / "scripts" / "build_deploy_package.sh").read_text()

    manifest_section = script[script.index("Writing release hash manifest"):]
    assert 'find wheels -type f -print0' in manifest_section
    assert 'find source -type f -print0' in manifest_section
    # Both loops must feed the same array the final hash call reads.
    assert 'MANIFEST_FILES+=("${f#./}")' in manifest_section
    hash_call = 'sha256 "${MANIFEST_FILES[@]}"'
    assert manifest_section.index("find wheels") < manifest_section.index(hash_call)
    assert manifest_section.index("find source") < manifest_section.index(hash_call)


def test_powershell_manifest_covers_every_wheel_and_source_file():
    """Same coverage gap, same fix, in the local one-command Windows stager
    (build_installer.ps1 builds RELEASE_MANIFEST.sha256 independently of the
    bash stager - see test_installer_stages_locally_in_one_command)."""
    script = (
        ROOT / "packaging" / "installer" / "build_installer.ps1"
    ).read_text(encoding="utf-8")

    manifest_at = script.index("$manifestLines = foreach")
    manifest_section = script[max(0, manifest_at - 1200): manifest_at + 600]
    assert 'Get-ChildItem (Join-Path $stage "wheels") -Recurse -File' in manifest_section
    assert 'Get-ChildItem (Join-Path $stage "source") -Recurse -File' in manifest_section
    assert "$treeManifestFiles" in manifest_section
    assert manifest_section.index("$treeManifestFiles = ") < manifest_section.index(
        "+ $treeManifestFiles"
    )


def test_windows_installer_verifies_manifest_before_python_install():
    script = (ROOT / "deploy" / "install.ps1").read_text()

    verify_at = script.index(
        "Assert-ReleaseManifest",
        script.index('Step "Verifying release manifest"'),
    )
    python_install_at = script.index("Start-Process -FilePath")
    assert verify_at < python_install_at
    assert "Get-FileHash -Path $candidate -Algorithm SHA256" in script
    assert "SHA-256 verification failed" in script
    assert 'Get-Command python.exe -ErrorAction Stop' in script
    assert "Run anyway" not in script
    assert "--require-hashes -r requirements-release.lock" in script


def test_windows_installer_derives_paths_and_validates_firewall_rules():
    script = (ROOT / "deploy" / "install.ps1").read_text()

    assert '$RepoDir = if ($RepoDir) { $RepoDir } else {' in script
    assert 'Join-Path $InstallDir "app"' in script
    assert 'Get-NetFirewallRule -Name $rule.Name' in script
    assert "Get-NetFirewallPortFilter" in script
    assert "Remove-NetFirewallRule -Name $rule.Name" in script
    assert "AstarSecsGemEap-Hsms-Out" in script


def test_release_docs_require_trusted_hash_before_unblock():
    quickstart = (ROOT / "docs" / "QUICKSTART_WIN11.md").read_text()
    deployment = (
        ROOT / "docs" / "MAC_TO_WINDOWS11_FULL_DEPLOYMENT_GUIDE.md"
    ).read_text()

    assert "trusted release record" in quickstart
    assert quickstart.index("trusted release record") < quickstart.index("Unblock-File")
    assert "trusted release record" in deployment
    assert deployment.index("Get-FileHash") < deployment.index("Unblock-File")


def test_gui_ships_end_to_end_from_stage_to_shortcut():
    """The GUI reaches the operator only if three separate files agree.

    build_deploy_package.sh must stage gui/, install.ps1 must copy it into the
    app dir, and start-gui.bat must launch it as a module (gui/app.py uses a
    relative import, so `python gui\\app.py` fails where `-m gui.app` works).
    Drop any one and the Start Menu shortcut dies silently on the fab server.
    """
    build = (ROOT / "scripts" / "build_deploy_package.sh").read_text()
    install = (ROOT / "deploy" / "install.ps1").read_text()
    launcher = (ROOT / "packaging" / "installer" / "start-gui.bat").read_text()

    assert '${REPO_DIR}/gui"' in build
    assert '"gui"' in install[install.index("$codeDirs = @("):]
    assert "-m gui.app" in launcher
    assert (ROOT / "gui" / "app.py").is_file()


def test_installer_runs_offline_setup_and_reports_its_exit_code():
    """Inno ignores a non-zero exit code unless asked, which would report a
    successful install after Python or pip actually failed."""
    iss = (ROOT / "packaging" / "installer" / "AstarMiddleware.iss").read_text()

    assert "install.ps1" in iss
    assert "ewWaitUntilTerminated" in iss
    assert "(ResultCode = 0)" in iss
    assert "if not RunOfflineInstall() then" in iss
    assert "OfflineInstallFailed := True" in iss
    assert "function GetCustomSetupExitCode(): Integer" in iss
    assert "Result := 100" in iss
    # Needs admin: install.ps1 installs Python for all users and adds firewall
    # rules. Bundled wheels and the Python installer are win_amd64 only.
    assert "PrivilegesRequired=admin" in iss
    assert "ArchitecturesAllowed=x64compatible" in iss
    # Operator config, logs and data must survive an uninstall. Only a real
    # section header counts here; the ';'-commented rationale does not.
    sections = [ln.strip() for ln in iss.splitlines() if ln.startswith("[")]
    assert "[UninstallDelete]" not in sections


def test_installer_stages_locally_in_one_command():
    """Packaging is local-only: one PowerShell script on the Windows machine
    stages the payload and wraps it. The bash stager runs under git-bash too,
    but an operator's Windows box is not required to have bash at all."""
    script = (
        ROOT / "packaging" / "installer" / "build_installer.ps1"
    ).read_text(encoding="utf-8")

    assert "function New-StagedPackage" in script
    # With no -PackageDir the script must stage for itself rather than telling
    # the operator to go run something else first. Naming the bash stager as an
    # optional input is fine; depending on it is not.
    assert "if (-not $PackageDir) {\n    $PackageDir = New-StagedPackage" in script
    assert "Run scripts/build_deploy_package.sh first" not in script
    # gui must be staged or the installed product has no control panel. This
    # list has to agree with $codeDirs in deploy/install.ps1.
    for staged in ("eap_middleware", "gateway", "gui", "config"):
        assert f'"{staged}"' in script, staged
    # The payload install.ps1 refuses to run without these.
    assert "RELEASE_MANIFEST.sha256" in script
    assert "PYTHON_VERSION.txt" in script
    # A short wheel set means the offline install fails on the target machine,
    # which is exactly the failure the operator cannot debug.
    assert "-lt 20" in script
    assert "*.local.yaml" in script, "an operator's live config must never ship"
    assert 'git -C $RepoRoot rev-parse --short=12 HEAD' in script
    assert '"/DAppBuildId=$BuildId"' in script
    assert '$Version-$BuildId-win-x64.exe' in script


def test_staged_dirs_match_what_install_ps1_copies():
    """Three lists must agree or the GUI silently disappears from the installed
    product: what the stager copies, what install.ps1 copies out of source\\,
    and what start-gui.bat launches."""
    stager = (
        ROOT / "packaging" / "installer" / "build_installer.ps1"
    ).read_text(encoding="utf-8")
    install = (ROOT / "deploy" / "install.ps1").read_text(encoding="utf-8")

    code_dirs = install[install.index("$codeDirs = @("):]
    code_dirs = code_dirs[: code_dirs.index(")")]
    for name in ("eap_middleware", "gateway", "gui"):
        assert f'"{name}"' in code_dirs, f"install.ps1 does not copy {name}"
        assert f'"{name}"' in stager, f"the stager does not stage {name}"


def test_simulator_never_installs_on_a_middleware_host():
    """An EAP host carries no equipment-side code.

    The ZIP carries the simulator so one drag-and-drop provisions both
    machines of an installation, but the boundary is enforced where it matters:
    install.ps1 copies it only when someone explicitly says this box is a
    test machine. The default role must stay middleware-only.
    """
    install = (ROOT / "deploy" / "install.ps1").read_text(encoding="utf-8")

    # The middleware set itself must never name the simulator.
    code_dirs = install[install.index("$codeDirs = @("):]
    code_dirs = code_dirs[: code_dirs.index(")")]
    assert '"simulator"' not in code_dirs
    assert '"simulator_gui"' not in code_dirs

    # Defaulting to anything else would silently put a simulator on a fab
    # server for every operator who runs install.ps1 with no arguments.
    assert '[string]$Role = "Middleware"' in install
    assert '[ValidateSet("Middleware", "Simulator", "Both")]' in install

    # Only the two explicit simulator-bearing roles may pull it in, and the
    # default branch must resolve to the middleware set alone.
    switch_body = install[install.index("$installDirs = switch ($Role) {"):]
    switch_body = switch_body[: switch_body.index("\n}")]
    assert '"Simulator" {' in switch_body
    assert '"Both"      {' in switch_body
    assert "default     { $codeDirs }" in switch_body
    for line in switch_body.splitlines():
        if "$simulatorDirs" in line and "=" not in line:
            assert ('"Simulator"' in line) or ('"Both"' in line), line

    # The Inno deliverable stays middleware-only; it has no role switch.
    stager = (
        ROOT / "packaging" / "installer" / "build_installer.ps1"
    ).read_text(encoding="utf-8")
    staged_list = stager[stager.index("foreach ($dir in @("):]
    assert '"simulator"' not in staged_list[: staged_list.index(")")]

    # The simulator keeps its own standalone installers.
    assert (ROOT / "packaging" / "secsgem_simulator" / "SecsGemSimulator.iss").is_file()
    assert (ROOT / "packaging" / "mg_simulator" / "MGSimulator.iss").is_file()


def test_simulator_source_reaches_the_second_machine():
    """The installation is two machines and one ZIP.

    Staging the simulator is what makes `-Role Simulator` possible at all;
    without it the second machine installs a role whose files are absent and
    fails at the first import, which is the bug this pins.
    """
    bash = (ROOT / "scripts" / "build_deploy_package.sh").read_text()

    assert 'cp -R "${REPO_DIR}/simulator"' in bash
    assert 'cp -R "${REPO_DIR}/simulator_gui"' in bash
    # simulator_gui imports both of these; a simulator-role install that
    # skipped them would import-error on launch.
    install = (ROOT / "deploy" / "install.ps1").read_text(encoding="utf-8")
    support = install[install.index("$simulatorSupportDirs = @("):]
    support = support[: support.index(")")]
    for needed in ("eap_middleware", "gateway", "output"):
        assert f'"{needed}"' in support, needed


def test_graphical_setup_is_the_double_click_entry_point():
    """Setup has to be reachable without a typed command.

    Three files have to agree or the operator is back in PowerShell:
    SETUP.bat must launch Setup.ps1 without changing machine policy, the
    build must stage both, and both must be hashed in the release manifest.
    """
    bat = (ROOT / "deploy" / "SETUP.bat").read_text(encoding="utf-8")
    setup = (ROOT / "deploy" / "Setup.ps1").read_text(encoding="utf-8")
    bash = (ROOT / "scripts" / "build_deploy_package.sh").read_text()

    # Process-scoped bypass only: never Set-ExecutionPolicy on the machine.
    assert "-ExecutionPolicy Bypass" in bat
    assert "Setup.ps1" in bat
    assert "Set-ExecutionPolicy" not in bat
    assert "Set-ExecutionPolicy" not in setup

    # Elevation is requested, not assumed, and only install.ps1 runs elevated.
    assert "-Verb RunAs" in setup
    assert "install.ps1" in setup

    assert 'cp "${REPO_DIR}/deploy/SETUP.bat"' in bash
    assert 'cp "${REPO_DIR}/deploy/Setup.ps1"' in bash
    assert 'MANIFEST_FILES=("SETUP.bat" "Setup.ps1" "install.ps1"' in bash


def test_setup_does_not_trust_lastexitcode_for_success():
    """The last native command install.ps1 runs is validate-config, whose
    non-zero exit it explicitly tolerates ("normal until you edit
    production.yaml"). Reading $LASTEXITCODE after it would report a failed
    install after a perfectly good one, so success comes from try/catch."""
    setup = (ROOT / "deploy" / "Setup.ps1").read_text(encoding="utf-8")
    install = (ROOT / "deploy" / "install.ps1").read_text(encoding="utf-8")

    # The tolerated failure that makes $LASTEXITCODE unusable.
    assert "validate-config reported errors" in install

    inner = setup[setup.index("$inner ="):]
    inner = inner[: inner.index("\n\n")]
    assert "$LASTEXITCODE" not in inner
    assert "exit 0" in inner and "exit 1" in inner
    assert "try {{" in inner and "catch {{" in inner
    # install.ps1 must actually throw on real failures for this to hold.
    assert '$ErrorActionPreference = "Stop"' in install


def test_python_path_is_resolved_before_it_is_split():
    """$PythonExe defaults to the bare string "python" and is only replaced
    with a full path when the bundled installer actually runs. On a machine
    that already had the right Python, Split-Path -Parent "python" returns ""
    and Join-Path rejects it - killing an install that had already copied
    every file, installed every wheel and added every firewall rule."""
    install = (ROOT / "deploy" / "install.ps1").read_text(encoding="utf-8")

    resolve_at = install.index("$PythonExe = (Get-Command $PythonExe")
    # Every Split-Path of that variable must come after the resolution.
    for offset, line in enumerate(install.splitlines()):
        if "Split-Path" in line and "$PythonExe" in line:
            assert install.index(line) > resolve_at, line

    # And the split result is still guarded, so this can never be fatal.
    assert 'if ($pythonHome) { Join-Path $pythonHome "pythonw.exe" }' in install
    # Same class, same guard, for the Start Menu location.
    assert 'if ($programs) { Join-Path $programs "ASTAR SECS-GEM" }' in install


def test_setup_log_is_written_in_one_encoding():
    """Tee-Object -FilePath writes UTF-16LE on Windows PowerShell 5.1 and
    takes no -Encoding. Teeing the run while appending the error as UTF-8
    made one file in two encodings, and Get-Content locked onto the UTF-16
    BOM - rendering the error, the only part worth reading, as CJK."""
    setup = (ROOT / "deploy" / "Setup.ps1").read_text(encoding="utf-8")

    inner = setup[setup.index("$inner ="):]
    inner = inner[: inner.index("\n\n")]
    assert "Tee-Object" not in inner
    assert inner.count("-Encoding UTF8") == 2, "both branches must agree"
    # The reader must not fall back to auto-detection either.
    assert "Get-Content $logPath -Encoding UTF8" in setup


def test_runtime_acl_uses_service_identity_and_narrow_operator_group():
    """Ordinary users must not write data consumed by the Windows service."""
    install = (ROOT / "deploy" / "install.ps1").read_text(encoding="utf-8")
    service = (ROOT / "scripts" / "install_service.ps1").read_text(
        encoding="utf-8"
    )

    assert '"ASTAR Operators"' in install
    assert "New-LocalGroup" in install
    assert "Add-LocalGroupMember" in install
    assert 'Join-Path $InstallDir "control"' in install
    assert 'Rights = "(OI)(CI)M"' in install
    assert 'Rights = "(OI)(CI)RX"' in install
    # The old dangerous grant is removed by locale-stable SID, never added.
    assert "*S-1-5-32-545" in install
    assert '/remove:g "*S-1-5-32-545"' in install
    assert '/grant "*S-1-5-32-545' not in install
    assert "Assert-NoBuiltinUsersWrite" in install

    assert '"NT SERVICE\\$ServiceName"' in service
    assert 'sc.exe" config $ServiceName' in service
    assert '"obj=" $ServiceIdentity' in service
    assert '"${ServiceIdentity}:(OI)(CI)M"' in service
    assert "Assert-NoBuiltinUsersWrite" in service
    assert 'Assert-NssmValue "ObjectName" $ServiceIdentity' in service


def test_middleware_ci_enforces_release_quality_and_installer_execution():
    workflow = (ROOT / ".github" / "workflows" / "middleware.yml").read_text(
        encoding="utf-8"
    )

    assert "pyright eap_middleware gateway gui simulator simulator_gui" in workflow
    assert "pip-audit==2.10.1" in workflow
    assert "--format cyclonedx-json --output sbom.cdx.json" in workflow
    assert "-m slow tests/test_twenty_two_machines.py" in workflow
    assert "choco install innosetup" in workflow
    assert "build_installer.ps1" in workflow
    assert "expected custom inner-install failure exit 100" in workflow
    assert "compiled Setup EXE upgrade failed" in workflow


def test_service_install_is_idempotent_and_verifies_final_settings():
    service = (ROOT / "scripts" / "install_service.ps1").read_text(
        encoding="utf-8"
    )

    assert "Get-Service -Name $ServiceName" in service
    assert "if (-not $existingService)" in service
    install_at = service.index('Invoke-Nssm @("install"')
    conditional_at = service.index("if (-not $existingService)")
    assert conditional_at < install_at
    for setting in (
        "Application",
        "AppParameters",
        "AppDirectory",
        "ObjectName",
        "Start",
    ):
        assert f'Assert-NssmValue "{setting}"' in service


def test_installed_panels_get_shortcuts_for_their_role():
    """A control panel nobody can find is a control panel nobody uses.

    Both panels use relative imports, so a shortcut must run `-m package.app`;
    a shortcut targeting the .py file dies silently on the fab server.
    """
    install = (ROOT / "deploy" / "install.ps1").read_text(encoding="utf-8")

    assert "WScript.Shell" in install
    assert "CreateShortcut" in install
    assert "-m $module" in install
    assert 'New-PanelShortcut "ASTAR EAP Control" "gui.app"' in install
    assert 'New-PanelShortcut "ASTAR Simulator" "simulator_gui.app"' in install
    # Role-gated: a middleware host gets no simulator shortcut and vice versa.
    assert 'if ($Role -ne "Simulator") {' in install
    assert 'if ($Role -ne "Middleware") {' in install
    # A simulator listens, so its inbound port must be opened for it.
    assert "AstarSecsGemSimulator-Hsms-In" in install
    assert "-Direction Inbound" in install


def test_the_headless_service_never_loads_tkinter():
    """ScrollableTab lives in eap_middleware because that is the only
    package both install roles get - a middleware host has gui/ and no
    simulator_gui/, a test machine has the reverse. It imports tkinter at
    module scope, so eap_middleware/__init__ must not pull it in: a Windows
    service host has no display and need not have Tk installed at all."""
    import subprocess
    import sys

    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys, eap_middleware, eap_middleware.service;"
         "print('tkinter' in sys.modules)"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "False", "the service pulled in tkinter"

    init = (ROOT / "eap_middleware" / "__init__.py").read_text(encoding="utf-8")
    assert "tkwidgets" not in init


def test_service_imports_without_the_simulator_installed():
    """The whole separation rests on this. `simulator` must not be imported at
    module scope in service.py, or the middleware becomes unimportable on a
    middleware-only install and every machine dies, not just simulated ones."""
    import ast

    pkg = ROOT / "eap_middleware" / "service"
    for path in sorted(pkg.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:  # module scope only; nested imports are the fix
            # node.level == 0 keeps this to absolute imports: a relative
            # `from .simulator_runtime import ...` is this package's own module.
            if (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and (node.module or "").startswith("simulator")
            ):
                raise AssertionError(
                    f"{path.name} line {node.lineno} imports {node.module} at "
                    "module scope; move it into _start_simulator"
                )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("simulator"), alias.name


def test_missing_simulator_degrades_to_one_machine():
    """A middleware-only install running `runtime_mode: simulated` must fail
    that one machine with an actionable message, not crash the service."""
    from eap_middleware.service import (
        SIMULATOR_MISSING_HINT,
        SimulatorUnavailableError,
    )

    assert issubclass(SimulatorUnavailableError, RuntimeError)
    # The message has to say what to do, not just what broke.
    assert "SecsGemSimulator" in SIMULATOR_MISSING_HINT
    assert "runtime_mode" in SIMULATOR_MISSING_HINT

    service = (
        ROOT / "eap_middleware" / "service" / "simulator_runtime.py"
    ).read_text(encoding="utf-8")
    # Both call sites already wrap _start_simulator and mark that endpoint
    # Error; this pins that the raise is reachable from inside that guard.
    assert "raise SimulatorUnavailableError" in service
    start = service.index("def _start_simulator")
    assert "except ImportError" in service[start : start + 1500]
