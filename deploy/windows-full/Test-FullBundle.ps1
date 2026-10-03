[CmdletBinding()]
param(
    [string]$BundleRoot = 'D:\TruePeopleSearch',
    [switch]$AllowRuntimeState
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

$root = Get-NormalizedInstallRoot $BundleRoot
Assert-NoReparsePoint $root
$manifestPath = Join-Path $root 'bundle-manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw 'bundle-manifest.json is missing.'
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.schema_version -ne 1 -or $manifest.target -ne 'windows-amd64' -or
    $manifest.source_dirty -ne $false -or $manifest.commit -notmatch '^[A-Fa-f0-9]{40}$') {
    throw 'The bundle manifest is invalid or was built from dirty source.'
}

$expected = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
[void]$expected.Add('bundle-manifest.json')
foreach ($entry in @($manifest.files)) {
    $relative = ([string]$entry.path).Replace('\', '/')
    if (-not $relative -or $relative.StartsWith('/') -or $relative -match ':' -or
        $relative -match '(^|/)\.\.(/|$)' -or -not $expected.Add($relative)) {
        throw 'The bundle manifest contains an unsafe or duplicate path.'
    }
    if ([string]$entry.sha256 -notmatch '^[A-Fa-f0-9]{64}$') {
        throw "Invalid SHA-256 record: $relative"
    }
    $candidate = [IO.Path]::GetFullPath((Join-Path $root $relative))
    $rootPrefix = $root.TrimEnd('\') + '\'
    if (-not $candidate.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Manifest path escapes the bundle: $relative"
    }
    $item = Get-Item -LiteralPath $candidate -Force -ErrorAction Stop
    if (-not ($item -is [IO.FileInfo]) -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Bundle entry is not a regular file: $relative"
    }
    if ([Int64]$entry.size -ne $item.Length) {
        throw "Bundle entry size mismatch: $relative"
    }
    if ((Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash -ne ([string]$entry.sha256).ToUpperInvariant()) {
        throw "Bundle entry checksum mismatch: $relative"
    }
    if ($entry.PSObject.Properties['authenticode_thumbprint']) {
        $thumbprint = ([string]$entry.authenticode_thumbprint).ToUpperInvariant()
        if ($thumbprint -notmatch '^[A-F0-9]{40}$') {
            throw "Invalid Authenticode thumbprint record: $relative"
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $candidate
        if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate -or
            $signature.SignerCertificate.Thumbprint.ToUpperInvariant() -ne $thumbprint) {
            throw "Authenticode publisher mismatch: $relative"
        }
    }
}

foreach ($tree in @('app', 'vendor')) {
    $treePath = Join-Path $root $tree
    foreach ($item in Get-ChildItem -LiteralPath $treePath -Recurse -Force) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "The bundle contains a reparse point: $($item.FullName)"
        }
        if (-not $item.PSIsContainer) {
            $relative = $item.FullName.Substring(($root.TrimEnd('\') + '\').Length).Replace('\', '/')
            if (-not $expected.Contains($relative)) {
                throw "Unexpected file outside the manifest: $relative"
            }
        }
    }
}

if (-not $AllowRuntimeState) {
    $allowedTopLevel = @('app', 'vendor', 'bundle-manifest.json')
    foreach ($item in Get-ChildItem -LiteralPath $root -Force) {
        if ($allowedTopLevel -notcontains $item.Name) {
            throw "Unexpected top-level entry in a pristine bundle: $($item.Name)"
        }
    }
}

Write-Host 'Full Windows bundle verification passed.'
Write-Host "Source commit: $($manifest.commit)"
Write-Host "Verified files: $(@($manifest.files).Count)"
Write-Host 'No installer, service, database, queue, dashboard, browser, or collector was started.'
