[CmdletBinding()]
param(
    [switch]$SkipTests,
    [string]$CertificateThumbprint = $env:ASTAR_SIGN_CERT_THUMBPRINT,
    [switch]$RequireSignature
)

$ErrorActionPreference = "Stop"
$PackageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $PackageDir "..\..")).Path
$BuildRoot = Join-Path $RepoRoot ".build\secsgem-simulator"
$VenvPython = Join-Path $BuildRoot "venv\Scripts\python.exe"
$DistRoot = Join-Path $BuildRoot "dist"
$WorkRoot = Join-Path $BuildRoot "work"
$AppDir = Join-Path $DistRoot "SecsGemSimulator"
$ArtifactDir = Join-Path $RepoRoot "artifacts\secsgem-simulator"
$Version = "1.0.0"
$ZipPath = Join-Path $ArtifactDir "SecsGemSimulator-$Version-win-x64.zip"
$ChecksumPath = "$ZipPath.sha256"
$InstallerName = "SecsGemSimulator-Setup-$Version-win-x64.exe"
$InstallerPath = Join-Path $ArtifactDir $InstallerName
$InstallerChecksumPath = "$InstallerPath.sha256"

if ($env:OS -ne "Windows_NT") {
    throw "The Windows executable must be built on Windows."
}

New-Item -ItemType Directory -Force -Path $BuildRoot, $ArtifactDir | Out-Null
if (-not (Test-Path $VenvPython)) {
    $BootstrapPython = (Get-Command python -ErrorAction Stop).Source
    $BootstrapRuntime = & $BootstrapPython -c "import struct, sys; print('%d.%d|%d' % (sys.version_info.major, sys.version_info.minor, struct.calcsize('P') * 8))"
    if ($LASTEXITCODE -ne 0 -or $BootstrapRuntime -ne "3.11|64") {
        throw "Building requires 64-bit Python 3.11 on PATH; found $BootstrapRuntime."
    }
    & $BootstrapPython -m venv (Join-Path $BuildRoot "venv")
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python 3.11 virtual environment." }
}

$BuildRuntime = & $VenvPython -c "import struct, sys; print('%d.%d|%d' % (sys.version_info.major, sys.version_info.minor, struct.calcsize('P') * 8))"
if ($LASTEXITCODE -ne 0 -or $BuildRuntime -ne "3.11|64") {
    throw "The build environment must use 64-bit Python 3.11; found $BuildRuntime. Remove $BuildRoot and retry."
}

& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Could not upgrade pip." }
& $VenvPython -m pip install -r (Join-Path $RepoRoot "requirements-dev.txt")
if ($LASTEXITCODE -ne 0) { throw "Could not install runtime dependencies." }
& $VenvPython -m pip install -r (Join-Path $PackageDir "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "Could not install build dependencies." }

if (-not $SkipTests) {
    Push-Location $RepoRoot
    try {
        & $VenvPython -m pytest -q tests
        if ($LASTEXITCODE -ne 0) { throw "Test suite failed." }
    }
    finally {
        Pop-Location
    }
}

Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $DistRoot, $WorkRoot
Push-Location $RepoRoot
try {
    & $VenvPython -m PyInstaller `
        --noconfirm `
        --clean `
        --distpath $DistRoot `
        --workpath $WorkRoot `
        (Join-Path $PackageDir "SecsGemSimulator.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
}
finally {
    Pop-Location
}

$Assets = @(
    "simulator.yaml",
    "davinci-active.yaml",
    "davinci-passive.yaml",
    "host-example.yaml",
    "start-gui.bat",
    "start-active.bat",
    "start-passive.bat",
    "start-host.bat",
    "README_OPERATOR.md",
    "THIRD_PARTY_NOTICES.txt"
)
foreach ($Asset in $Assets) {
    Copy-Item (Join-Path $PackageDir $Asset) (Join-Path $AppDir $Asset)
}

$LicensePath = & $VenvPython -c @"
from importlib.metadata import distribution
d = distribution('secsgem')
for item in d.files or []:
    if str(item).replace('\\', '/').endswith('.dist-info/LICENSE'):
        print(d.locate_file(item))
        break
"@
if (-not $LicensePath -or -not (Test-Path $LicensePath)) {
    throw "Could not locate the installed secsgem license."
}
Copy-Item $LicensePath (Join-Path $AppDir "LICENSE-secsgem.txt")

$PythonLicense = & $VenvPython -c "import sys; from pathlib import Path; print(Path(sys.base_prefix) / 'LICENSE.txt')"
if ($LASTEXITCODE -ne 0) { throw "Could not locate the Python installation." }
if (Test-Path $PythonLicense) {
    Copy-Item $PythonLicense (Join-Path $AppDir "LICENSE-Python.txt")
}

$GuiExe = Join-Path $AppDir "AstarSimulatorGui.exe"
if (-not (Test-Path $GuiExe)) {
    throw "PyInstaller did not produce $GuiExe; the control panel would be missing from the package."
}

& (Join-Path $RepoRoot "packaging\sign_artifact.ps1") `
    -Path $GuiExe -CertificateThumbprint $CertificateThumbprint `
    -RequireSignature:$RequireSignature

@"
SecsGemSimulator=$Version
AstarSimulatorGui=$Version
Python=3.11
secsgem=0.3.0
platform=windows-x64
"@ | Set-Content -Encoding ASCII (Join-Path $AppDir "VERSION.txt")

Remove-Item -Force -ErrorAction SilentlyContinue `
    $ZipPath, `
    $ChecksumPath, `
    $InstallerPath, `
    $InstallerChecksumPath
Compress-Archive -Path $AppDir -DestinationPath $ZipPath -CompressionLevel Optimal
$Hash = (Get-FileHash -Algorithm SHA256 $ZipPath).Hash.ToLowerInvariant()
"$Hash  $(Split-Path -Leaf $ZipPath)" | Set-Content -Encoding ASCII $ChecksumPath

$IsccCandidates = @(
    (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source,
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
) | Where-Object { $_ -and (Test-Path $_) }
$IsccPath = $IsccCandidates | Select-Object -First 1
if (-not $IsccPath) {
    throw "Inno Setup 6 is required on the build machine. Install it from https://jrsoftware.org/isinfo.php and retry."
}

$InnoArguments = @(
    "/DAppVersion=$Version",
    "/DAppSource=$AppDir",
    "/DOutputDir=$ArtifactDir",
    (Join-Path $PackageDir "SecsGemSimulator.iss")
)
& $IsccPath @InnoArguments
if ($LASTEXITCODE -ne 0) { throw "Inno Setup build failed." }
if (-not (Test-Path $InstallerPath)) {
    throw "Inno Setup completed without producing $InstallerPath."
}

& (Join-Path $RepoRoot "packaging\sign_artifact.ps1") `
    -Path $InstallerPath -CertificateThumbprint $CertificateThumbprint `
    -RequireSignature:$RequireSignature

$InstallerHash = (Get-FileHash -Algorithm SHA256 $InstallerPath).Hash.ToLowerInvariant()
"$InstallerHash  $InstallerName" | Set-Content -Encoding ASCII $InstallerChecksumPath

Write-Host "Built portable package: $ZipPath"
Write-Host "Portable SHA256: $Hash"
Write-Host "Built recommended installer: $InstallerPath"
Write-Host "Installer SHA256: $InstallerHash"
