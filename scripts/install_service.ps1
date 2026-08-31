# Install or update the AstarSecsGemEapMiddleware Windows service via NSSM.
#
# Usage (PowerShell as Administrator):
#   scripts\install_service.ps1                           # auto-detect everything
#   scripts\install_service.ps1 -NssmExe C:\Tools\nssm\nssm.exe
#   scripts\install_service.ps1 -PythonExe C:\Python311\python.exe

param(
    [string]$PythonExe   = "",
    [string]$RepoDir     = "",
    [string]$ConfigPath  = "",
    [string]$ServiceName = "AstarSecsGemEapMiddleware",
    [string]$NssmExe     = "nssm.exe"
)

$ErrorActionPreference = "Stop"

# Auto-detect Python if not supplied
if (-not $PythonExe) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    $PythonExe = if ($pythonCommand) { $pythonCommand.Source } else { "" }
    if (-not $PythonExe) {
        throw "Python not found on PATH. Install Python 3.11 or pass -PythonExe."
    }
}

# Auto-detect repo dir as the parent of the scripts\ folder
if (-not $RepoDir) {
    $RepoDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}

if (-not $ConfigPath) {
    $ConfigPath = Join-Path $RepoDir "config\production.yaml"
}

$LogDir = "C:\SECSGEM_EAP\logs"
$ServiceIdentity = "NT SERVICE\$ServiceName"
$serviceArgs = "-m eap_middleware run-service --config `"$ConfigPath`""

function Invoke-Nssm {
    param([string[]]$Arguments, [string]$Description)
    & $NssmExe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed (nssm exit $LASTEXITCODE)."
    }
}

function Get-NssmValue {
    param([string]$Setting, [string]$Subsetting = "")
    $arguments = @("get", $ServiceName, $Setting)
    if ($Subsetting) { $arguments += $Subsetting }
    $value = & $NssmExe @arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "nssm get $Setting failed (exit $LASTEXITCODE): $value"
    }
    return "$value".Trim().Trim('"')
}

function Assert-NssmValue {
    param([string]$Setting, [string]$Expected, [string]$Subsetting = "")
    $actual = Get-NssmValue $Setting $Subsetting
    if ($actual -ne $Expected) {
        throw "Service setting $Setting verification failed: expected '$Expected', got '$actual'."
    }
}

function Invoke-Icacls {
    param([string]$Path, [string[]]$Arguments, [string]$Description)
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & icacls $Path @Arguments 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "$Description failed (icacls exit $LASTEXITCODE): $output"
        }
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

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
            throw "BUILTIN\Users still has write-capable access to $Path."
        }
    }
}

# Read only path settings without loading tokens or requiring an otherwise
# complete production configuration. Nothing secret is printed.
$pathProbe = @'
import json, os, sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
paths = raw.get("paths", {}) or {}
install = str(paths.get("install_dir") or r"C:\SECSGEM_EAP")
data = str(paths.get("data_dir") or os.path.join(install, "data"))
print(json.dumps({
    "install_dir": install,
    "log_dir": str(paths.get("log_dir") or os.path.join(install, "logs")),
    "data_dir": data,
    "control_dir": str(paths.get("control_dir") or os.path.join(os.path.dirname(data), "control")),
    "archive_dir": str(paths.get("archive_dir") or os.path.join(install, "archive")),
    "machines_dir": os.path.join(install, "machines"),
}))
'@
Push-Location $RepoDir
try {
    $pathJson = & $PythonExe -c $pathProbe $ConfigPath
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read runtime paths from $ConfigPath (exit $LASTEXITCODE)."
    }
    $runtimePaths = $pathJson | ConvertFrom-Json
} finally {
    Pop-Location
}
$LogDir = $runtimePaths.log_dir
foreach ($directory in @(
    $runtimePaths.log_dir,
    $runtimePaths.data_dir,
    $runtimePaths.control_dir,
    $runtimePaths.archive_dir,
    $runtimePaths.machines_dir
)) {
    if (-not (Test-Path $directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
}

Write-Host "Installing service '$ServiceName'..." -ForegroundColor Cyan
Write-Host "  Python  : $PythonExe"
Write-Host "  RepoDir : $RepoDir"
Write-Host "  Config  : $ConfigPath"
Write-Host "  NSSM    : $NssmExe"
Write-Host "  Identity: $ServiceIdentity"
Write-Host ""

$existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
$wasRunning = $existingService -and $existingService.Status -eq "Running"
if ($existingService -and $existingService.Status -ne "Stopped") {
    Stop-Service -Name $ServiceName -Force -ErrorAction Stop
    (Get-Service -Name $ServiceName).WaitForStatus("Stopped", "00:00:30")
}
if (-not $existingService) {
    Invoke-Nssm @("install", $ServiceName, $PythonExe, $serviceArgs) `
        "Service installation"
} else {
    Write-Host "Updating existing service '$ServiceName'..." -ForegroundColor Cyan
}

Invoke-Nssm @("set", $ServiceName, "Application", $PythonExe) "Application update"
Invoke-Nssm @("set", $ServiceName, "AppParameters", $serviceArgs) "Arguments update"
Invoke-Nssm @("set", $ServiceName, "AppDirectory", $RepoDir) "Working-directory update"
Invoke-Nssm @("set", $ServiceName, "AppStdout", "$LogDir\service_stdout.log") "stdout update"
Invoke-Nssm @("set", $ServiceName, "AppStderr", "$LogDir\service_stderr.log") "stderr update"
Invoke-Nssm @("set", $ServiceName, "AppRotateFiles", "1") "log rotation update"
Invoke-Nssm @("set", $ServiceName, "AppRotateBytes", "10485760") "log size update"
Invoke-Nssm @("set", $ServiceName, "Start", "SERVICE_AUTO_START") "startup update"
# Restart unexpected exits after five seconds. NSSM treats a deliberate SCM
# stop separately, so upgrades can quiesce without fighting the recovery loop.
Invoke-Nssm @("set", $ServiceName, "AppExit", "Default", "Restart") "exit policy update"
Invoke-Nssm @("set", $ServiceName, "AppRestartDelay", "5000") "restart delay update"

# NSSM's ObjectName command expects a password argument. Windows virtual
# service accounts are passwordless, so configure only the account name
# through the native service manager; SCM supplies the managed identity.
& "$env:SystemRoot\System32\sc.exe" config $ServiceName `
    "obj=" $ServiceIdentity
if ($LASTEXITCODE -ne 0) {
    throw "Setting virtual service identity failed (sc.exe exit $LASTEXITCODE)."
}

# Grant only the virtual service account write access to operational state.
# Application code and configuration are read-only to the service; the named
# operator group receives its separate config/control grants in install.ps1.
Invoke-Icacls $runtimePaths.install_dir `
    @("/grant", "${ServiceIdentity}:(M)", "/C", "/Q") `
    "Service lock-file ACL"
Invoke-Icacls $RepoDir `
    @("/grant", "${ServiceIdentity}:(OI)(CI)RX", "/T", "/C", "/Q") `
    "Service application read ACL"
foreach ($directory in @(
    $runtimePaths.log_dir,
    $runtimePaths.data_dir,
    $runtimePaths.control_dir,
    $runtimePaths.archive_dir,
    $runtimePaths.machines_dir
)) {
    Invoke-Icacls $directory `
        @("/remove:g", "*S-1-5-32-545", "/T", "/C", "/Q") `
        "Legacy BUILTIN\Users ACL removal"
    Invoke-Icacls $directory `
        @("/grant", "${ServiceIdentity}:(OI)(CI)M", "/T", "/C", "/Q") `
        "Service runtime ACL"
    Assert-NoBuiltinUsersWrite $directory
}

Assert-NssmValue "Application" $PythonExe
Assert-NssmValue "AppParameters" $serviceArgs
Assert-NssmValue "AppDirectory" $RepoDir
Assert-NssmValue "ObjectName" $ServiceIdentity
Assert-NssmValue "Start" "SERVICE_AUTO_START"
Assert-NssmValue "AppExit" "Restart" "Default"
Assert-NssmValue "AppRestartDelay" "5000"

if ($wasRunning) {
    Start-Service -Name $ServiceName -ErrorAction Stop
    (Get-Service -Name $ServiceName).WaitForStatus("Running", "00:00:30")
}

Write-Host ""
Write-Host "Service installed/updated and verified as $ServiceIdentity." -ForegroundColor Green
Write-Host "To start it now:"
Write-Host "    Start-Service $ServiceName"
Write-Host ""
Write-Host "To watch the log live:"
Write-Host "    Get-Content $LogDir\service_stderr.log -Tail 30 -Wait"
