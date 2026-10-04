[CmdletBinding()]
param(
    [string]$InstallRoot = 'D:\TruePeopleSearch',
    [switch]$NoCollection
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

$root = Get-NormalizedInstallRoot $InstallRoot
Assert-TpsSupportedWindowsHost
$appRoot = Get-TpsAppRoot $root
$python = Get-TpsPythonPath $root

function Test-DashboardAlive {
    $recordPath = Join-Path $root 'runtime\dashboard-process.json'
    if (-not (Test-Path -LiteralPath $recordPath -PathType Leaf)) {
        return $false
    }
    try {
        Assert-TpsProtectedFileAcl $recordPath
        $record = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([string]$record.release_mode -ne 'customer') {
            return $false
        }
        $process = Get-Process -Id ([int]$record.pid) -ErrorAction Stop
        $recordedStart = [DateTime]::Parse([string]$record.process_start_utc).ToUniversalTime()
        return (
            [Math]::Abs(($process.StartTime.ToUniversalTime() - $recordedStart).TotalSeconds) -le 1 -and
            [IO.Path]::GetFullPath($process.Path) -eq [IO.Path]::GetFullPath($python)
        )
    }
    catch [Microsoft.PowerShell.Commands.ProcessCommandException] {
        return $false
    }
}

Write-Host '[1/3] 检查基础栈（面板 + MySQL + Redis）...' -ForegroundColor Cyan
if (Test-DashboardAlive) {
    Write-Host '基础栈已在运行，跳过启动。' -ForegroundColor Green
}
else {
    Write-Host '正在启动基础栈，首次启动需要几分钟，请稍候...' -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot 'Start-Stack.ps1') -InstallRoot $root
    if (-not (Test-DashboardAlive)) {
        throw '基础栈启动后仍未就绪，请查看上方报错输出，修复后再试。'
    }
    Write-Host '基础栈就绪。' -ForegroundColor Green
}

if ($NoCollection) {
    Write-Host '[2/3] 已按要求跳过采集，仅面板运行中。' -ForegroundColor Green
    Write-Host '[3/3] 完成：在浏览器打开面板查看队列与数据。'
    exit 0
}

Write-Host '[2/3] 检查采集代理配置...' -ForegroundColor Cyan
$configuration = Read-TpsRuntimeEnvironment (Join-Path $root 'config\runtime.env') $root
$proxyOk = @('CLOUDBYPASS_PROXY', 'PROXY_TUNNEL', 'PROXY_FILE') | Where-Object {
    $configuration.ContainsKey($_) -and -not [string]::IsNullOrWhiteSpace($configuration[$_])
}
if (@($proxyOk).Count -eq 0) {
    Write-Host ''
    Write-Host '还没配代理，采集开不了。' -ForegroundColor Yellow
    Write-Host '请用记事本打开 D:\TruePeopleSearch\config\runtime.env，加上一行（3 选 1）：'
    Write-Host '  CLOUDBYPASS_PROXY=http://你的代理地址:端口'
    Write-Host '  # 或 PROXY_TUNNEL=... / PROXY_FILE=...'
    Write-Host '保存后再跑一次这个脚本。面板已经开着，不用关。'
    exit 1
}
Write-Host '代理已配置，正在启动采集...' -ForegroundColor Yellow
& (Join-Path $PSScriptRoot 'Start-Collector.ps1') -InstallRoot $root

Write-Host '[3/3] 全部就绪。' -ForegroundColor Green
Write-Host '面板看进度，停采集用 Stop-Collector.ps1，停面板用 Stop-Stack.ps1。'
