[CmdletBinding()]
param(
    [string]$InstallRoot = 'D:\TruePeopleSearch'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

$root = Get-NormalizedInstallRoot $InstallRoot
Assert-TpsSupportedWindowsHost
$null = Assert-TpsProtectedDirectoryAcl $root
$appRoot = Get-TpsAppRoot $root
& (Join-Path $appRoot 'deploy\windows-full\Test-FullBundle.ps1') -BundleRoot $root -AllowRuntimeState
Assert-TpsRuntimeManifest $root
$python = Get-TpsPythonPath $root
$configuration = Read-TpsRuntimeEnvironment (Join-Path $root 'config\runtime.env') $root
Set-TpsProcessEnvironment $configuration
Assert-TpsBusinessStorageOnD $root
Assert-TpsDockerStorageOnD $root
$dockerTools = Get-TpsDockerTools $root
$docker = $dockerTools.Docker
$recordPath = Join-Path $root 'runtime\dashboard-process.json'
$authPath = Join-Path $root 'runtime\dashboard-auth.json'
Assert-TpsProtectedFileAcl $recordPath
Assert-TpsProtectedFileAcl $authPath
$record = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
$auth = Get-Content -LiteralPath $authPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($record.schema_version -ne 1 -or [int]$record.pid -le 0 -or
    [int]$record.pid -ne [int]$auth.pid -or
    [string]$record.process_start_utc -notmatch '^\d{4}-\d{2}-\d{2}T' -or
    [IO.Path]::GetFullPath([string]$record.executable) -ne [IO.Path]::GetFullPath($python) -or
    [IO.Path]::GetFullPath([string]$record.application_root) -ne [IO.Path]::GetFullPath($appRoot) -or
    [string]$record.release_mode -ne 'customer' -or
    $auth.schema_version -ne 1 -or [int]$auth.pid -le 0 -or
    [string]$auth.access_token -notmatch '^[A-Fa-f0-9]{48}$') {
    throw 'Dashboard authentication record is invalid.'
}
try {
    $dashboardProcess = Get-Process -Id ([int]$record.pid) -ErrorAction Stop
    $recordedStart = [DateTime]::Parse([string]$record.process_start_utc).ToUniversalTime()
    if ([Math]::Abs(($dashboardProcess.StartTime.ToUniversalTime() - $recordedStart).TotalSeconds) -gt 1 -or
        [IO.Path]::GetFullPath($dashboardProcess.Path) -ne [IO.Path]::GetFullPath($python)) {
        throw 'Dashboard process identity does not match its protected launch record.'
    }
}
catch [Microsoft.PowerShell.Commands.ProcessCommandException] {
    throw 'The protected dashboard process is not running.'
}
$dashboardHeaders = @{ Authorization = ('Bearer ' + [string]$auth.access_token) }
$expectedDockerImages = Get-TpsVerifiedDockerImageIds $root

$failed = $false
foreach ($container in @('tps-full-mysql', 'tps-full-redis')) {
    $status = (& $docker --context desktop-linux inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' $container 2>$null)
    if ($LASTEXITCODE -ne 0 -or ([string]$status).Trim() -ne 'healthy') {
        Write-Host "[FAIL] $container is not healthy" -ForegroundColor Red
        $failed = $true
    }
    else {
        Write-Host "[OK] $container is healthy"
    }
}
$containerImages = @{
    'tps-full-mysql' = 'tps-offline/mysql:8.4.11-amd64'
    'tps-full-redis' = 'tps-offline/redis:7.4.8-alpine-amd64'
}
foreach ($container in $containerImages.Keys) {
    $actualImage = ([string](& $docker --context desktop-linux inspect --format '{{.Image}}' $container 2>$null)).Trim().ToLowerInvariant()
    $expectedImage = ([string]$expectedDockerImages[$containerImages[$container]]).ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $actualImage -ne $expectedImage) {
        Write-Host "[FAIL] $container image identity mismatch" -ForegroundColor Red
        $failed = $true
    }
}

$probe = @'
import json, os
import mysql.connector
import redis

db = mysql.connector.connect(
    host=os.environ['TPS_DB_HOST'], port=int(os.environ['TPS_DB_PORT']),
    user=os.environ['TPS_DB_USER'], password=os.environ['TPS_DB_PASSWORD'],
    database=os.environ['TPS_DB_NAME'], connection_timeout=3,
    read_timeout=3, write_timeout=3,
)
cursor = db.cursor()
cursor.execute('SELECT VERSION()')
mysql_version = str(cursor.fetchone()[0]).split('-', 1)[0]
cursor.execute('SELECT COUNT(*) FROM persons')
persons = int(cursor.fetchone()[0])
cursor.close()
db.close()

queue = redis.Redis(
    host=os.environ['TPS_REDIS_HOST'], port=int(os.environ['TPS_REDIS_PORT']),
    password=os.environ['TPS_REDIS_PASSWORD'],
    socket_connect_timeout=3, socket_timeout=3,
)
queue.ping()
redis_version = str(queue.info('server').get('redis_version', ''))
if mysql_version != '8.4.11' or redis_version != '7.4.8':
    raise RuntimeError('dependency version mismatch')
summary = {
    'mysql_version': mysql_version,
    'redis_version': redis_version,
    'persons': persons,
    'pending': int(queue.llen('tps:pending')),
    'processing': int(queue.llen('tps:processing')),
    'leases': int(queue.zcard('tps:leases')),
    'dlq': int(queue.llen('tps:dlq')),
}
queue.close()
print(json.dumps(summary, sort_keys=True))
'@

Set-TpsProcessEnvironment $configuration -IncludeServiceCredentials
$summary = $null
$probeExitCode = 1
try {
    $summary = & $python -I -B -c $probe
    $probeExitCode = $LASTEXITCODE
}
finally {
    Clear-TpsServiceCredentialEnvironment
}
if ($probeExitCode -ne 0) {
    Write-Host '[FAIL] Database/Redis business-readiness probe failed' -ForegroundColor Red
    $failed = $true
}
else {
    Write-Host "[OK] Local data services: $summary"
}

$port = Convert-ToApprovedPort $configuration['TPS_DASHBOARD_PORT'] 'TPS_DASHBOARD_PORT'
$unauthorizedStatus = 0
try {
    $unexpected = Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/system/version" -Method Get -UseBasicParsing -TimeoutSec 5
    $unauthorizedStatus = [int]$unexpected.StatusCode
}
catch {
    if ($null -ne $_.Exception.Response) {
        $unauthorizedStatus = [int]$_.Exception.Response.StatusCode
    }
}
if ($unauthorizedStatus -ne 401) {
    Write-Host '[FAIL] Customer API accepted a request without its protected bearer token' -ForegroundColor Red
    $failed = $true
}
else {
    Write-Host '[OK] Customer API rejects unauthenticated local requests'
}
foreach ($endpoint in @('/api/health', '/api/ready', '/api/system/version')) {
    try {
        $response = Invoke-RestMethod -Uri ("http://127.0.0.1:$port" + $endpoint) -Method Get -Headers $dashboardHeaders -TimeoutSec 5
        if ($response.ok -ne $true) {
            throw 'endpoint returned ok=false'
        }
        if ($endpoint -eq '/api/system/version') {
            $expectedVersion = Get-Content -LiteralPath (Join-Path $appRoot 'version.json') -Raw -Encoding UTF8 | ConvertFrom-Json
            if ([string]$response.release_mode -ne 'customer' -or
                [string]$response.version -ne [string]$expectedVersion.version -or
                [string]$response.build -ne [string]$expectedVersion.build) {
                throw 'dashboard release identity does not match the verified application bundle'
            }
        }
        Write-Host "[OK] $endpoint"
    }
    catch {
        Write-Host "[FAIL] $endpoint" -ForegroundColor Red
        $failed = $true
    }
}

if ($failed) {
    throw 'Full-stack readiness failed. A listening process alone is not accepted as ready.'
}

Write-Host 'Full-stack local readiness passed.'
Write-Host 'This check made no external request and did not start a collection job.'
