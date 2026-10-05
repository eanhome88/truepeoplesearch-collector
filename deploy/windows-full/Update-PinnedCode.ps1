[CmdletBinding()]
param(
    [string]$InstallRoot = 'D:\truepeoplesearch',
    [Parameter(Mandatory = $true)][string]$SourceRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-Sha256Upper([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$SourceRoot = [IO.Path]::GetFullPath($SourceRoot)
$app = Join-Path $InstallRoot 'app'
$manifestPath = Join-Path $InstallRoot 'bundle-manifest.json'
$deploy = Join-Path $app 'deploy\windows-full'
if (-not (Test-Path -LiteralPath $manifestPath) -or -not (Test-Path -LiteralPath $deploy)) {
    throw "Install root is not a collector bundle: $InstallRoot"
}

$pairs = @(
    @{
        ManifestPath = 'app/scripts/distributed_worker.py'
        Source = Join-Path $SourceRoot 'scripts\distributed_worker.py'
        Dest = Join-Path $app 'scripts\distributed_worker.py'
    },
    @{
        ManifestPath = 'app/scripts/scrape_to_tidb.py'
        Source = Join-Path $SourceRoot 'scripts\scrape_to_tidb.py'
        Dest = Join-Path $app 'scripts\scrape_to_tidb.py'
    }
)
foreach ($pair in $pairs) {
    if (-not (Test-Path -LiteralPath $pair.Source)) {
        throw "Source file missing: $($pair.Source)"
    }
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = Join-Path $InstallRoot ("backup\pinned-" + $stamp)
New-Item -ItemType Directory -Path $backup -Force | Out-Null
Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $backup 'bundle-manifest.json') -Force
foreach ($pair in $pairs) {
    if (Test-Path -LiteralPath $pair.Dest) {
        $name = Split-Path -Leaf $pair.Dest
        Copy-Item -LiteralPath $pair.Dest -Destination (Join-Path $backup $name) -Force
    }
}

$stop = Join-Path $deploy 'Stop-Collector.ps1'
if (Test-Path -LiteralPath $stop) {
    & $stop -Confirm:$false
}

foreach ($pair in $pairs) {
    Copy-Item -LiteralPath $pair.Source -Destination $pair.Dest -Force
}

$beforeText = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8
$manifest = $beforeText | ConvertFrom-Json
if ($null -eq $manifest.files) {
    throw 'bundle-manifest.json has no files array.'
}
$before = @{}
foreach ($entry in @($manifest.files)) {
    $path = ([string]$entry.path).Replace('\', '/')
    $before[$path] = ('{0}|{1}' -f [int64]$entry.size, ([string]$entry.sha256).ToUpperInvariant())
}

foreach ($pair in $pairs) {
    $hash = Get-Sha256Upper $pair.Dest
    $size = [int64](Get-Item -LiteralPath $pair.Dest).Length
    $found = $false
    foreach ($entry in @($manifest.files)) {
        $path = ([string]$entry.path).Replace('\', '/')
        if ($path -eq $pair.ManifestPath) {
            $entry.size = $size
            $entry.sha256 = $hash.ToLowerInvariant()
            $found = $true
        }
    }
    if (-not $found) {
        throw "Manifest has no entry for $($pair.ManifestPath)"
    }
}

$json = $manifest | ConvertTo-Json -Depth 12
[IO.File]::WriteAllText($manifestPath, $json)
$after = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($entry in @($after.files)) {
    $path = ([string]$entry.path).Replace('\', '/')
    $now = ('{0}|{1}' -f [int64]$entry.size, ([string]$entry.sha256).ToUpperInvariant())
    $isTarget = $false
    foreach ($pair in $pairs) {
        if ($pair.ManifestPath -eq $path) { $isTarget = $true }
    }
    if ($isTarget) {
        $file = $null
        foreach ($pair in $pairs) {
            if ($pair.ManifestPath -eq $path) { $file = $pair.Dest }
        }
        $expect = ('{0}|{1}' -f (Get-Item -LiteralPath $file).Length, (Get-Sha256Upper $file))
        if ($now -ne $expect) {
            throw "Manifest entry does not match the copied file: $path"
        }
    } elseif ($before.ContainsKey($path) -and $before[$path] -ne $now) {
        Copy-Item -LiteralPath (Join-Path $backup 'bundle-manifest.json') -Destination $manifestPath -Force
        foreach ($pair in $pairs) {
            $name = Split-Path -Leaf $pair.Dest
            $saved = Join-Path $backup $name
            if (Test-Path -LiteralPath $saved) {
                Copy-Item -LiteralPath $saved -Destination $pair.Dest -Force
            }
        }
        throw "Refusing to keep manifest because an unrelated entry changed: $path"
    }
}

function Restore-PinnedBackup {
    Copy-Item -LiteralPath (Join-Path $backup 'bundle-manifest.json') -Destination $manifestPath -Force
    foreach ($pair in $pairs) {
        $saved = Join-Path $backup (Split-Path -Leaf $pair.Dest)
        if (Test-Path -LiteralPath $saved) {
            Copy-Item -LiteralPath $saved -Destination $pair.Dest -Force
        }
    }
}

$bundle = Join-Path $deploy 'Test-FullBundle.ps1'
try {
    & $bundle -BundleRoot $InstallRoot -AllowRuntimeState
} catch {
    Restore-PinnedBackup
    throw
}

$repair = Join-Path $deploy 'Repair-RuntimeBinding.ps1'
& $repair -Confirm:$false
$start = Join-Path $deploy 'Start-Collector.ps1'
& $start
Write-Host 'PINNED_CODE_STARTED'
