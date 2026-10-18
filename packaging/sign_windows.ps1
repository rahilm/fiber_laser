<#
.SYNOPSIS
  Sign the Windows one-folder build with Azure Trusted Signing (Artifact Signing) and signtool.

.DESCRIPTION
  Placeholder hook: a no-op (exit 0, with a message) unless the signing settings below are set.
  Signs mlaser.exe, mlaser-console.exe and every .dll/.pyd in the bundle that has no valid
  Authenticode signature yet (Qt's and Python's own binaries are usually signed already).

  Environment (CI: repository secrets / variables):
    AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET   service principal allowed to sign
                                                            ("Trusted Signing Certificate Profile Signer")
    TRUSTED_SIGNING_ENDPOINT   e.g. https://weu.codesigning.azure.net
    TRUSTED_SIGNING_ACCOUNT    the Trusted Signing account name
    TRUSTED_SIGNING_PROFILE    the certificate profile name
    TRUSTED_SIGNING_DLIB       optional: path to Azure.CodeSigning.Dlib.dll (NuGet package
                               Microsoft.Trusted.Signing.Client); searched under %USERPROFILE%\.nuget
    SIGNTOOL                   optional: path to signtool.exe (Windows SDK); searched otherwise

.EXAMPLE
  pwsh packaging/sign_windows.ps1 -Bundle packaging/build/dist/mlaser
#>
param(
  [string]$Bundle = "packaging/build/dist/mlaser",
  [string]$TimestampUrl = "http://timestamp.acs.microsoft.com"
)
$ErrorActionPreference = "Stop"

$required = "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
            "TRUSTED_SIGNING_ENDPOINT", "TRUSTED_SIGNING_ACCOUNT", "TRUSTED_SIGNING_PROFILE"
$missing = $required | Where-Object { -not [Environment]::GetEnvironmentVariable($_) }
if ($missing) {
  Write-Host "sign_windows: signing skipped (not configured: $($missing -join ', ')). The build is unsigned."
  exit 0
}
if (-not (Test-Path $Bundle)) { throw "bundle folder not found: $Bundle" }

$signtool = $env:SIGNTOOL
if (-not $signtool) {
  $signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
              Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $signtool) { throw "signtool.exe not found (install the Windows SDK or set SIGNTOOL)" }

$dlib = $env:TRUSTED_SIGNING_DLIB
if (-not $dlib) {
  $nuget = Join-Path $env:USERPROFILE ".nuget\packages\microsoft.trusted.signing.client"
  if (-not (Test-Path $nuget)) {
    Write-Host "sign_windows: installing Microsoft.Trusted.Signing.Client (NuGet)"
    nuget install Microsoft.Trusted.Signing.Client -OutputDirectory (Split-Path $nuget) -ExcludeVersion | Out-Null
    $nuget = Join-Path (Split-Path $nuget) "Microsoft.Trusted.Signing.Client"
  }
  $dlib = Get-ChildItem $nuget -Recurse -Filter Azure.CodeSigning.Dlib.dll |
          Where-Object { $_.FullName -match "x64" } | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $dlib) { throw "Azure.CodeSigning.Dlib.dll not found (set TRUSTED_SIGNING_DLIB)" }

$metadata = Join-Path ([IO.Path]::GetTempPath()) "mlaser-trusted-signing.json"
@{ Endpoint = $env:TRUSTED_SIGNING_ENDPOINT; CodeSigningAccountName = $env:TRUSTED_SIGNING_ACCOUNT;
   CertificateProfileName = $env:TRUSTED_SIGNING_PROFILE } | ConvertTo-Json | Set-Content $metadata

$files = Get-ChildItem $Bundle -Recurse -Include *.exe, *.dll, *.pyd |
         Where-Object { $_.Extension -eq ".exe" -or (Get-AuthenticodeSignature $_.FullName).Status -ne "Valid" }
Write-Host "sign_windows: signing $($files.Count) files with $signtool"
# signtool takes many files per call; keep the command line short
$batch = @()
foreach ($f in $files) {
  $batch += $f.FullName
  if ($batch.Count -ge 50) {
    & $signtool sign /v /fd SHA256 /tr $TimestampUrl /td SHA256 /dlib $dlib /dmdf $metadata @batch
    if ($LASTEXITCODE) { throw "signtool failed ($LASTEXITCODE)" }
    $batch = @()
  }
}
if ($batch.Count) {
  & $signtool sign /v /fd SHA256 /tr $TimestampUrl /td SHA256 /dlib $dlib /dmdf $metadata @batch
  if ($LASTEXITCODE) { throw "signtool failed ($LASTEXITCODE)" }
}
foreach ($exe in Get-ChildItem $Bundle -Filter *.exe) {
  & $signtool verify /pa /v $exe.FullName | Out-Null
  if ($LASTEXITCODE) { throw "signature check failed: $($exe.Name)" }
}
Write-Host "sign_windows: done"
