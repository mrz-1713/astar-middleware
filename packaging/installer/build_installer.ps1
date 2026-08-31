[CmdletBinding()]
param(
    # Reuse an already-staged package instead of staging a fresh one. Accepts
    # either a deploy_out\astar-middleware-deploy-* folder produced here or one
    # copied over from scripts/build_deploy_package.sh on another machine.
    [string]$PackageDir,
    [string]$Version = "1.0.0",
    [string]$BuildId = "",
    [string]$CertificateThumbprint = $env:ASTAR_SIGN_CERT_THUMBPRINT,
    [switch]$RequireSignature
)

# ONE command, fully local: stages the offline payload and wraps it into
# AstarMiddleware-Setup-<version>-win-x64.exe.
#
#     packaging\installer\build_installer.ps1
#
# Run it on the Windows machine. Nothing else is needed - no CI, no second
# machine, no bash. Inno Setup 6 must be installed (see the error below if it
# is not); everything else comes from this checkout.
#
# Staging is done here in PowerShell rather than by calling
# scripts/build_deploy_package.sh so that an operator's Windows machine needs no
# bash at all. (That script does run under git-bash - CI builds the release with
# it - but requiring bash here would put a second toolchain on the fab server.)
# The two stagings are deliberately not identical: this one skips the ZIP (Inno
# compresses the payload itself) and never downloads wheels (it reuses the
# vetted, version-matched set already tracked under deploy\).

$ErrorActionPreference = "Stop"
$InstallerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $InstallerDir "..\..")).Path
$ArtifactDir = Join-Path $RepoRoot "artifacts\installer"
if (-not $BuildId) {
    $BuildId = (& git -C $RepoRoot rev-parse --short=12 HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $BuildId) { $BuildId = "local" }
}
$BuildId = ($BuildId -replace '[^0-9A-Za-z._-]', '-').Trim('-')
if (-not $BuildId) { $BuildId = "local" }
$InstallerName = "AstarMiddleware-Setup-$Version-$BuildId-win-x64.exe"
$InstallerPath = Join-Path $ArtifactDir $InstallerName

if ($env:OS -ne "Windows_NT") {
    throw "Inno Setup's ISCC.exe only runs on Windows. Run this on the Windows machine; it stages the payload itself, so nothing needs to be prepared beforehand."
}

function New-StagedPackage {
    <#
      Lays out the payload install.ps1 expects:
        install.ps1  README_DEPLOY.txt  SETUP_CHECKLIST.txt  QUICKSTART.md
        PYTHON_VERSION.txt  RELEASE_MANIFEST.sha256
        python\python-*.exe   wheels\*.whl   source\<code>
    #>
    $stamp = Get-Date -Format "yyyy-MM-dd-HHmmss"
    $stage = Join-Path $RepoRoot "deploy_out\astar-middleware-deploy-$stamp"
    New-Item -ItemType Directory -Force -Path (Join-Path $stage "source") | Out-Null

    # Middleware only. The simulator is a SEPARATE deliverable with its own
    # installer (packaging\secsgem_simulator) and is deliberately absent here:
    # an EAP host has no reason to carry equipment-side code, and the two
    # connect over HSMS/TCP like real equipment does.
    #
    # gui must be here or the installed product has no control panel; the same
    # list lives in deploy\install.ps1 ($codeDirs) and the two must agree.
    foreach ($dir in @("eap_middleware", "gateway", "gui", "scripts", "config", "docs")) {
        Copy-Item (Join-Path $RepoRoot $dir) (Join-Path $stage "source\$dir") -Recurse -Force
    }
    # Only generated machine-profile data is needed at runtime; document renders
    # and screenshots under output\ do not belong in a deployment.
    foreach ($profile in @("davinci200_mc4_hc1", "nexgen_mg_series", "spts_fxp_omega")) {
        $src = Join-Path $RepoRoot "output\$profile"
        if (Test-Path $src) {
            Copy-Item $src (Join-Path $stage "source\output\$profile") -Recurse -Force
        }
    }
    foreach ($file in @(
        "pyproject.toml", "requirements.txt", "requirements-release.lock", "README.md"
    )) {
        Copy-Item (Join-Path $RepoRoot $file) (Join-Path $stage "source") -Force
    }
    # Never package an operator's live configuration.
    Get-ChildItem (Join-Path $stage "source\config") -Filter "*.local.yaml" -ErrorAction SilentlyContinue |
        Remove-Item -Force
    Get-ChildItem $stage -Recurse -Force -Include "__pycache__", ".DS_Store" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    Copy-Item (Join-Path $RepoRoot "deploy\install.ps1")         (Join-Path $stage "install.ps1") -Force
    Copy-Item (Join-Path $RepoRoot "deploy\upgrade.ps1")         (Join-Path $stage "upgrade.ps1") -Force
    Copy-Item (Join-Path $RepoRoot "deploy\README_DEPLOY.txt")   $stage -Force
    Copy-Item (Join-Path $RepoRoot "deploy\SETUP_CHECKLIST.txt") $stage -Force
    Copy-Item (Join-Path $RepoRoot "deploy\PYTHON_VERSION.txt")  $stage -Force
    Copy-Item (Join-Path $RepoRoot "docs\QUICKSTART_WIN11.md")   (Join-Path $stage "QUICKSTART.md") -Force

    Copy-Item (Join-Path $RepoRoot "deploy\wheels") (Join-Path $stage "wheels") -Recurse -Force
    $wheels = @(Get-ChildItem (Join-Path $stage "wheels") -Filter "*.whl")
    if ($wheels.Count -lt 20) {
        throw "Only $($wheels.Count) wheels found in deploy\wheels - the offline install would fail on the target machine."
    }

    # install.ps1 refuses to run unless the bundled interpreter matches the
    # version the wheels were built for, so ship only a matching installer.
    $requiredVersion = (Get-Content (Join-Path $stage "PYTHON_VERSION.txt")).Trim()
    $pythonInstallers = @(
        Get-ChildItem (Join-Path $RepoRoot "deploy\python") -Filter "python-$requiredVersion.*-amd64.exe" -ErrorAction SilentlyContinue
    )
    if ($pythonInstallers.Count -ne 1) {
        throw "Expected exactly one deploy\python\python-$requiredVersion.*-amd64.exe, found $($pythonInstallers.Count)."
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $stage "python") | Out-Null
    Copy-Item $pythonInstallers[0].FullName (Join-Path $stage "python") -Force

    # The manifest install.ps1 verifies before it executes anything. Paths are
    # forward-slashed and relative because that is the form it parses. Cover
    # every wheel (install.ps1 pip-installs straight from wheels\ with no
    # per-package check of its own) and every file under source\ (copied
    # verbatim into the running app) - not just the three bootstrap files -
    # so a corrupted or substituted file in an offline USB/network-share copy
    # is caught before anything installs, not after.
    $stageFull = (Resolve-Path $stage).Path.TrimEnd('\')
    $treeManifestFiles = @(
        Get-ChildItem (Join-Path $stage "wheels") -Recurse -File
        Get-ChildItem (Join-Path $stage "source") -Recurse -File
    ) | ForEach-Object { $_.FullName.Substring($stageFull.Length + 1).Replace('\', '/') }
    $manifestLines = foreach ($relative in (@(
        "install.ps1", "upgrade.ps1", "PYTHON_VERSION.txt", "python/$($pythonInstallers[0].Name)"
    ) + $treeManifestFiles)) {
        $hash = (Get-FileHash (Join-Path $stage ($relative -replace '/', '\')) -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $relative"
    }
    $manifestLines | Set-Content -Encoding ASCII (Join-Path $stage "RELEASE_MANIFEST.sha256")

    Write-Host "Staged $stage ($($wheels.Count) wheels, Python $requiredVersion)"
    return $stage
}

if (-not $PackageDir) {
    $PackageDir = New-StagedPackage
}
if (-not (Test-Path $PackageDir -PathType Container)) {
    throw "Staged package not found: $PackageDir"
}

# Without these the installer would ship a payload install.ps1 refuses to run,
# and the failure would only appear on the operator's server.
foreach ($required in @("install.ps1", "upgrade.ps1", "RELEASE_MANIFEST.sha256", "PYTHON_VERSION.txt", "source\gui", "wheels")) {
    if (-not (Test-Path (Join-Path $PackageDir $required))) {
        throw "Staged package is incomplete: $required is missing from $PackageDir"
    }
}
Write-Host "Packaging $PackageDir"

$IsccPath = @(
    (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source,
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $IsccPath) {
    throw "Inno Setup 6 is required on the build machine. Install it from https://jrsoftware.org/isinfo.php and retry."
}

New-Item -ItemType Directory -Force -Path $ArtifactDir | Out-Null
Remove-Item -Force -ErrorAction SilentlyContinue $InstallerPath, "$InstallerPath.sha256"

& $IsccPath `
    "/DAppVersion=$Version" `
    "/DAppBuildId=$BuildId" `
    "/DAppSource=$PackageDir" `
    "/DOutputDir=$ArtifactDir" `
    (Join-Path $InstallerDir "AstarMiddleware.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup build failed." }
if (-not (Test-Path $InstallerPath)) { throw "Inno Setup completed without producing $InstallerPath." }

& (Join-Path $RepoRoot "packaging\sign_artifact.ps1") `
    -Path $InstallerPath `
    -CertificateThumbprint $CertificateThumbprint `
    -RequireSignature:$RequireSignature

$Hash = (Get-FileHash -Algorithm SHA256 $InstallerPath).Hash.ToLowerInvariant()
"$Hash  $InstallerName" | Set-Content -Encoding ASCII "$InstallerPath.sha256"

Write-Host "Built installer: $InstallerPath"
Write-Host "SHA256: $Hash"
