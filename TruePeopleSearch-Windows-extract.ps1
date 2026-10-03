[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$PackagePath,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Destination,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-fA-F0-9]{64}$')]
    [string]$ExpectedPackageSha256,

    [string]$ChecksumPath,

    [string]$ExpectedAuthenticodeThumbprint
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
    throw "Release extraction failed: $Message"
}

if (-not (Test-Path -LiteralPath $PackagePath -PathType Leaf)) {
    Fail "package does not exist"
}

$package = (Resolve-Path -LiteralPath $PackagePath).Path
if ([IO.Path]::GetExtension($package) -notmatch '^\.zip$') {
    Fail "package must be a ZIP file"
}
if (-not $ChecksumPath) {
    $ChecksumPath = "$package.sha256"
}
if (-not (Test-Path -LiteralPath $ChecksumPath -PathType Leaf)) {
    Fail "checksum file is required"
}

$checksumRecord = (Get-Content -LiteralPath $ChecksumPath -Raw -Encoding ASCII).Trim()
$checksumFields = $checksumRecord -split '\s+', 2
$expectedHash = $checksumFields[0].ToUpperInvariant()
if ($expectedHash -notmatch '^[A-F0-9]{64}$') {
    Fail "checksum file does not contain a SHA-256 hash"
}
if ($checksumFields.Count -ne 2 -or $checksumFields[1].Trim().TrimStart('*') -ne (Split-Path -Leaf $package)) {
    Fail "checksum file does not identify this package"
}
$actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $package).Hash.ToUpperInvariant()
$trustedExpectedHash = $ExpectedPackageSha256.ToUpperInvariant()
if ($actualHash -ne $trustedExpectedHash) {
    Fail "package SHA-256 does not match the trusted expected value"
}
if ($actualHash -ne $expectedHash) {
    Fail "package SHA-256 does not match"
}
Write-Host "Package SHA-256 matched the trusted expected value and supplied checksum record."

$destinationPath = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $destinationPath) {
    Fail "destination must not already exist; this script never replaces a live release"
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead($package)
try {
    $archiveNames = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in $archive.Entries) {
        $name = $entry.FullName.Replace('\', '/')
        if (-not $name -or $name.StartsWith('/') -or $name -match '^[A-Za-z]:' -or $name -match '(^|/)\.\.(/|$)') {
            Fail "archive contains an unsafe path"
        }
        if (-not $archiveNames.Add($name)) {
            Fail "archive contains duplicate paths"
        }
    }
}
finally {
    $archive.Dispose()
}

$parent = Split-Path -Parent $destinationPath
if (-not $parent) {
    Fail "destination must have a parent directory"
}
New-Item -ItemType Directory -Path $parent -Force | Out-Null
$stage = Join-Path $parent ('.release-stage-' + [Guid]::NewGuid().ToString('N'))

try {
    Expand-Archive -LiteralPath $package -DestinationPath $stage -Force
    $verifier = Join-Path $stage 'deploy\windows\Verify-Release.ps1'
    if (-not (Test-Path -LiteralPath $verifier -PathType Leaf)) {
        Fail "extracted package is missing its verification script"
    }
    $verificationArguments = @{ ReleaseRoot = $stage }
    if ($PSBoundParameters.ContainsKey('ExpectedAuthenticodeThumbprint')) {
        $verificationArguments['ExpectedAuthenticodeThumbprint'] = $ExpectedAuthenticodeThumbprint
    }
    & $verifier @verificationArguments
    Move-Item -LiteralPath $stage -Destination $destinationPath -ErrorAction Stop
}
catch {
    if (Test-Path -LiteralPath $stage) {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
    throw
}

Write-Host "Package extracted and verified: $destinationPath"
Write-Host "No service, database, queue, browser runtime, or background job was started."
Write-Host "Keep the previous release untouched until customer acceptance succeeds."
