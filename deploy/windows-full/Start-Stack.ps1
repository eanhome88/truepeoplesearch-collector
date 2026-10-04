[CmdletBinding()]
param(
    [string]$InstallRoot = 'D:\TruePeopleSearch',
    [int]$ReadyTimeoutSeconds = 120
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

if ($ReadyTimeoutSeconds -lt 10 -or $ReadyTimeoutSeconds -gt 600) {
    throw 'ReadyTimeoutSeconds must be between 10 and 600.'
}

$root = Get-NormalizedInstallRoot $InstallRoot
Assert-TpsSupportedWindowsHost
$null = Assert-TpsProtectedDirectoryAcl $root
$appRoot = Get-TpsAppRoot $root
$bundleVerifier = Join-Path $appRoot 'deploy\windows-full\Test-FullBundle.ps1'
& $bundleVerifier -BundleRoot $root -AllowRuntimeState
Assert-TpsRuntimeManifest $root
$python = Get-TpsPythonPath $root
$environmentPath = Join-Path $root 'config\runtime.env'
$configuration = Read-TpsRuntimeEnvironment $environmentPath $root
Set-TpsProcessEnvironment $configuration
Assert-TpsBusinessStorageOnD $root
Assert-TpsDockerStorageOnD $root
$dockerTools = Get-TpsDockerTools $root
$docker = $dockerTools.Docker
$composeExecutable = $dockerTools.Compose

$env:TPS_DASHBOARD_HOST = '127.0.0.1'
$env:TPS_ALERT_WEBHOOK = ''
$env:TPS_UPDATE_CHECK_URL = ''
$env:PROXY_TUNNEL = ''
$env:PROXY_API = ''
$env:PROXY_FILE = ''
$env:TPS_ALLOW_CLUSTER = ''
$env:TPS_CONCURRENCY = ''
$env:TPS_START_AREA = ''

$expectedDockerImages = Get-TpsVerifiedDockerImageIds $root
$compose = Get-TpsComposeArguments $root $appRoot
& $composeExecutable @compose up -d mysql redis
if ($LASTEXITCODE -ne 0) {
    throw 'MySQL/Redis startup failed. The collector and dashboard were not started.'
}

$deadline = [DateTime]::UtcNow.AddSeconds($ReadyTimeoutSeconds)
foreach ($container in @('tps-full-mysql', 'tps-full-redis')) {
    $healthy = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        $status = (& $docker --context desktop-linux inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' $container)
        if ($LASTEXITCODE -eq 0 -and ([string]$status).Trim() -eq 'healthy') {
            $healthy = $true
            break
        }
        Start-Sleep -Seconds 2
    }
    if (-not $healthy) {
        throw "Local dependency did not become healthy: $container"
    }
}
$containerImages = @{
    'tps-full-mysql' = 'tps-offline/mysql:8.4.11-amd64'
    'tps-full-redis' = 'tps-offline/redis:7.4.8-alpine-amd64'
}
foreach ($container in $containerImages.Keys) {
    $actualImage = ([string](& $docker --context desktop-linux inspect --format '{{.Image}}' $container)).Trim().ToLowerInvariant()
    $expectedImage = ([string]$expectedDockerImages[$containerImages[$container]]).ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $actualImage -ne $expectedImage) {
        throw "Running container does not use the verified offline image: $container"
    }
}

# Schema initialization uses the local-only root credential. The dashboard and
# collection tools receive only the application account afterward.
Set-TpsProcessEnvironment $configuration -IncludeServiceCredentials
$applicationUser = $configuration['TPS_DB_USER']
$applicationPassword = $configuration['TPS_DB_PASSWORD']
$env:TPS_DB_USER = 'root'
$env:TPS_DB_PASSWORD = $configuration['TPS_MYSQL_ROOT_PASSWORD']
try {
    & $python -I -B (Join-Path $appRoot 'deploy\init_db.py')
    if ($LASTEXITCODE -ne 0) {
        throw 'Database schema initialization failed.'
    }
}
finally {
    $env:TPS_DB_USER = $applicationUser
    $env:TPS_DB_PASSWORD = $applicationPassword
    # Never pass the local MySQL root credential to the dashboard process.
    [Environment]::SetEnvironmentVariable('TPS_MYSQL_ROOT_PASSWORD', $null, 'Process')
}

$dependencyVersionProbe = @'
import os
import mysql.connector
import redis

db = mysql.connector.connect(
    host=os.environ['TPS_DB_HOST'], port=int(os.environ['TPS_DB_PORT']),
    user=os.environ['TPS_DB_USER'], password=os.environ['TPS_DB_PASSWORD'],
    database=os.environ['TPS_DB_NAME'], connection_timeout=3,
)
cursor = db.cursor()
cursor.execute('SELECT VERSION()')
mysql_version = str(cursor.fetchone()[0]).split('-', 1)[0]
cursor.close()
db.close()
queue = redis.Redis(
    host=os.environ['TPS_REDIS_HOST'], port=int(os.environ['TPS_REDIS_PORT']),
    password=os.environ['TPS_REDIS_PASSWORD'],
    socket_connect_timeout=3, socket_timeout=3,
)
redis_version = str(queue.info('server').get('redis_version', ''))
queue.close()
raise SystemExit(0 if mysql_version == '8.4.11' and redis_version == '7.4.8' else 1)
'@
& $python -I -B -c $dependencyVersionProbe
if ($LASTEXITCODE -ne 0) {
    throw 'Running dependency versions do not match MySQL 8.4.11 and Redis 7.4.8.'
}

$recordPath = Join-Path $root 'runtime\dashboard-process.json'
$authPath = Join-Path $root 'runtime\dashboard-auth.json'
if (Test-Path -LiteralPath $recordPath -PathType Leaf) {
    Assert-TpsProtectedFileAcl $recordPath
    $existing = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($existing.schema_version -ne 1 -or [int]$existing.pid -le 0 -or
        [string]$existing.process_start_utc -notmatch '^\d{4}-\d{2}-\d{2}T' -or
        [IO.Path]::GetFullPath([string]$existing.executable) -ne [IO.Path]::GetFullPath($python) -or
        [IO.Path]::GetFullPath([string]$existing.application_root) -ne [IO.Path]::GetFullPath($appRoot) -or
        [string]$existing.release_mode -ne 'customer') {
        throw 'The prior dashboard process record is invalid; refusing to guess a process target.'
    }
    $existingMatches = $false
    try {
        $existingProcess = Get-Process -Id ([int]$existing.pid) -ErrorAction Stop
        $recordedStart = [DateTime]::Parse([string]$existing.process_start_utc).ToUniversalTime()
        $existingMatches = (
            [Math]::Abs(($existingProcess.StartTime.ToUniversalTime() - $recordedStart).TotalSeconds) -le 1 -and
            [IO.Path]::GetFullPath($existingProcess.Path) -eq [IO.Path]::GetFullPath([string]$existing.executable)
        )
    }
    catch [Microsoft.PowerShell.Commands.ProcessCommandException] {
        $existingMatches = $false
    }
    if ($existingMatches) {
        throw "A verified dashboard process is still running (PID $($existingProcess.Id))."
    }
    else {
        Remove-Item -LiteralPath $recordPath -Force
        Remove-Item -LiteralPath $authPath -Force -ErrorAction SilentlyContinue
    }
}

$env:TPS_RELEASE_MODE = 'customer'
$env:TPS_LOCAL_AUTH_REQUIRED = '1'
$launchToken = New-CryptographicHexSecret -Bytes 24
$env:TPS_RELEASE_LAUNCH_TOKEN = $launchToken
$port = Convert-ToApprovedPort $configuration['TPS_DASHBOARD_PORT'] 'TPS_DASHBOARD_PORT'
$stdoutLog = Join-Path $root 'logs\app\dashboard.out.log'
$stderrLog = Join-Path $root 'logs\app\dashboard.err.log'
$arguments = @(
    '-I', '-B', '-u', (Join-Path $appRoot 'tools\dashboard_api.py'),
    '--host', '127.0.0.1', '--port', [string]$port, '--strict-port'
)
$dashboard = $null
$launchedPid = 0
$launchedStartUtc = $null
$launchedExecutable = $null
try {
    try {
        $dashboard = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $appRoot -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru
    }
    finally {
        # The dashboard inherited its restricted application credentials.  Do
        # not leak those credentials to the browser association below.
        Clear-TpsServiceCredentialEnvironment
    }
    if ($null -eq $dashboard) {
        throw 'The dashboard process could not be started.'
    }
    $dashboard.Refresh()
    $launchedPid = $dashboard.Id
    $launchedStartUtc = $dashboard.StartTime.ToUniversalTime()
    $launchedExecutable = [IO.Path]::GetFullPath($dashboard.Path)
    if ($dashboard.HasExited -or
        $launchedExecutable -ne [IO.Path]::GetFullPath($python)) {
        throw 'The launched dashboard process identity could not be verified.'
    }

    $record = [ordered]@{
        schema_version = 1
        pid = $launchedPid
        process_start_utc = $launchedStartUtc.ToString('o')
        executable = $launchedExecutable
        application_root = [IO.Path]::GetFullPath($appRoot)
        release_mode = $env:TPS_RELEASE_MODE
    }
    $record | ConvertTo-Json | Set-Content -LiteralPath $recordPath -Encoding UTF8
    Protect-TpsSecretFile $recordPath
    Assert-TpsProtectedFileAcl $recordPath
    $authRecord = [ordered]@{
        schema_version = 1
        pid = $launchedPid
        access_token = $launchToken
    }
    $authRecord | ConvertTo-Json | Set-Content -LiteralPath $authPath -Encoding UTF8
    Protect-TpsSecretFile $authPath
    Assert-TpsProtectedFileAcl $authPath

    $url = "http://127.0.0.1:$port/api/ready"
    $ready = $false
    $dashboardDeadline = [DateTime]::UtcNow.AddSeconds([Math]::Min(60, $ReadyTimeoutSeconds))
    while ([DateTime]::UtcNow -lt $dashboardDeadline) {
        if ($dashboard.HasExited) {
            break
        }
        try {
            $payload = Invoke-RestMethod -Uri $url -Method Get -Headers @{ Authorization = "Bearer $launchToken" } -TimeoutSec 3
            if ($payload.ok -eq $true) {
                $ready = $true
                break
            }
        }
        catch {
            Start-Sleep -Seconds 1
        }
    }

    if (-not $ready) {
        throw 'The dashboard did not pass /api/ready. Review D:\TruePeopleSearch\logs\app.'
    }

    try {
        Start-Process "http://127.0.0.1:$port/#access_token=$launchToken" | Out-Null
    }
    catch {
        throw 'Dashboard became ready, but the protected local browser entry could not be opened.'
    }
}
catch {
    $launchFailure = $_
    if ($launchedPid -gt 0 -and $null -ne $launchedStartUtc -and $launchedExecutable) {
        try {
            $candidate = Get-Process -Id $launchedPid -ErrorAction Stop
            $sameProcess = (
                [Math]::Abs(($candidate.StartTime.ToUniversalTime() - $launchedStartUtc).TotalSeconds) -le 1 -and
                [IO.Path]::GetFullPath($candidate.Path) -eq $launchedExecutable
            )
            if ($sameProcess) {
                Stop-Process -InputObject $candidate -Force -ErrorAction Stop
            }
        }
        catch [Microsoft.PowerShell.Commands.ProcessCommandException] {
            # The launched child already exited; no process target remains.
        }
        catch {
            # Identity could not be re-verified, so never guess another PID.
        }
    }
    Remove-Item -LiteralPath $recordPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $authPath -Force -ErrorAction SilentlyContinue
    throw $launchFailure
}

Write-Host "MySQL and Redis are healthy; dashboard is ready at http://127.0.0.1:$port/"
Write-Host 'Safe dashboard mode is active. No feeder, worker, proxy test, or collection job was started.'
Write-Host 'Windows collection controls remain disabled until their process lifecycle has passed Windows-specific acceptance tests.'
