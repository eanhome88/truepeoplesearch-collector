[CmdletBinding()]
param(
    [string]$PythonCommand = "python",
    [string]$DatabasePort,
    [string]$RedisPort
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$MaxEnvironmentFileBytes = 1024 * 1024

function Test-LoopbackPort([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $connection = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $connection.AsyncWaitHandle.WaitOne(1500)) {
            return $false
        }
        $client.EndConnect($connection)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Fail([string]$Message) {
    Write-Host "[NOT READY] $Message" -ForegroundColor Red
    $script:ready = $false
}

function Convert-ApprovedPort([string]$Value, [string]$Name) {
    $parsed = 0
    $validNumber = -not [string]::IsNullOrWhiteSpace($Value) -and
        [int]::TryParse(
            $Value.Trim(),
            [Globalization.NumberStyles]::None,
            [Globalization.CultureInfo]::InvariantCulture,
            [ref]$parsed
        )
    if (-not $validNumber -or $parsed -lt 1 -or $parsed -gt 65535) {
        throw "Approved $Name must be an integer from 1 to 65535."
    }
    return $parsed
}

function Read-ReleasePortOverrides {
    # Never dot-source an environment file.  This reads only the two numeric
    # local dependency ports from a bounded, regular file in the release root.
    $releaseRoot = [IO.Path]::GetFullPath((Split-Path -Parent (Split-Path -Parent $PSScriptRoot)))
    $envPath = Join-Path $releaseRoot '.env'
    $ports = @{}
    try {
        $item = Get-Item -LiteralPath $envPath -Force -ErrorAction Stop
    }
    catch {
        if ($_.Exception -is [System.Management.Automation.ItemNotFoundException]) {
            return $ports
        }
        throw 'Release configuration file could not be inspected safely.'
    }
    if (-not ($item -is [System.IO.FileInfo]) -or
        (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw 'Release configuration file must be a regular file.'
    }
    if ($item.Length -gt $MaxEnvironmentFileBytes) {
        throw 'Release configuration file exceeds the approved size limit.'
    }

    try {
        $content = Get-Content -LiteralPath $envPath -Raw -Encoding UTF8 -ErrorAction Stop
    }
    catch {
        throw 'Release configuration file could not be read safely.'
    }
    foreach ($line in ($content -split "`r?`n")) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) {
            continue
        }
        $match = [regex]::Match($trimmed, '^(TPS_DB_PORT|TPS_REDIS_PORT)\s*=\s*(.*)$')
        if (-not $match.Success) {
            continue
        }
        $name = $match.Groups[1].Value
        if ($ports.ContainsKey($name)) {
            throw 'Release configuration contains duplicate approved port settings.'
        }
        $value = $match.Groups[2].Value.Trim()
        if ($value.Length -ge 2 -and
            (($value[0] -eq '"' -and $value[$value.Length - 1] -eq '"') -or
             ($value[0] -eq "'" -and $value[$value.Length - 1] -eq "'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $ports[$name] = Convert-ApprovedPort -Value $value -Name $name
    }
    return $ports
}

$ready = $true
Write-Host "Customer host readiness check (read-only)"
Write-Host "This script never starts services, installs packages, connects to a remote host, or changes a database."

$releasePorts = Read-ReleasePortOverrides
if ($PSBoundParameters.ContainsKey('DatabasePort')) {
    $databasePort = Convert-ApprovedPort -Value $DatabasePort -Name 'database port'
}
elseif ($releasePorts.ContainsKey('TPS_DB_PORT')) {
    $databasePort = $releasePorts['TPS_DB_PORT']
}
else {
    $databasePort = 4000
}
if ($PSBoundParameters.ContainsKey('RedisPort')) {
    $redisPort = Convert-ApprovedPort -Value $RedisPort -Name 'Redis port'
}
elseif ($releasePorts.ContainsKey('TPS_REDIS_PORT')) {
    $redisPort = $releasePorts['TPS_REDIS_PORT']
}
else {
    $redisPort = 6379
}

try {
    $version = & $PythonCommand --version 2>&1
    if ($LASTEXITCODE -ne 0 -or -not $version) {
        Fail "Python 3.9+ is not available through '$PythonCommand'."
    }
    else {
        $match = [regex]::Match(([string]$version), 'Python\s+(\d+)\.(\d+)')
        if (-not $match.Success -or [int]$match.Groups[1].Value -lt 3 -or
            ([int]$match.Groups[1].Value -eq 3 -and [int]$match.Groups[2].Value -lt 9)) {
            Fail "Python 3.9+ is required."
        }
        else {
            Write-Host "[OK] $version"
            & $PythonCommand -c "import flask, mysql.connector, redis, psutil" 2>$null
            if ($LASTEXITCODE -ne 0) {
                Fail "The approved dashboard Python dependencies are unavailable."
            }
            else {
                Write-Host "[OK] Approved dashboard Python dependencies are available"
            }
        }
    }
}
catch {
    Fail "Python command could not be executed."
}

foreach ($service in @(
    @{ Name = 'database'; Port = $databasePort },
    @{ Name = 'Redis'; Port = $redisPort }
)) {
    if (Test-LoopbackPort -Port $service.Port) {
        Write-Host "[OK] Local $($service.Name) is listening on its approved loopback port"
    }
    else {
        Fail "Local $($service.Name) is not reachable on its approved loopback port."
    }
}

if (-not $ready) {
    throw "Host readiness check failed. Do not start or initialize the customer release."
}

Write-Host "Host readiness check passed. No runtime action was performed."
