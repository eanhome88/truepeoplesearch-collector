[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string]$InstallRoot = 'D:\TruePeopleSearch',
    [switch]$StopInfrastructure
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

$root = Get-NormalizedInstallRoot $InstallRoot
$appRoot = Get-TpsAppRoot $root
$recordPath = Join-Path $root 'runtime\dashboard-process.json'
$authPath = Join-Path $root 'runtime\dashboard-auth.json'

if (Test-Path -LiteralPath $recordPath -PathType Leaf) {
    Assert-TpsProtectedFileAcl $recordPath
    $record = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($record.schema_version -ne 1 -or [int]$record.pid -le 0 -or -not $record.process_start_utc -or -not $record.executable) {
        throw 'Dashboard process record is invalid; refusing to guess a process target.'
    }
    $removeRecord = $false
    try {
        $process = Get-Process -Id ([int]$record.pid) -ErrorAction Stop
        $recordedStart = [DateTime]::Parse([string]$record.process_start_utc).ToUniversalTime()
        $actualStart = $process.StartTime.ToUniversalTime()
        $actualExecutable = [IO.Path]::GetFullPath($process.Path)
        if ([Math]::Abs(($actualStart - $recordedStart).TotalSeconds) -gt 1 -or
            $actualExecutable -ne [IO.Path]::GetFullPath([string]$record.executable)) {
            throw 'Dashboard process identity does not match; refusing to stop it.'
        }
        if ($PSCmdlet.ShouldProcess("PID $($process.Id)", 'Stop the verified dashboard process')) {
            Stop-Process -Id $process.Id -ErrorAction Stop
            $process.WaitForExit(10000)
            if (-not $process.HasExited) {
                throw 'Dashboard did not stop within 10 seconds.'
            }
            $removeRecord = $true
        }
    }
    catch [Microsoft.PowerShell.Commands.ProcessCommandException] {
        Write-Host 'Recorded dashboard process is no longer running.'
        $removeRecord = $true
    }
    if ($removeRecord) {
        Remove-Item -LiteralPath $recordPath -Force
        Remove-Item -LiteralPath $authPath -Force -ErrorAction SilentlyContinue
    }
}
else {
    Write-Host 'No dashboard process record exists; no process was stopped.'
}

if ($StopInfrastructure) {
    $configuration = Read-TpsRuntimeEnvironment (Join-Path $root 'config\runtime.env') $root
    Set-TpsProcessEnvironment $configuration
    Assert-TpsBusinessStorageOnD $root
    Assert-TpsDockerStorageOnD $root
    $dockerTools = Get-TpsDockerTools $root
    $composeExecutable = $dockerTools.Compose
    $compose = Get-TpsComposeArguments $root $appRoot
    if ($PSCmdlet.ShouldProcess('tps-full MySQL and Redis containers', 'Stop local infrastructure without deleting data')) {
        & $composeExecutable @compose stop mysql redis
        if ($LASTEXITCODE -ne 0) {
            throw 'Could not stop local MySQL/Redis containers.'
        }
    }
}

Write-Host 'Stop completed. No database volume, queue file, log, or backup was deleted.'
