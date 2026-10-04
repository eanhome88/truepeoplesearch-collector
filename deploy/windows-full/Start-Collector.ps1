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

# Collection proxy credentials are installation state: they are never part of
# the release archive and must be configured locally before collection starts.
$proxySources = @('CLOUDBYPASS_PROXY', 'PROXY_TUNNEL', 'PROXY_FILE', 'PROXY_API_URL') | Where-Object {
    $configuration.ContainsKey($_) -and -not [string]::IsNullOrWhiteSpace($configuration[$_])
}
if (@($proxySources).Count -eq 0) {
    throw 'No collection proxy configured. Add CLOUDBYPASS_PROXY (or PROXY_TUNNEL/PROXY_FILE) to config\runtime.env first; proxy credentials are never shipped in the bundle.'
}

$concurrency = 4
if ($configuration.ContainsKey('TPS_CONCURRENCY') -and -not [string]::IsNullOrWhiteSpace($configuration['TPS_CONCURRENCY'])) {
    $parsed = 0
    if (-not [int]::TryParse([string]$configuration['TPS_CONCURRENCY'], [ref]$parsed)) {
        throw 'TPS_CONCURRENCY in runtime.env must be an integer.'
    }
    if ($parsed -lt 1 -or $parsed -gt 64) {
        Write-Host "TPS_CONCURRENCY=$parsed is outside the range 1-64; clamped." -ForegroundColor Yellow
        $parsed = [Math]::Min(64, [Math]::Max(1, $parsed))
    }
    elseif ($parsed -gt 8) {
        Write-Host "TPS_CONCURRENCY=$parsed is above the routine customer range 1-8 (high-throughput mode)." -ForegroundColor Yellow
    }
    $concurrency = $parsed
}

# The base stack (dashboard + MySQL + Redis) must be running first.
$dashboardRecordPath = Join-Path $root 'runtime\dashboard-process.json'
if (-not (Test-Path -LiteralPath $dashboardRecordPath -PathType Leaf)) {
    throw 'The base stack is not running. Start it first with Start-Stack.ps1.'
}
Assert-TpsProtectedFileAcl $dashboardRecordPath
$dashboardRecord = Get-Content -LiteralPath $dashboardRecordPath -Raw -Encoding UTF8 | ConvertFrom-Json
try {
    $dashboardProcess = Get-Process -Id ([int]$dashboardRecord.pid) -ErrorAction Stop
    $recordedStart = [DateTime]::Parse([string]$dashboardRecord.process_start_utc).ToUniversalTime()
    if ([Math]::Abs(($dashboardProcess.StartTime.ToUniversalTime() - $recordedStart).TotalSeconds) -gt 1 -or
        [IO.Path]::GetFullPath($dashboardProcess.Path) -ne [IO.Path]::GetFullPath($python)) {
        throw 'Dashboard process identity does not match its launch record.'
    }
}
catch [Microsoft.PowerShell.Commands.ProcessCommandException] {
    throw 'The recorded dashboard process is not running. Start the base stack first with Start-Stack.ps1.'
}

$dockerTools = Get-TpsDockerTools $root
$docker = $dockerTools.Docker
foreach ($container in @('tps-full-mysql', 'tps-full-redis')) {
    $status = (& $docker --context desktop-linux inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' $container 2>$null)
    if ($LASTEXITCODE -ne 0 -or ([string]$status).Trim() -ne 'healthy') {
        throw "Local dependency is not healthy: $container. Start the base stack first with Start-Stack.ps1."
    }
}

$recordPath = Join-Path $root 'runtime\collector-process.json'
if (Test-Path -LiteralPath $recordPath -PathType Leaf) {
    Assert-TpsProtectedFileAcl $recordPath
    $existing = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($existing.schema_version -ne 1 -or [int]$existing.pid -le 0 -or
        [string]$existing.release_mode -ne 'customer-collector' -or
        [IO.Path]::GetFullPath([string]$existing.executable) -ne [IO.Path]::GetFullPath($python) -or
        [IO.Path]::GetFullPath([string]$existing.application_root) -ne [IO.Path]::GetFullPath($appRoot)) {
        throw 'The prior collector process record is invalid; refusing to guess a process target.'
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
        throw "A collector is already running (PID $($existingProcess.Id)). Stop it first with Stop-Collector.ps1."
    }
    Remove-Item -LiteralPath $recordPath -Force
}

# Drop a stale supervisor PID/mode record only after its process is confirmed dead.
$supervisorPidPath = Join-Path $root 'runtime\supervisor.pid'
foreach ($stale in @($supervisorPidPath, ($supervisorPidPath + '.mode.json'), ($supervisorPidPath + '.lock'))) {
    if (Test-Path -LiteralPath $stale -PathType Leaf) {
        $stalePid = 0
        try { $stalePid = [int](Get-Content -LiteralPath $stale -Raw -Encoding UTF8).Trim() } catch { $stalePid = 0 }
        $staleAlive = $false
        if ($stalePid -gt 0) {
            try { $null = Get-Process -Id $stalePid -ErrorAction Stop; $staleAlive = $true }
            catch [Microsoft.PowerShell.Commands.ProcessCommandException] { $staleAlive = $false }
        }
        if ($staleAlive) {
            throw "An unmanaged supervisor process is still running (PID $stalePid). Stop it before starting the collector."
        }
        Remove-Item -LiteralPath $stale -Force
    }
}

# customer-collector is a reviewed collection mode: it passes the customer-mode
# gate in tps_supervisor.py (which only restricts exactly 'customer') while
# remaining distinct from it in every process record.
Set-TpsProcessEnvironment $configuration -IncludeServiceCredentials
$env:TPS_RELEASE_MODE = 'customer-collector'
$env:TPS_CONCURRENCY = [string]$concurrency
$stdoutLog = Join-Path $root 'logs\app\collector.out.log'
$stderrLog = Join-Path $root 'logs\app\collector.err.log'
$arguments = @(
    '-I', '-X', 'utf8', '-B', '-u', (Join-Path $appRoot 'scripts\tps_supervisor.py'),
    'start', '--no-dashboard', '--concurrency', [string]$concurrency
)
$collector = $null
$launchedPid = 0
$launchedStartUtc = $null
$launchedExecutable = $null
try {
    $collector = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $appRoot -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru
    if ($null -eq $collector) {
        throw 'The collector process could not be started.'
    }
    $collector.Refresh()
    $launchedPid = $collector.Id
    $launchedStartUtc = $collector.StartTime.ToUniversalTime()
    $launchedExecutable = [IO.Path]::GetFullPath($collector.Path)
    if ($collector.HasExited -or $launchedExecutable -ne [IO.Path]::GetFullPath($python)) {
        throw 'The launched collector process identity could not be verified.'
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

    $supervisorDeadline = [DateTime]::UtcNow.AddSeconds([Math]::Min(60, $ReadyTimeoutSeconds))
    $supervisorReady = $false
    while ([DateTime]::UtcNow -lt $supervisorDeadline) {
        if ($collector.HasExited) {
            break
        }
        if (Test-Path -LiteralPath $supervisorPidPath -PathType Leaf) {
            try {
                if ([int](Get-Content -LiteralPath $supervisorPidPath -Raw -Encoding UTF8).Trim() -eq $launchedPid) {
                    $supervisorReady = $true
                    break
                }
            }
            catch {
                Start-Sleep -Seconds 1
            }
        }
        Start-Sleep -Seconds 1
    }
    if (-not $supervisorReady) {
        throw 'The collector supervisor did not claim its PID file. Review logs\app\collector.err.log.'
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
                # Tree kill: the supervisor may already have spawned workers.
                & taskkill /PID ([string]$launchedPid) /T /F | Out-Null
            }
        }
        catch [Microsoft.PowerShell.Commands.ProcessCommandException] {
        }
        catch {
        }
    }
    Remove-Item -LiteralPath $recordPath -Force -ErrorAction SilentlyContinue
    throw $launchFailure
}

Write-Host "Collector started (supervisor PID $launchedPid, concurrency $concurrency)."
Write-Host 'Queue depth and results remain visible in the local dashboard.'
Write-Host 'Stop collection with Stop-Collector.ps1; the base stack keeps running.'
