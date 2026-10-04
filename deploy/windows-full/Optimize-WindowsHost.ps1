[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$InstallRoot = 'D:\TruePeopleSearch'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

# Optimize-WindowsHost.ps1 - Windows 采集主机一次性性能调优（会修改系统状态，需管理员）。
# 只做幂等变更：重复执行不会叠加。改完 TCP 项后需要重启才完全生效。
#   [1] 机器画像：CPU/内存，并给出并发建议值（只打印，不改 runtime.env）
#   [2] Defender 实时扫描排除采集目录（Chromium 配置文件 + venv 每秒上百次 IO，不排除会被反复扫描）
#   [3] 高性能电源 + 禁止待机/休眠（7x24 采集，睡一次 WSL 时钟漂移会导致 TLS 报错）
#   [4] TCP 动态端口扩大 + TIME_WAIT 缩短（32 并发 + 代理高频建连，默认 16384 端口不够用）
#   [5] 禁止 Windows Update 在已登录时自动重启（跑一半被重启最伤）
#   [6] 时钟同步一次（TLS/CF 对时钟漂移敏感）
# 退出码：0 全部搞定；1 存在失败项。

$script:OkCount = 0
$script:ChangeCount = 0
$script:SkipCount = 0
$script:FailCount = 0
$script:NeedReboot = $false

function Write-HostOk { param([string]$Message) $script:OkCount++; Write-Host "通过：$Message" -ForegroundColor Green }
function Write-HostChange { param([string]$Message) $script:ChangeCount++; Write-Host "已设置：$Message" -ForegroundColor Cyan }
function Write-HostSkip { param([string]$Message) $script:SkipCount++; Write-Host "跳过：$Message" -ForegroundColor Gray }
function Write-HostWarn { param([string]$Message) Write-Host "警告：$Message" -ForegroundColor Yellow }
function Write-HostBad { param([string]$Message) $script:FailCount++; Write-Host "失败：$Message" -ForegroundColor Red }

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object -TypeName Security.Principal.WindowsPrincipal -ArgumentList $identity
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdmin)) {
    Write-HostBad '当前不是管理员权限，请右键 PowerShell 选择“以管理员身份运行”后重试。'
    exit 1
}

$root = Get-NormalizedInstallRoot -InstallRoot $InstallRoot
Write-Host ("采集目录：$root")

# [1/6] 机器画像 + 并发建议。
try {
    $cpuCount = 0
    foreach ($cpu in (Get-CimInstance -ClassName Win32_Processor)) {
        $cpuCount += [int]$cpu.NumberOfLogicalProcessors
    }
    $osInfo = Get-CimInstance -ClassName Win32_OperatingSystem
    $ramGb = [math]::Round([double]$osInfo.TotalVisibleMemorySize / 1MB, 1)
    # 单浏览器约 0.5~1GB；系统 + Docker(MySQL/Redis) 预留 4GB。
    $suggest = [math]::Max(2, [math]::Min(64, [int](($ramGb - 4) / 1)))
    if ($cpuCount -gt 0) {
        $suggest = [math]::Min($suggest, $cpuCount * 2)
    }
    if ($ramGb -lt 16) {
        $suggest = [math]::Min($suggest, 8)
    }
    Write-HostOk ("[1/6] CPU 逻辑核心 {0} 个，内存 {1}GB，建议 TPS_CONCURRENCY 不超过 {2}（当前值以 runtime.env 为准，需改请找 Cline 调整后重启采集）。" -f $cpuCount, $ramGb, $suggest)
}
catch {
    Write-HostBad ('[1/6] 读取机器配置失败：' + $_.Exception.Message)
}

# [2/6] Defender 排除采集目录。
try {
    $mpStatus = $null
    try { $mpStatus = Get-MpComputerStatus -ErrorAction Stop } catch { $mpStatus = $null }
    if ($null -eq $mpStatus) {
        Write-HostSkip '[2/6] 未检测到 Microsoft Defender（可能被第三方杀软接管），跳过排除设置。'
    }
    elseif (-not $mpStatus.RealTimeProtectionEnabled) {
        Write-HostSkip '[2/6] Defender 实时保护未开启，无需加排除。'
    }
    else {
        $pref = Get-MpPreference
        $existing = @()
        if ($null -ne $pref.ExclusionPath) { $existing = @($pref.ExclusionPath) }
        $want = @($root)
        $runtimeDir = Join-Path $root 'runtime'
        if (Test-Path -LiteralPath $runtimeDir -PathType Container) { $want += $runtimeDir }
        foreach ($path in $want) {
            $already = $false
            foreach ($item in $existing) {
                if ($item.TrimEnd('\') -ieq $path.TrimEnd('\')) { $already = $true; break }
            }
            if ($already) {
                Write-HostOk ("[2/6] Defender 排除已存在：$path")
            }
            elseif ($PSCmdlet.ShouldProcess($path, '加入 Defender 扫描排除')) {
                Add-MpPreference -ExclusionPath $path
                Write-HostChange ("[2/6] Defender 排除已加入：$path")
            }
        }
    }
}
catch {
    Write-HostBad ('[2/6] Defender 排除设置失败：' + $_.Exception.Message)
}

# [3/6] 高性能电源 + 禁止待机/休眠。
try {
    $highPerf = '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c'
    $active = (& powercfg /getactivescheme 2>$null | Out-String)
    if ($active -match [regex]::Escape($highPerf)) {
        Write-HostOk '[3/6] 已是高性能电源方案。'
    }
    elseif ($PSCmdlet.ShouldProcess('高性能电源方案', '启用')) {
        & powercfg /setactive $highPerf
        if ($LASTEXITCODE -ne 0) { throw ('powercfg /setactive 退出码 ' + $LASTEXITCODE) }
        Write-HostChange '[3/6] 已切换到高性能电源方案。'
    }
    foreach ($setting in @('standby-timeout-ac', 'hibernate-timeout-ac', 'disk-timeout-ac')) {
        if ($PSCmdlet.ShouldProcess($setting, '设为从不')) {
            & powercfg /change $setting 0 2>$null
        }
    }
    Write-HostOk '[3/6] 交流供电下待机/休眠/硬盘关闭已设为从不。'
}
catch {
    Write-HostBad ('[3/6] 电源设置失败：' + $_.Exception.Message)
}

# [4/6] TCP 动态端口 + TIME_WAIT。
try {
    $rangeText = ((& netsh int ipv4 show dynamicport tcp 2>$null | Out-String) -replace "\x00", '')
    $startMatch = [regex]::Match($rangeText, '(起始端口|Start Port)\s*[:：]\s*(\d+)')
    $numMatch = [regex]::Match($rangeText, '(端口数|Number of Ports)\s*[:：]\s*(\d+)')
    $startPort = -1
    $portNum = -1
    if ($startMatch.Success) { $startPort = [int]$startMatch.Groups[2].Value }
    if ($numMatch.Success) { $portNum = [int]$numMatch.Groups[2].Value }
    if ($startPort -lt 0 -or $portNum -lt 0) {
        Write-HostWarn '[4/6] 读不到当前动态端口范围（可能是系统语言输出格式差异），跳过本项，手动执行 netsh int ipv4 set dynamicport tcp start=1025 num=60000 即可。'
    }
    elseif ($startPort -eq 1025 -and $portNum -ge 60000) {
        Write-HostOk '[4/6] TCP 动态端口范围已足够大。'
    }
    elseif ($PSCmdlet.ShouldProcess('TCP 动态端口', '扩大到 1025 起 60000 个')) {
        & netsh int ipv4 set dynamicport tcp start=1025 num=60000 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw ('netsh 退出码 ' + $LASTEXITCODE) }
        Write-HostChange '[4/6] TCP 动态端口已扩大（1025 起 60000 个，需重启生效）。'
        $script:NeedReboot = $true
    }
    $tcpParams = 'HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters'
    $currentWait = Get-ItemProperty -Path $tcpParams -Name 'TcpTimedWaitDelay' -ErrorAction SilentlyContinue
    $waitValue = -1
    if ($null -ne $currentWait) { $waitValue = [int]$currentWait.TcpTimedWaitDelay }
    if ($waitValue -eq 30) {
        Write-HostOk '[4/6] TcpTimedWaitDelay 已是 30 秒。'
    }
    elseif ($PSCmdlet.ShouldProcess('TcpTimedWaitDelay', '设为 30 秒')) {
        New-ItemProperty -Path $tcpParams -Name 'TcpTimedWaitDelay' -Value 30 -PropertyType DWord -Force | Out-Null
        Write-HostChange '[4/6] TcpTimedWaitDelay 已设为 30 秒（需重启生效）。'
        $script:NeedReboot = $true
    }
}
catch {
    Write-HostBad ('[4/6] TCP 参数设置失败：' + $_.Exception.Message)
}

# [5/6] 禁止已登录时 Windows Update 自动重启。
try {
    $auKey = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
    $current = Get-ItemProperty -Path $auKey -Name 'NoAutoRebootWithLoggedOnUsers' -ErrorAction SilentlyContinue
    if ($null -ne $current -and [int]$current.NoAutoRebootWithLoggedOnUsers -eq 1) {
        Write-HostOk '[5/6] 已禁止登录状态下自动重启。'
    }
    elseif ($PSCmdlet.ShouldProcess('Windows Update', '禁止已登录时自动重启')) {
        if (-not (Test-Path -LiteralPath $auKey)) {
            New-Item -Path $auKey -Force | Out-Null
        }
        New-ItemProperty -Path $auKey -Name 'NoAutoRebootWithLoggedOnUsers' -Value 1 -PropertyType DWord -Force | Out-Null
        Write-HostChange '[5/6] 已禁止登录状态下自动重启（更新仍会下载安装，只是不再半夜重启）。'
    }
}
catch {
    Write-HostBad ('[5/6] 更新策略设置失败：' + $_.Exception.Message)
}

# [6/6] 时钟同步一次。
try {
    $syncOut = ((& w32tm /resync 2>&1 | Out-String) -replace "\x00", '').Trim()
    if ($LASTEXITCODE -eq 0) {
        Write-HostOk '[6/6] 时钟同步成功。'
    }
    else {
        Write-HostWarn ('[6/6] 时钟同步未成功（不影响继续，TLS 对时间敏感，若后续出现证书类错误再处理）：' + $syncOut)
    }
}
catch {
    Write-HostWarn ('[6/6] 时钟同步异常（警告，不影响继续）：' + $_.Exception.Message)
}

Write-Host ''
Write-Host ("调优汇总：正常 {0} 项，变更 {1} 项，跳过 {2} 项，失败 {3} 项。" -f $script:OkCount, $script:ChangeCount, $script:SkipCount, $script:FailCount)
if ($script:NeedReboot) {
    Write-HostWarn 'TCP 参数改动需要重启系统才完全生效，请找个低峰期重启一次客户机。'
}
if ($script:FailCount -gt 0) {
    Write-Host '存在失败项，请按上方红色提示处理后重新运行本脚本。' -ForegroundColor Red
    exit 1
}
Write-Host '主机调优完成。' -ForegroundColor Green
exit 0
