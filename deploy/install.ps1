# ASTAR SECS/GEM EAP Middleware - Windows 11 install script
#
# Usage (PowerShell as Administrator):
#   .\install.ps1                     # middleware (the EAP host)
#   .\install.ps1 -Role Simulator     # a test machine that pretends to be a tool
#   .\install.ps1 -Role Both          # both sides on one computer
#   .\install.ps1 -ConfigOnly         # reset config/dirs only, skip pip
#
# Idempotent: safe to re-run. Existing production.yaml is backed up, not overwritten.
#
# -Role decides what lands in the app directory. Middleware is the default and
# installs no equipment-side code: a production EAP host has no business
# carrying a simulator, and the two are meant to meet over HSMS/TCP exactly as
# the middleware meets real equipment. Choosing Simulator or Both is an
# explicit statement that this computer runs both sides.
#
param(
    [string]$InstallDir = "C:\SECSGEM_EAP",
    [string]$RepoDir,
    [string]$PythonExe = "python",
    [ValidateSet("Middleware", "Simulator", "Both")]
    [string]$Role = "Middleware",
    [int]$SimulatorPort = 5051,
    [switch]$ConfigOnly,
    [string]$ActiveRepoDir = "",
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$RepoDir = if ($RepoDir) { $RepoDir } else { Join-Path $InstallDir "app" }
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    [OK] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    [WARN] $msg" -ForegroundColor Yellow }

function Assert-NoBuiltinUsersWrite {
    param([string]$Path)
    $usersSid = "S-1-5-32-545"
    $writeRights = [System.Security.AccessControl.FileSystemRights]::Write -bor `
        [System.Security.AccessControl.FileSystemRights]::Modify -bor `
        [System.Security.AccessControl.FileSystemRights]::FullControl
    foreach ($ace in (Get-Acl $Path).Access) {
        try {
            $sid = $ace.IdentityReference.Translate(
                [System.Security.Principal.SecurityIdentifier]
            ).Value
        } catch { continue }
        if (
            $sid -eq $usersSid -and
            $ace.AccessControlType -eq "Allow" -and
            (($ace.FileSystemRights -band $writeRights) -ne 0)
        ) {
            throw "BUILTIN\Users has inherited or explicit write-capable access to $Path. Correct the parent ACL and retry."
        }
    }
}

function Assert-ReleaseManifest {
    $manifestPath = Join-Path $scriptDir "RELEASE_MANIFEST.sha256"
    if (-not (Test-Path $manifestPath -PathType Leaf)) {
        throw "RELEASE_MANIFEST.sha256 is missing; refusing to run an unverified package."
    }

    $packageRoot = [System.IO.Path]::GetFullPath($scriptDir + [System.IO.Path]::DirectorySeparatorChar)
    $verified = 0
    foreach ($line in Get-Content $manifestPath) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line -notmatch '^([0-9A-Fa-f]{64})\s+\*?(.+)$') {
            throw "Invalid release manifest entry: $line"
        }
        $expected = $Matches[1].ToUpperInvariant()
        $relative = $Matches[2].Trim().Replace('/', [System.IO.Path]::DirectorySeparatorChar)
        $candidate = [System.IO.Path]::GetFullPath((Join-Path $scriptDir $relative))
        if (-not $candidate.StartsWith($packageRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Release manifest path escapes the package: $relative"
        }
        if (-not (Test-Path $candidate -PathType Leaf)) {
            throw "Release manifest file is missing: $relative"
        }
        $actual = (Get-FileHash -Path $candidate -Algorithm SHA256).Hash.ToUpperInvariant()
        if ($actual -ne $expected) {
            throw "SHA-256 verification failed for $relative; refusing installation."
        }
        $verified++
    }
    if ($verified -lt 2) {
        throw "Release manifest is incomplete; refusing installation."
    }
    Ok "Verified $verified release files with SHA-256"
}

Step "Verifying release manifest"
Assert-ReleaseManifest

# 1) Install Python if missing or wrong version
$versionFile = Join-Path $scriptDir "PYTHON_VERSION.txt"
if (-not (Test-Path $versionFile -PathType Leaf)) {
    throw "PYTHON_VERSION.txt is missing; refusing installation."
}
$requiredVer = (Get-Content $versionFile).Trim()

Step "Checking Python $requiredVer"
$needInstall = $false
# A native command that writes to stderr under `2>&1` becomes a TERMINATING
# NativeCommandError when $ErrorActionPreference is "Stop" (the script default).
# Python's Store-alias stub, and any interpreter that prints a banner, do
# exactly that - so a machine that merely had the wrong Python aborted the
# whole install instead of installing the bundled one.
$previousPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    $pyShortVer = & $PythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>&1
    $pyExit = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $previousPreference
}
$pyShortVer = "$pyShortVer".Trim()
if ($pyExit -ne 0 -or $pyShortVer -ne $requiredVer) {
    if ($pyExit -ne 0) {
        Warn "Python not found (exit code $pyExit) - will install bundled version"
    } else {
        Warn "Found Python $pyShortVer, need $requiredVer - will install bundled version"
    }
    $needInstall = $true
} else {
    Ok "Python $pyShortVer already installed"
}

if ($needInstall) {
    # Look for bundled installer: deploy\python\python-*.exe
    $pythonDir = Join-Path $scriptDir "python"
    $installer  = Get-ChildItem $pythonDir -Filter "python-*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $installer) {
        throw "Python $requiredVer not found and no bundled installer in $pythonDir\. Either install Python $requiredVer manually from https://python.org/downloads/ (check 'Add to PATH') or add the installer to the deploy package."
    }
    Step "Installing Python from $($installer.Name)"
    # Silent install: all users, prepend to PATH, no test suite, no launcher
    $install = Start-Process -FilePath $installer.FullName `
        -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0 Include_launcher=0" `
        -Wait -PassThru
    if ($install.ExitCode -ne 0) {
        throw "Python installer exited with code $($install.ExitCode). The install cannot continue."
    }
    # Refresh PATH in this session so subsequent commands find the new Python
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("PATH", "User")
    $resolvedPython = Get-Command python.exe -ErrorAction Stop
    $PythonExe = $resolvedPython.Source
    Ok "Python installed"
}

# Confirm correct version is now available. Same stderr->terminating-error trap
# as the first check: a python.exe that prints anything to stderr must be
# treated as a wrong Python, not as a reason to abort the script outright.
$previousPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    $pyShortVer = & $PythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>&1
    $pyExit = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $previousPreference
}
$pyShortVer = "$pyShortVer".Trim()
if ($pyExit -ne 0 -or $pyShortVer -ne $requiredVer) {
    throw "Python $requiredVer required but got '$pyShortVer' (exit $pyExit) after install. Re-open PowerShell as Administrator and re-run."
}

# Resolve to a full path now, for every route through the checks above. When
# the bundled installer did not run - the normal case on a re-install, or on a
# machine that already had the right Python - $PythonExe is still the bare
# string "python". Split-Path -Parent on a bare command name returns "", which
# Join-Path rejects with a terminating error, and the shortcut step below then
# kills an install that had otherwise completely succeeded.
$PythonExe = (Get-Command $PythonExe -ErrorAction Stop).Source
Ok "Using $PythonExe"

# 2) Create directory structure
Step "Creating $InstallDir directory tree"
$dirs = @(
    "$InstallDir",
    "$InstallDir\app",
    "$InstallDir\logs",
    "$InstallDir\data",
    "$InstallDir\control",
    "$InstallDir\archive",
    "$InstallDir\machines"
)
foreach ($d in $dirs) {
    if (-not (Test-Path $d)) {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
        Ok "Created $d"
    }
}

# The service consumes configuration and file-based control commands, so no
# ordinary local account may write either. Operators get a named, auditable
# group and only the configuration/control permissions the panel requires.
# Queues, journals, archives and machine state remain service/admin-only.
Step "Establishing the ASTAR operator trust boundary"
$operatorsGroup = "ASTAR Operators"
$configDir = Join-Path $RepoDir "config"
$controlDir = Join-Path $InstallDir "control"
if (-not (Get-LocalGroup -Name $operatorsGroup -ErrorAction SilentlyContinue)) {
    New-LocalGroup -Name $operatorsGroup `
        -Description "Approved operators of ASTAR EAP config/control" |
        Out-Null
    Ok "Created local group '$operatorsGroup'"
}
$installerIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($installerIdentity -and $installerIdentity -notmatch '\\SYSTEM$') {
    try {
        Add-LocalGroupMember -Group $operatorsGroup -Member $installerIdentity `
            -ErrorAction Stop
        Ok "Added $installerIdentity to '$operatorsGroup'"
    } catch {
        if ($_.Exception.Message -notmatch "already a member") { throw }
        Ok "$installerIdentity is already in '$operatorsGroup'"
    }
}
foreach ($d in @($configDir, $controlDir)) {
    if (-not (Test-Path $d)) {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
    }
}

# Remove the dangerous grant from older releases before applying the narrower
# policy. S-1-5-32-545 is BUILTIN\Users by SID and therefore locale-safe.
#
# /remove:g only strips EXPLICIT ACEs - it cannot touch a write grant that
# $InstallDir merely inherits from its parent (e.g. a non-default ACL on the
# drive root). Break inheritance first so any such grant is frozen into an
# explicit ACE here, where /remove:g can actually reach it.
$previousPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    $breakInheritance = & icacls $InstallDir /inheritance:d /T /C /Q 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Could not break ACL inheritance on $InstallDir ($breakInheritance)"
    }
    $removeUsers = & icacls $InstallDir /remove:g "*S-1-5-32-545" /T /C /Q 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Could not remove legacy BUILTIN\Users grants ($removeUsers)"
    }
    foreach ($grant in @(
        @{ Path = $configDir; Rights = "(OI)(CI)M"; Label = "configuration modify" },
        @{ Path = $controlDir; Rights = "(OI)(CI)M"; Label = "control modify" },
        @{ Path = "$InstallDir\logs"; Rights = "(OI)(CI)RX"; Label = "log read" }
    )) {
        $result = & icacls $grant.Path /grant "${operatorsGroup}:$($grant.Rights)" `
            /T /C /Q 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "Could not grant $($grant.Label) to '$operatorsGroup' on $($grant.Path) ($result)"
        }
        Ok "${operatorsGroup}: $($grant.Label) on $($grant.Path)"
    }
} finally {
    $ErrorActionPreference = $previousPreference
}

# 3) Copy source code into RepoDir (overwrites code, not config or data)
$sourceDir = Join-Path $scriptDir "source"
if (-not (Test-Path $sourceDir)) {
    throw "source\ folder not found next to install.ps1. Make sure you extracted the full ZIP."
}

Step "Copying source to $RepoDir (role: $Role)"
# The middleware set. Deliberately free of equipment-side code: this is what a
# production EAP host gets, and it must stay that way.
$codeDirs = @("eap_middleware", "gateway", "gui", "scripts", "docs", "output")
# The simulator set. simulator_gui imports eap_middleware.profiles and gateway,
# so a simulator-only box still needs those two - it just gets no middleware
# control panel and never runs the service.
$simulatorDirs = @("simulator", "simulator_gui")
$simulatorSupportDirs = @("eap_middleware", "gateway", "scripts", "docs", "output")

$installDirs = switch ($Role) {
    "Simulator" { $simulatorSupportDirs + $simulatorDirs }
    "Both"      { $codeDirs + $simulatorDirs }
    default     { $codeDirs }
}
foreach ($d in $installDirs) {
    $src = Join-Path $sourceDir $d
    $dst = Join-Path $RepoDir $d
    if (Test-Path $src) {
        if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
        Copy-Item $src $dst -Recurse
        Ok "Copied $d"
    }
}
foreach ($f in @(
    "pyproject.toml", "requirements.txt", "requirements-release.lock", "README.md"
)) {
    $src = Join-Path $sourceDir $f
    if (Test-Path $src) { Copy-Item $src $RepoDir -Force }
}

# 4) Copy config templates.
# production.yaml is PRESERVED on re-install (it carries the operator's live
# machine IPs, ports, and device tokens). The new template ships alongside as
# production.yaml.new and a snapshot is kept as production.yaml.bak so changes
# can be merged by hand. Admin JSON files (AlarmConfig, EventSubscription, etc.)
# are user-data: also preserved, with new defaults dropped as *.new.
$cfgDst = Join-Path $RepoDir "config"
if (-not (Test-Path $cfgDst)) {
    New-Item -ItemType Directory -Path $cfgDst -Force | Out-Null
}
$cfgSrc = Join-Path $sourceDir "config"
foreach ($file in Get-ChildItem $cfgSrc -File) {
    $target = Join-Path $cfgDst $file.Name
    if ($file.Name -eq "production.yaml") {
        if (Test-Path $target) {
            # Preserve the operator's production.yaml on re-install - it holds
            # the live machine IPs, ports, and device tokens. Overwriting it
            # would silently reset them to the template placeholders. Ship the
            # new template alongside as production.yaml.new (+ a .bak snapshot)
            # so changes can be merged by hand.
            Copy-Item $target "$target.bak" -Force
            Copy-Item $file.FullName "$target.new" -Force
            Warn "config\production.yaml kept (your version). New template at production.yaml.new; snapshot at production.yaml.bak"
        } else {
            Copy-Item $file.FullName $target -Force
            Ok "Installed config\production.yaml"
        }
    } elseif (Test-Path $target) {
        Warn "config\$($file.Name) already exists - keeping your version. New default at $($file.Name).new"
        Copy-Item $file.FullName "$target.new" -Force
    } else {
        Copy-Item $file.FullName $target
        Ok "Installed default config\$($file.Name)"
    }
}

# Verify effective entries after files are copied. This also catches a
# dangerous write grant inherited from a non-standard parent directory; merely
# removing explicit legacy grants would miss that case.
foreach ($trustedPath in @(
    $cfgDst,
    $controlDir,
    "$InstallDir\logs",
    "$InstallDir\data",
    "$InstallDir\archive",
    "$InstallDir\machines"
)) {
    Assert-NoBuiltinUsersWrite $trustedPath
}
Ok "Verified that BUILTIN\Users has no write-capable runtime ACL"

if ($ConfigOnly) {
    Step "ConfigOnly mode - skipping pip install"
    Write-Host "`nDone. Edit $cfgDst\production.yaml and run 'python -m eap_middleware validate-config'"
    exit 0
}

# 5) Offline pip install from bundled wheels
$wheelsDir = Join-Path $scriptDir "wheels"
if (-not (Test-Path $wheelsDir)) {
    throw "wheels\ folder not found. Make sure the offline package is complete."
}
Step "Installing Python dependencies offline from wheels"
Push-Location $RepoDir
try {
    & $PythonExe -m pip install --no-index --find-links $wheelsDir `
        --require-hashes -r requirements-release.lock
    if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE) - see errors above" }
    Ok "Dependencies installed"
} finally {
    Pop-Location
}

# 6) Quick import smoke test - catch missing wheels early
Step "Smoke checking middleware imports"
$smokeScript = @'
import importlib, sys
mods = [
    "eap_middleware",
    "eap_middleware.service",
    "eap_middleware.config",
    "eap_middleware.profiles",
    "eap_middleware.secure_payload",
    "eap_middleware.job_tracker",
    "secsgem",
    "paho.mqtt.client",
    "cryptography",
    "yaml",
]
failed = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as e:
        failed.append((m, str(e)))
if failed:
    for m, err in failed:
        print(f"  FAIL: {m}: {err}")
    sys.exit(1)
print(f"  all {len(mods)} modules import OK")
'@
Push-Location $RepoDir
try {
    $smokeScript | & $PythonExe -
    if ($LASTEXITCODE -ne 0) { throw "Import smoke check failed - fix the errors above before continuing" }
    Ok "All imports OK"
} finally {
    Pop-Location
}

# 7) Windows Firewall: allow outbound TCP to HSMS tools (port 5000) and
#    Linkstuffs MQTT over TLS (port 8883). Rules are named so re-running is idempotent.
Step "Adding Windows Firewall rules for HSMS and Linkstuffs"
$fwRules = @(
    @{ Name = "AstarSecsGemEap-Hsms-Out"; DisplayName = "ASTAR SECS-GEM HSMS outbound"; Port = 5000; Desc = "HSMS port 5000 (adjust if tools use a different port)" },
    @{ Name = "AstarSecsGemEap-MqttTls-Out"; DisplayName = "ASTAR Linkstuffs MQTT TLS outbound"; Port = 8883; Desc = "Linkstuffs MQTT TLS (skip if using HTTPS-only mode)" }
)
foreach ($rule in $fwRules) {
    $existing = Get-NetFirewallRule -Name $rule.Name -ErrorAction SilentlyContinue
    $ruleMatches = $false
    if ($existing) {
        $portFilter = $existing | Get-NetFirewallPortFilter
        $protocolMatches = ($portFilter.Protocol -eq "TCP") -or ($portFilter.Protocol -eq 6)
        $ruleMatches = `
            ("$($existing.Enabled)" -eq "True") -and `
            ("$($existing.Direction)" -eq "Outbound") -and `
            ("$($existing.Action)" -eq "Allow") -and `
            $protocolMatches -and `
            ("$($portFilter.RemotePort)" -eq "$($rule.Port)")
        if (-not $ruleMatches) {
            Warn "Firewall rule '$($rule.Name)' is stale or mismatched - recreating it"
            Remove-NetFirewallRule -Name $rule.Name
        }
    }
    if (-not $ruleMatches) {
        New-NetFirewallRule -Name $rule.Name -DisplayName $rule.DisplayName `
            -Direction Outbound -Protocol TCP `
            -RemotePort $rule.Port -Action Allow -Enabled True -Profile Any | Out-Null
        Ok "Firewall: $($rule.Desc)"
    } else {
        Ok "Firewall rule '$($rule.Name)' already matches the required policy"
    }
}

# 7b) A simulator LISTENS, so it needs an inbound rule. Windows blocks the
#     port by default and the symptom on the other machine is a connection
#     timeout, which reads exactly like a wrong IP - the single most expensive
#     wrong turn on a two-machine installation.
if ($Role -ne "Middleware") {
    Step "Adding inbound firewall rule for the simulator (TCP $SimulatorPort)"
    $inName = "AstarSecsGemSimulator-Hsms-In"
    $existingIn = Get-NetFirewallRule -Name $inName -ErrorAction SilentlyContinue
    $inMatches = $false
    if ($existingIn) {
        $inFilter = $existingIn | Get-NetFirewallPortFilter
        $inProtocolMatches = ($inFilter.Protocol -eq "TCP") -or ($inFilter.Protocol -eq 6)
        $inMatches = `
            ("$($existingIn.Enabled)" -eq "True") -and `
            ("$($existingIn.Direction)" -eq "Inbound") -and `
            ("$($existingIn.Action)" -eq "Allow") -and `
            $inProtocolMatches -and `
            ("$($inFilter.LocalPort)" -eq "$SimulatorPort")
        if (-not $inMatches) {
            Warn "Firewall rule '$inName' is stale or mismatched - recreating it"
            Remove-NetFirewallRule -Name $inName
        }
    }
    if (-not $inMatches) {
        New-NetFirewallRule -Name $inName -DisplayName "ASTAR SECS-GEM simulator inbound" `
            -Direction Inbound -Protocol TCP `
            -LocalPort $SimulatorPort -Action Allow -Enabled True -Profile Any | Out-Null
        Ok "Firewall: inbound TCP $SimulatorPort for the simulator"
    } else {
        Ok "Firewall rule '$inName' already matches the required policy"
    }

    # Seed a runnable simulator.yaml so the panel opens on a working config
    # rather than an empty form. Loopback is safe by default; the panel can
    # expose a selected lab interface only with an explicit opt-in. Never
    # overwrite an operator's tuned file.
    $simConfig = Join-Path $RepoDir "simulator.yaml"
    if (Test-Path $simConfig) {
        Ok "simulator.yaml kept (your version)"
    } else {
        @"
connection:
  role: equipment
  mode: passive
  address: "127.0.0.1"
  allow_external_bind: false
  port: $SimulatorPort
  device_id: 0

simulation:
  profile: davinci_200_mc4_hc1
  tool_id: "SIM_01"
  wafer_count: 3
  event_interval_sec: 0.5
  repeat_lots: true
  emit_alarm: true
"@ | Set-Content -Encoding UTF8 $simConfig
        Ok "Created simulator.yaml (equipment, passive, 127.0.0.1:$SimulatorPort)"
    }
}

# 7c) Shortcuts. The whole point: nobody should have to remember a module
#     path or open PowerShell to reach a control panel. pythonw suppresses
#     the console window that would otherwise sit behind every panel.
Step "Creating Start Menu and desktop shortcuts"
$pythonHome = Split-Path -Parent $PythonExe
$pythonwExe = if ($pythonHome) { Join-Path $pythonHome "pythonw.exe" } else { "pythonw.exe" }
if (-not (Test-Path $pythonwExe)) { $pythonwExe = $PythonExe }
# Same failure class as the Python path above: GetFolderPath returns "" for a
# folder the OS has no definition for, and Join-Path turns that into a
# terminating error. A missing shortcut location is worth skipping, never
# worth failing a completed install over.
$desktop = [System.Environment]::GetFolderPath("CommonDesktopDirectory")
$programs = [System.Environment]::GetFolderPath("CommonPrograms")
$startMenu = if ($programs) { Join-Path $programs "ASTAR SECS-GEM" } else { "" }
if ($startMenu -and -not (Test-Path $startMenu)) {
    New-Item -ItemType Directory -Path $startMenu -Force | Out-Null
}

function New-PanelShortcut($name, $module, $argument) {
    $shell = New-Object -ComObject WScript.Shell
    foreach ($dir in @($desktop, $startMenu)) {
        if (-not $dir -or -not (Test-Path $dir)) { continue }
        $link = $shell.CreateShortcut((Join-Path $dir "$name.lnk"))
        $link.TargetPath = $pythonwExe
        # -m, not a file path: both panels use relative imports, so
        # `pythonw guipp.py` fails where `-m gui.app` works.
        $link.Arguments = "-m $module $argument"
        $link.WorkingDirectory = if ($ActiveRepoDir) { $ActiveRepoDir } else { $RepoDir }
        $link.Description = $name
        $link.Save()
    }
    Ok "Shortcut: $name"
}

if ($Role -ne "Simulator") {
    $shortcutRoot = if ($ActiveRepoDir) { $ActiveRepoDir } else { $RepoDir }
    New-PanelShortcut "ASTAR EAP Control" "gui.app" "--config `"$shortcutRoot\config\production.yaml`""
}
if ($Role -ne "Middleware") {
    $shortcutRoot = if ($ActiveRepoDir) { $ActiveRepoDir } else { $RepoDir }
    New-PanelShortcut "ASTAR Simulator" "simulator_gui.app" "--config `"$shortcutRoot\simulator.yaml`""
}

# 8) Validate the (possibly stub) production.yaml so user gets immediate
#    feedback about what they still need to edit.
Step "Validating config (informational)"
Push-Location $RepoDir
try {
    & $PythonExe -m eap_middleware validate-config --config "$cfgDst\production.yaml" 2>&1 | Tee-Object -Variable validateOutput
    if ($LASTEXITCODE -ne 0) {
        Warn "validate-config reported errors - normal until you edit production.yaml"
    }
} catch {
    Warn "validate-config crashed: $_"
} finally {
    Pop-Location
}

Write-Host "`n=================================================================" -ForegroundColor Green
Write-Host " Installation complete - role: $Role" -ForegroundColor Green
Write-Host "=================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Everything from here is done in the control panel. Shortcuts are on" -ForegroundColor White
Write-Host "the desktop and under Start > ASTAR SECS-GEM." -ForegroundColor White
Write-Host ""
if ($Role -ne "Middleware") {
    Write-Host "  ASTAR Simulator   - pick a machine profile, press Start." -ForegroundColor White
    Write-Host "                      The panel shows the address:port to enter" -ForegroundColor White
    Write-Host "                      on the middleware machine." -ForegroundColor White
    Write-Host ""
}
if ($Role -ne "Simulator") {
    Write-Host "  ASTAR EAP Control - set a machine's host/port, then press" -ForegroundColor White
    Write-Host "                      'Test connection'. It probes directly, so" -ForegroundColor White
    Write-Host "                      it works before any service is installed." -ForegroundColor White
    Write-Host ""
    Write-Host "  Run as a Windows service once the link is proven:" -ForegroundColor White
    Write-Host "       scripts\install_service.ps1"
    Write-Host ""
}

# Open the panel this machine is for. The operator asked to install it; the
# next thing they want is to look at it, not to find a shortcut. Notepad on a
# YAML file used to be that next step, which is what this replaces.
$panelModule = if ($Role -eq "Simulator") { "simulator_gui.app" } else { "gui.app" }
$panelArgs = if ($Role -eq "Simulator") {
    "--config `"$RepoDir\simulator.yaml`""
} else {
    "--config `"$cfgDst\production.yaml`""
}
if (-not $NoLaunch) {
    Start-Process -FilePath $pythonwExe -ArgumentList "-m $panelModule $panelArgs" -WorkingDirectory $RepoDir
}
