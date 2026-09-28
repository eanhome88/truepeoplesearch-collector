[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ReleaseRoot,

    [switch]$AllowRuntimeState,

    [string]$ExpectedAuthenticodeThumbprint
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
    throw "Release verification failed: $Message"
}

function Resolve-ReleasePath([string]$Root, [string]$RelativePath) {
    if ([IO.Path]::IsPathRooted($RelativePath) -or
        $RelativePath -match '(^|[\\/])\.\.([\\/]|$)') {
        Fail "manifest contains an unsafe path"
    }
    $candidate = [IO.Path]::GetFullPath((Join-Path $Root $RelativePath))
    $rootWithSeparator = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $candidate.StartsWith($rootWithSeparator, [StringComparison]::OrdinalIgnoreCase)) {
        Fail "manifest path escapes the release root"
    }
    return $candidate
}

function Get-ReleaseRelativePath([string]$Root, [string]$FullPath) {
    $rootWithSeparator = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    $full = [IO.Path]::GetFullPath($FullPath)
    if (-not $full.StartsWith($rootWithSeparator, [StringComparison]::OrdinalIgnoreCase)) {
        Fail "file escapes the release root"
    }
    return $full.Substring($rootWithSeparator.Length).Replace('\', '/')
}

function Get-NormalizedAuthenticodeThumbprint([string]$Thumbprint) {
    if ([string]::IsNullOrWhiteSpace($Thumbprint) -or $Thumbprint -notmatch '^[a-fA-F0-9]{40}$') {
        Fail "expected Authenticode thumbprint must be exactly 40 hexadecimal characters"
    }
    return $Thumbprint.ToUpperInvariant()
}

function Assert-AuthenticodePublisher([string]$FilePath, [string]$RelativePath, [string]$ExpectedThumbprint) {
    try {
        $signature = Get-AuthenticodeSignature -LiteralPath $FilePath -ErrorAction Stop
    }
    catch {
        Fail "could not inspect Authenticode signature: $RelativePath"
    }
    if ([string]$signature.Status -ne 'Valid') {
        Fail "Authenticode signature is not valid: $RelativePath ($($signature.Status))"
    }
    if ($null -eq $signature.SignerCertificate) {
        Fail "Authenticode signature has no signer certificate: $RelativePath"
    }
    $actualThumbprint = ([string]$signature.SignerCertificate.Thumbprint).ToUpperInvariant()
    if ($actualThumbprint -notmatch '^[A-F0-9]{40}$' -or $actualThumbprint -ne $ExpectedThumbprint) {
        Fail "Authenticode publisher thumbprint does not match: $RelativePath"
    }
}

$root = (Resolve-Path -LiteralPath $ReleaseRoot).Path
$manifestPath = Join-Path $root 'release-manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    Fail "release-manifest.json is missing"
}

$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.schema_version -ne 1 -or -not $manifest.version) {
    Fail "manifest schema or version is invalid"
}
if ($null -eq $manifest.PSObject.Properties['source_dirty'] -or [bool]$manifest.source_dirty) {
    Fail "manifest was produced from a dirty source tree"
}
if ([string]$manifest.commit -notmatch '^[a-fA-F0-9]{40}$') {
    Fail "manifest commit is invalid"
}
if ($null -eq $manifest.PSObject.Properties['release_files'] -or $null -eq $manifest.release_files) {
    Fail "manifest is missing release_files"
}
$releaseFiles = @($manifest.release_files)
if ($releaseFiles.Count -eq 0) {
    Fail "manifest is missing release_files"
}

$forbiddenRoots = @('.git', '.venv', 'venv', 'data', 'logs', '__pycache__')
if (-not $AllowRuntimeState) {
    foreach ($rootName in $forbiddenRoots) {
        if (Test-Path -LiteralPath (Join-Path $root $rootName)) {
            Fail "forbidden runtime directory present: $rootName"
        }
    }
}

$expectedPaths = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
[void]$expectedPaths.Add('release-manifest.json')
$verifiedCount = 0
foreach ($entry in $releaseFiles) {
    $relativePath = [string]$entry.path
    $expectedHash = ([string]$entry.sha256).ToUpperInvariant()
    if (-not $relativePath -or $expectedHash -notmatch '^[A-F0-9]{64}$' -or $null -eq $entry.size) {
        Fail "manifest contains an invalid file record"
    }
    try {
        $expectedSize = [Int64]$entry.size
    }
    catch {
        Fail "manifest contains an invalid file size"
    }
    if ($expectedSize -lt 0 -or -not $expectedPaths.Add($relativePath.Replace('\', '/'))) {
        Fail "manifest contains a duplicate or invalid file record"
    }
    $filePath = Resolve-ReleasePath $root $relativePath
    if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
        Fail "missing packaged file: $relativePath"
    }
    $item = Get-Item -LiteralPath $filePath -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        Fail "packaged file must not be a reparse point: $relativePath"
    }
    if ($item.Length -ne $expectedSize) {
        Fail "size mismatch: $relativePath"
    }
    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $filePath).Hash.ToUpperInvariant()
    if ($actualHash -ne $expectedHash) {
        Fail "checksum mismatch: $relativePath"
    }
    $verifiedCount++
}

$allowedRuntimeRoots = @('.venv', 'venv', 'data', 'logs')
foreach ($item in Get-ChildItem -LiteralPath $root -Recurse -Force) {
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        Fail "release contains a reparse point"
    }
    if (-not $item.PSIsContainer) {
        $relativePath = Get-ReleaseRelativePath $root $item.FullName
        if ($expectedPaths.Contains($relativePath)) {
            continue
        }
        $relativeParts = $relativePath -split '/'
        $topLevel = $relativeParts[0]
        # A verified release may subsequently gain only its explicit local
        # configuration, approved state roots, or Python bytecode caches.  A
        # reparse point was rejected above, so this exception cannot redirect
        # verification outside the release root.
        $isAllowedRuntimeFile = $AllowRuntimeState -and (
            $relativePath -eq '.env' -or
            $allowedRuntimeRoots -contains $topLevel -or
            $relativeParts -contains '__pycache__'
        )
        if ($isAllowedRuntimeFile) {
            continue
        }
        Fail "unexpected file outside the reviewed release manifest: $relativePath"
    }
}

$publisherVerificationRequested = $PSBoundParameters.ContainsKey('ExpectedAuthenticodeThumbprint')
$authenticatedLauncherCount = 0
if ($publisherVerificationRequested) {
    $expectedThumbprint = Get-NormalizedAuthenticodeThumbprint $ExpectedAuthenticodeThumbprint
    $requiredLaunchers = @(
        'TruePeopleSearch.exe',
        'TruePeopleSearch_后台无窗启动.exe',
        'TruePeopleSearch_停止.exe'
    )
    foreach ($launcher in $requiredLaunchers) {
        if (-not $expectedPaths.Contains($launcher)) {
            Fail "manifest is missing required Windows launcher: $launcher"
        }
        $launcherPath = Resolve-ReleasePath $root $launcher
        Assert-AuthenticodePublisher $launcherPath $launcher $expectedThumbprint
        $authenticatedLauncherCount++
    }
}

Write-Host "Release verification passed."
Write-Host "  Version: $($manifest.version)"
Write-Host "  Files:   $verifiedCount"
Write-Host "  Integrity: SHA-256 records matched the release manifest"
if ($publisherVerificationRequested) {
    Write-Host "  Authenticode publisher: $authenticatedLauncherCount Windows launchers matched the expected thumbprint"
}
else {
    Write-Host "  Publisher authentication: not requested (SHA-256 integrity does not establish publisher identity)"
}
if ($AllowRuntimeState) {
    Write-Host "  Runtime state: allowed outside the reviewed package manifest"
}
Write-Host "No service, database, queue, browser runtime, or background job was started."
