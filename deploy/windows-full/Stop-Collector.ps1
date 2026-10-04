[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string]$InstallRoot = 'D:\TruePeopleSearch'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

$root = Get-NormalizedInstallRoot $InstallRoot
$appRoot = Get-TpsAppRoot $root
$recordPath = Join-Path $root 'runtime\collector-process.json'
$python = Get-TpsPythonPath $root

if (-not (Test-Path -LiteralPath $recordPath -PathType Leaf)) {
    Write-Host 'No collector process record exists; no process was stopped.'
    exit 0
}

Assert-TpsProtectedFileAcl $recordPath
$record = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
$recordedExe = [IO.Path]::GetFullPath([string]$record.executable)
$runtimePrefix = $root.TrimEnd('\') + '\runtime\'
$exeOk = ($recordedExe -eq [IO.Path]::GetFullPath($python)) -or (
    $recordedExe.StartsWith($runtimePrefix, [StringComparison]::OrdinalIgnoreCase) -and
    $recordedExe.EndsWith('\python.exe', [StringComparison]::OrdinalIgnoreCase)
)
if ($record.schema_version -ne 1 -or [int]$record.pid -le 0 -or
    [string]$record.release_mode -ne 'customer-collector' -or
    -not $record.process_start_utc -or -not $exeOk -or -not $record.application_root -or
    [IO.Path]::GetFullPath([string]$record.application_root) -ne [IO.Path]::GetFullPath($appRoot)) {
    throw 'Collector process record is invalid; refusing to guess a process target.'
}

$removeRecord = $false
try {
    $process = Get-Process -Id ([int]$record.pid) -ErrorAction Stop
    $recordedStart = [DateTime]::Parse([string]$record.process_start_utc).ToUniversalTime()
    if ([Math]::Abs(($process.StartTime.ToUniversalTime() - $recordedStart).TotalSeconds) -gt 1 -or
        [IO.Path]::GetFullPath($process.Path) -ne [IO.Path]::GetFullPath([string]$record.executable)) {
        throw 'Collector process identity does not match; refusing to stop it.'
    }
    if ($PSCmdlet.ShouldProcess("PID $($process.Id)", 'Stop the verified collector supervisor and its worker tree')) {
        # The supervisor owns worker/feeder child processes: stop the whole
        # tree, or orphaned workers would keep collecting with no record.
        $supervisorPidPath = Join-Path $root 'runtime\supervisor.pid'
        if (Test-Path -LiteralPath $supervisorPidPath -PathType Leaf) {
            $supervisorPid = 0
            try { $supervisorPid = [int](Get-Content -LiteralPath $supervisorPidPath -Raw -Encoding UTF8).Trim() } catch { $supervisorPid = 0 }
            if ($supervisorPid -ne [int]$record.pid) {
                throw 'Supervisor PID file does not match the verified collector record; refusing to guess a process target.'
            }
        }
        & taskkill /PID ([string]$process.Id) /T /F | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'Collector tree could not be stopped; the process record was kept for review.'
        }
        $process.WaitForExit(30000)
        if (-not $process.HasExited) {
            throw 'Collector did not stop within 30 seconds; the process record was kept for review.'
        }
        $removeRecord = $true
    }
}
catch [Microsoft.PowerShell.Commands.ProcessCommandException] {
    Write-Host 'Recorded collector process is no longer running.'
    $removeRecord = $true
}
if ($removeRecord) {
    Remove-Item -LiteralPath $recordPath -Force
}

Write-Host 'Collector stopped. Queued jobs, database rows, logs, and backups were kept.'
Write-Host 'The base stack (dashboard, MySQL, Redis) keeps running; use Stop-Stack.ps1 to stop it.'
