[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [string[]]$Path,
    [string]$CertificateThumbprint = $env:ASTAR_SIGN_CERT_THUMBPRINT,
    [switch]$RequireSignature,
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"
if (-not $CertificateThumbprint) {
    if ($RequireSignature) {
        throw "Artifact signing is required but ASTAR_SIGN_CERT_THUMBPRINT is not configured."
    }
    Write-Warning "Signing skipped; artifacts are not production-approvable."
    exit 0
}
$signtool = @(
    (Get-Command signtool.exe -ErrorAction SilentlyContinue).Source,
    (Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin" `
        -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
        Where-Object FullName -Match 'x64' |
        Sort-Object FullName -Descending |
        Select-Object -First 1 -ExpandProperty FullName)
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $signtool) { throw "signtool.exe is required for Authenticode signing." }

foreach ($artifact in $Path) {
    & $signtool sign /sha1 $CertificateThumbprint /fd SHA256 /tr $TimestampUrl `
        /td SHA256 /v $artifact
    if ($LASTEXITCODE -ne 0) { throw "Signing failed for $artifact." }
    & $signtool verify /pa /all /v $artifact
    if ($LASTEXITCODE -ne 0) { throw "Signature verification failed for $artifact." }
    $signature = Get-AuthenticodeSignature -LiteralPath $artifact
    if ($signature.Status -ne "Valid") {
        throw "Authenticode status for $artifact is $($signature.Status)."
    }
}
