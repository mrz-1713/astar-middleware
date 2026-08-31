[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Installer,
    [string]$Python = "python",
    [string]$InstallDir,
    [string]$OutputDir
)

$ErrorActionPreference = "Stop"
$PackageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $PackageDir "..\..")).Path
$InstallerPath = (Resolve-Path $Installer).Path

if (-not $InstallDir) {
    $InstallDir = Join-Path $RepoRoot ".build\secsgem-simulator\installer-smoke-app-$PID"
}
if (-not $OutputDir) {
    $OutputDir = Join-Path $RepoRoot ".build\secsgem-simulator\installer-smoke-logs"
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$InstallArguments = @(
    "/VERYSILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/SP-",
    "/DIR=`"$InstallDir`""
)
$InstallProcess = Start-Process `
    -FilePath $InstallerPath `
    -ArgumentList $InstallArguments `
    -Wait `
    -PassThru
if ($InstallProcess.ExitCode -ne 0) {
    throw "Installer exited with code $($InstallProcess.ExitCode)."
}

$AppExe = Join-Path $InstallDir "SecsGemSimulator.exe"
$Uninstaller = Join-Path $InstallDir "unins000.exe"
try {
    foreach ($RequiredPath in @(
        $AppExe,
        (Join-Path $InstallDir "davinci-active.yaml"),
        (Join-Path $InstallDir "davinci-passive.yaml"),
        (Join-Path $InstallDir "start-active.bat"),
        (Join-Path $InstallDir "start-passive.bat"),
        $Uninstaller
    )) {
        if (-not (Test-Path $RequiredPath -PathType Leaf)) {
            throw "Installed package is missing $RequiredPath."
        }
    }

    $PassiveConfig = Join-Path $InstallDir "davinci-passive.yaml"
    $UpgradeMarker = "# operator-edited-config-marker"
    Add-Content -Path $PassiveConfig -Value $UpgradeMarker -Encoding UTF8
    $UpgradeProcess = Start-Process `
        -FilePath $InstallerPath `
        -ArgumentList $InstallArguments `
        -Wait `
        -PassThru
    if ($UpgradeProcess.ExitCode -ne 0) {
        throw "Installer upgrade exited with code $($UpgradeProcess.ExitCode)."
    }
    if (-not (Get-Content $PassiveConfig -Raw).Contains($UpgradeMarker)) {
        throw "Installer upgrade did not preserve the operator-edited Passive configuration."
    }

    $RetainedLog = Join-Path $InstallDir "logs\operator-retained.log"
    Set-Content -Path $RetainedLog -Value "operator log retention check" -Encoding UTF8

    & $AppExe version
    if ($LASTEXITCODE -ne 0) { throw "Installed executable version check failed." }
    & $AppExe check-config --config (Join-Path $InstallDir "davinci-active.yaml")
    if ($LASTEXITCODE -ne 0) { throw "Installed Active configuration check failed." }
    & $AppExe check-config --config (Join-Path $InstallDir "davinci-passive.yaml")
    if ($LASTEXITCODE -ne 0) { throw "Installed Passive configuration check failed." }

    & $Python `
        (Join-Path $PackageDir "smoke_packaged_exe.py") `
        --exe $AppExe `
        --output-dir $OutputDir
    if ($LASTEXITCODE -ne 0) { throw "Installed executable smoke test failed." }
}
finally {
    if (Test-Path $Uninstaller -PathType Leaf) {
        $UninstallProcess = Start-Process `
            -FilePath $Uninstaller `
            -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART") `
            -Wait `
            -PassThru
        if ($UninstallProcess.ExitCode -ne 0) {
            throw "Uninstaller exited with code $($UninstallProcess.ExitCode)."
        }
    }
}

if (Test-Path $AppExe) {
    throw "Uninstall left the application executable behind at $AppExe."
}
if (-not (Test-Path $PassiveConfig -PathType Leaf) -or -not (Test-Path $RetainedLog -PathType Leaf)) {
    throw "Uninstall removed preserved operator data (configuration or logs)."
}

Write-Host "Installed DaVinci Simulator passed Active and Passive smoke tests; uninstall removed binaries and preserved operator data."
