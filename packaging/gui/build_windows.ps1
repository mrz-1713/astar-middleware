[CmdletBinding()]
param(
    [switch]$SkipTests,
    [string]$CertificateThumbprint = $env:ASTAR_SIGN_CERT_THUMBPRINT,
    [switch]$RequireSignature
)

# Builds AstarEapGui.exe as a portable folder + zip. Unlike the simulator
# packages there is no Inno Setup installer: the middleware GUI is unzipped
# onto the server by the admin who already owns C:\SECSGEM_EAP.

$ErrorActionPreference = "Stop"
$PackageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $PackageDir "..\..")).Path
$BuildRoot = Join-Path $RepoRoot ".build\gui"
$VenvPython = Join-Path $BuildRoot "venv\Scripts\python.exe"
$DistRoot = Join-Path $BuildRoot "dist"
$WorkRoot = Join-Path $BuildRoot "work"
$AppDir = Join-Path $DistRoot "AstarEapGui"
$ArtifactDir = Join-Path $RepoRoot "artifacts\gui"
$Version = "1.0.0"
$ZipPath = Join-Path $ArtifactDir "AstarEapGui-$Version-win-x64.zip"
$ChecksumPath = "$ZipPath.sha256"

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

# tkinter ships with the python.org installer but not with every distribution
# (Microsoft Store Python, some conda builds), and its absence only shows up
# at runtime inside the packaged exe. Fail here instead.
& $VenvPython -c "import tkinter"
if ($LASTEXITCODE -ne 0) {
    throw "The build Python has no tkinter. Install Python 3.11 from python.org (Tcl/Tk option enabled)."
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
        (Join-Path $PackageDir "AstarEapGui.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
}
finally {
    Pop-Location
}

Copy-Item (Join-Path $PackageDir "README_OPERATOR.md") (Join-Path $AppDir "README_OPERATOR.md")

@"
AstarEapGui=$Version
Python=3.11
secsgem=0.3.0
platform=windows-x64
"@ | Set-Content -Encoding ASCII (Join-Path $AppDir "VERSION.txt")

$AppExecutables = @(Get-ChildItem $AppDir -Filter "*.exe" -File)
if (-not $AppExecutables) { throw "GUI package contains no executable." }
& (Join-Path $RepoRoot "packaging\sign_artifact.ps1") `
    -Path $AppExecutables.FullName -CertificateThumbprint $CertificateThumbprint `
    -RequireSignature:$RequireSignature

Remove-Item -Force -ErrorAction SilentlyContinue $ZipPath, $ChecksumPath
Compress-Archive -Path $AppDir -DestinationPath $ZipPath -CompressionLevel Optimal
$Hash = (Get-FileHash -Algorithm SHA256 $ZipPath).Hash.ToLowerInvariant()
"$Hash  $(Split-Path -Leaf $ZipPath)" | Set-Content -Encoding ASCII $ChecksumPath

Write-Host "Built portable package: $ZipPath"
Write-Host "SHA256: $Hash"
