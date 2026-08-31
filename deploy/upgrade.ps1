# Stage, validate, atomically activate, and roll back an ASTAR Windows release.
param(
    [string]$InstallDir = "C:\SECSGEM_EAP",
    [string]$PythonExe = "python",
    [ValidateSet("Middleware", "Simulator", "Both")]
    [string]$Role = "Middleware",
    [string]$ServiceName = "AstarSecsGemEapMiddleware",
    [int]$HealthTimeoutSec = 60,
    [switch]$CiFailAfterSwitch
)

$ErrorActionPreference = "Stop"
$packageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$manifest = Join-Path $packageDir "RELEASE_MANIFEST.sha256"
if (-not (Test-Path $manifest -PathType Leaf)) {
    throw "Upgrade staging failed: RELEASE_MANIFEST.sha256 is missing."
}
$releaseId = (Get-FileHash $manifest -Algorithm SHA256).Hash.Substring(0, 16).ToLowerInvariant()
$releasesDir = Join-Path $InstallDir "releases"
$candidate = Join-Path $releasesDir "$releaseId.staging.$PID"
$releaseDir = Join-Path $releasesDir $releaseId
$appPointer = Join-Path $InstallDir "app"
$currentPointer = Join-Path $InstallDir "current"
$externalConfig = Join-Path $InstallDir "config"
$rollbackPointer = Join-Path $InstallDir "app.rollback"
$nextPointer = Join-Path $InstallDir "app.next"
$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
$wasRunning = $service -and $service.Status -eq "Running"
$switched = $false

function Invoke-NativeChecked([string]$Description, [scriptblock]$Command) {
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$Description failed (exit $LASTEXITCODE)." }
}

function New-Junction([string]$Link, [string]$Target) {
    if (Test-Path $Link) { throw "Refusing to replace existing pointer $Link" }
    Invoke-NativeChecked "junction creation" {
        & "$env:SystemRoot\System32\cmd.exe" /d /c mklink /J $Link $Target | Out-Null
    }
}

function Remove-CandidateSafely([string]$Path) {
    if (-not (Test-Path $Path)) { return }
    $configLink = Join-Path $Path "config"
    if (Test-Path $configLink) {
        $item = Get-Item $configLink -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            Remove-Item $configLink -Force
        }
    }
    Remove-Item $Path -Recurse -Force
}

try {
    New-Item -ItemType Directory -Path $releasesDir -Force | Out-Null
    New-Item -ItemType Directory -Path $candidate -Force | Out-Null

    # Preserve operator configuration before running the inner installer.
    if (-not (Test-Path $externalConfig)) {
        if (Test-Path (Join-Path $appPointer "config")) {
            Copy-Item (Join-Path $appPointer "config") $externalConfig -Recurse
        } else {
            New-Item -ItemType Directory -Path $externalConfig -Force | Out-Null
        }
    }

    $inner = Join-Path $packageDir "install.ps1"
    & $inner -InstallDir $InstallDir -RepoDir $candidate -PythonExe $PythonExe `
        -Role $Role -ActiveRepoDir $appPointer -NoLaunch
    if ($LASTEXITCODE -ne 0) { throw "Candidate installation failed (exit $LASTEXITCODE)." }

    # Configuration is runtime state, not release content. The inner installer
    # supplied first-install defaults; merge those into the external location,
    # then replace the candidate copy with a junction.
    $candidateConfig = Join-Path $candidate "config"
    if (-not (Get-ChildItem $externalConfig -Force -ErrorAction SilentlyContinue)) {
        Copy-Item (Join-Path $candidateConfig "*") $externalConfig -Recurse -Force
    }
    Invoke-NativeChecked "external configuration ACL" {
        & icacls $externalConfig /grant "ASTAR Operators:(OI)(CI)M" /T /C /Q
    }
    if (Test-Path $candidateConfig) { Remove-Item $candidateConfig -Recurse -Force }
    New-Junction $candidateConfig $externalConfig

    Push-Location $candidate
    try {
        & $PythonExe -c "import eap_middleware, gateway, gui"
        if ($LASTEXITCODE -ne 0) { throw "Candidate import probe failed." }
        & $PythonExe -m eap_middleware validate-config --config "$candidateConfig\production.yaml"
        if ($LASTEXITCODE -ne 0) { throw "Candidate configuration probe failed." }
    } finally { Pop-Location }

    if (Test-Path $releaseDir) {
        # Same manifest: retain the immutable release already installed.
        Remove-CandidateSafely $candidate
    } else {
        Move-Item $candidate $releaseDir
    }
    New-Junction $nextPointer $releaseDir

    if ($service -and $service.Status -ne "Stopped") {
        Stop-Service -Name $ServiceName -Force -ErrorAction Stop
        (Get-Service $ServiceName).WaitForStatus("Stopped", "00:00:30")
    }

    if (Test-Path $rollbackPointer) {
        throw "Rollback pointer already exists; refusing an ambiguous switch."
    }
    if (Test-Path $appPointer) { Rename-Item $appPointer $rollbackPointer }
    Rename-Item $nextPointer $appPointer
    $switched = $true

    if ($CiFailAfterSwitch) {
        if ($env:CI -ne "true") {
            throw "CiFailAfterSwitch is available only on a CI runner."
        }
        throw "Intentional post-switch health failure for rollback acceptance."
    }

    if ($service) {
        $healthStarted = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        Start-Service -Name $ServiceName -ErrorAction Stop
        (Get-Service $ServiceName).WaitForStatus("Running", "00:00:30")
        $statusPath = Join-Path $InstallDir "control\status.json"
        $deadline = [DateTime]::UtcNow.AddSeconds($HealthTimeoutSec)
        $healthy = $false
        while ([DateTime]::UtcNow -lt $deadline) {
            if ((Get-Service $ServiceName).Status -ne "Running") { break }
            if (Test-Path $statusPath) {
                try {
                    $status = Get-Content $statusPath -Raw | ConvertFrom-Json
                    if (
                        $status.updated_at -gt $healthStarted -and
                        $status.storage.state -ne "critical"
                    ) {
                        $healthy = $true
                        break
                    }
                } catch { }
            }
            Start-Sleep -Seconds 1
        }
        if (-not $healthy) { throw "Candidate health probe timed out." }
    }

    # Retain the previous release until explicit retention cleanup. A renamed
    # junction is safe to remove; a first-install physical app directory is
    # kept as app.previous.<timestamp> for manual recovery.
    if (Test-Path $rollbackPointer) {
        $attributes = (Get-Item $rollbackPointer -Force).Attributes
        if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            Remove-Item $rollbackPointer -Force
        } else {
            Rename-Item $rollbackPointer ("app.previous." + (Get-Date -Format "yyyyMMddHHmmss"))
        }
    }
    # Both published pointers move only once the candidate is proven healthy.
    # Publishing `current` before the health probe would leave it naming the
    # candidate after a probe failure rolled `app` back to the previous release.
    if (Test-Path $currentPointer) { Remove-Item $currentPointer -Force }
    New-Junction $currentPointer $releaseDir
    Set-Content -Path (Join-Path $InstallDir "ACTIVE_RELEASE") -Value $releaseId -Encoding ASCII
    Write-Host "Activated release $releaseId" -ForegroundColor Green
    exit 0
} catch {
    $failure = $_
    if ($switched -and (Test-Path $rollbackPointer)) {
        try {
            if ($service -and (Get-Service $ServiceName).Status -ne "Stopped") {
                Stop-Service $ServiceName -Force
                (Get-Service $ServiceName).WaitForStatus("Stopped", "00:00:30")
            }
            if (Test-Path $appPointer) { Rename-Item $appPointer $nextPointer }
            Rename-Item $rollbackPointer $appPointer
            if ($wasRunning) {
                Start-Service $ServiceName
                (Get-Service $ServiceName).WaitForStatus("Running", "00:00:30")
            }
        } catch {
            throw "Upgrade failed: $failure; rollback also failed: $_"
        }
    }
    throw "Upgrade failed and previous release was preserved: $failure"
} finally {
    Remove-CandidateSafely $candidate
    if (Test-Path $nextPointer) { Remove-Item $nextPointer -Force }
}
