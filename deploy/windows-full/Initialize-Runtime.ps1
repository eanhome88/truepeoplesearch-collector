[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string]$InstallRoot = 'D:\TruePeopleSearch'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

$root = Get-NormalizedInstallRoot $InstallRoot
Assert-TpsSupportedWindowsHost
if (-not (Test-Path -LiteralPath 'D:\' -PathType Container)) {
    throw 'Drive D: is not available. No C: fallback is permitted.'
}
Assert-NoReparsePoint 'D:\'
$appRoot = Get-TpsAppRoot $root
& (Join-Path $appRoot 'deploy\windows-full\Test-FullBundle.ps1') -BundleRoot $root -AllowRuntimeState

try {
    Assert-TpsProtectedDirectoryAcl $root
}
catch {
    if (Get-ChildItem -LiteralPath (Join-Path $root 'runtime\docker-data') -Recurse -File -Filter '*.vhdx' -ErrorAction SilentlyContinue) {
        throw 'The install-root ACL is not approved and Docker data already exists. Stop Docker Desktop before repairing the D-drive ACL.'
    }
    Protect-TpsInstallTree $root
}

$directories = @(
    $root,
    (Join-Path $root 'app'),
    (Join-Path $root 'backups'),
    (Join-Path $root 'config'),
    (Join-Path $root 'data'),
    (Join-Path $root 'data\mysql'),
    (Join-Path $root 'data\redis'),
    (Join-Path $root 'logs'),
    (Join-Path $root 'logs\app'),
    (Join-Path $root 'runtime'),
    (Join-Path $root 'runtime\docker-data'),
    (Join-Path $root 'runtime\ms-playwright'),
    (Join-Path $root 'runtime\temp'),
    (Join-Path $root 'vendor')
)

foreach ($directory in $directories) {
    if (-not (Test-Path -LiteralPath $directory)) {
        if ($PSCmdlet.ShouldProcess($directory, 'Create D-drive runtime directory')) {
            New-Item -ItemType Directory -Path $directory -Force | Out-Null
        }
    }
    Assert-NoReparsePoint $directory
    Assert-TpsProtectedDirectoryAcl $directory
}
Assert-TpsBusinessStorageOnD $root

$environmentPath = Join-Path $root 'config\runtime.env'
if (Test-Path -LiteralPath $environmentPath) {
    Protect-TpsSecretFile $environmentPath
    [void](Read-TpsRuntimeEnvironment $environmentPath $root)
    Write-Host 'Existing runtime.env passed structural validation; secrets were not changed.'
    Write-Host "Runtime root: $root"
    exit 0
}

$composeRoot = Convert-ToComposePath $root
$browserRoot = $composeRoot + '/runtime/ms-playwright'
$lines = @(
    '# Generated locally. Never commit, upload, or place this file in a release archive.',
    "TPS_INSTALL_ROOT=$composeRoot",
    'TPS_DB_HOST=127.0.0.1',
    'TPS_DB_PORT=3306',
    'TPS_DB_NAME=people_search',
    'TPS_DB_USER=tps_app',
    ('TPS_DB_PASSWORD=' + (New-CryptographicHexSecret)),
    ('TPS_MYSQL_ROOT_PASSWORD=' + (New-CryptographicHexSecret)),
    'TPS_REDIS_HOST=127.0.0.1',
    'TPS_REDIS_PORT=6379',
    ('TPS_REDIS_PASSWORD=' + (New-CryptographicHexSecret)),
    'TPS_DASHBOARD_PORT=5001',
    'TPS_TIMEZONE=Asia/Shanghai',
    "PLAYWRIGHT_BROWSERS_PATH=$browserRoot"
)

$temporaryPath = Join-Path (Split-Path -Parent $environmentPath) ('.runtime.env.' + [Guid]::NewGuid().ToString('N') + '.tmp')
if (-not $PSCmdlet.ShouldProcess($environmentPath, 'Create protected local runtime configuration')) {
    exit 0
}
try {
    [IO.File]::WriteAllText($temporaryPath, (($lines -join "`r`n") + "`r`n"), (New-Object Text.UTF8Encoding($false)))
    Protect-TpsSecretFile $temporaryPath
    Move-Item -LiteralPath $temporaryPath -Destination $environmentPath -Force
    $temporaryPath = $null
}
finally {
    if ($temporaryPath -and (Test-Path -LiteralPath $temporaryPath)) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
}

[void](Read-TpsRuntimeEnvironment $environmentPath $root)
Write-Host 'D-drive runtime layout initialized.'
Write-Host "Runtime root: $root"
Write-Host 'No package, container, database, queue, dashboard, or collector was started.'
