[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

# Test-CustomerEnv.ps1 - 客户机环境预检脚本（只做检查，不修改任何系统状态）。
# 检测口径与 Common.ps1 / Initialize-Runtime.ps1 保持一致：
#   管理员权限、Windows 10/11 build 号、WSL（商店版优先、NUL 剥离、版本三元组正则、
#   要求 >= 2.1.5）、D 盘存在且可写、Docker Desktop（缺失仅警告）、PowerShell 版本。
# 唯一写入动作：D 盘可写性探针会在 D:\ 下创建一个临时文件并立即删除。
# 退出码：全部通过（含警告）返回 0；存在任一失败返回 1。

$script:PassCount = 0
$script:WarningCount = 0
$script:FailCount = 0

function Write-EnvPass {
    param([Parameter(Mandatory = $true)][string]$Message)
    $script:PassCount++
    Write-Host "通过：$Message" -ForegroundColor Green
}

function Write-EnvWarning {
    param([Parameter(Mandatory = $true)][string]$Message)
    $script:WarningCount++
    Write-Host "警告：$Message" -ForegroundColor Yellow
}

function Write-EnvFail {
    param([Parameter(Mandatory = $true)][string]$Message)
    $script:FailCount++
    Write-Host "失败：$Message" -ForegroundColor Red
}

Write-Host '开始客户机环境预检（只检查、不修改）。'

# [1/6] 当前是否是管理员。
try {
    $windowsIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $windowsPrincipal = New-Object -TypeName Security.Principal.WindowsPrincipal -ArgumentList $windowsIdentity
    if ($windowsPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-EnvPass '[1/6] 当前已是管理员权限。'
    }
    else {
        Write-EnvFail '[1/6] 当前不是管理员权限，请右键 PowerShell 选择“以管理员身份运行”后重试。'
    }
}
catch {
    Write-EnvFail ('[1/6] 无法确认管理员权限：' + $_.Exception.Message + '请以管理员身份运行后重试。')
}

# [2/6] 操作系统：Windows 10/11 且 build 号满足 Docker Desktop 要求。
# 口径与 Assert-TpsSupportedWindowsHost 一致：仅 x64 客户端 Windows；
# Windows 10 要求 22H2（build >= 19045），Windows 11 要求 23H2 及更新（build >= 22631）。
try {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw '当前不是 Windows 系统，需要 Windows 10/11 客户机。'
    }
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw '当前不是 64 位操作系统，需要 64 位 Windows 10/11。'
    }
    if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
        throw '当前不是 x64 架构，需要 x64 Windows 10/11 客户机。'
    }
    $operatingSystem = Get-CimInstance -ClassName Win32_OperatingSystem
    if ([int]$operatingSystem.ProductType -ne 1) {
        throw '检测到 Windows Server，Docker Desktop 不支持服务端系统，请换用 Windows 10/11 客户端。'
    }
    $osBuild = [int]$operatingSystem.BuildNumber
    if (($osBuild -ge 22000 -and $osBuild -lt 22631) -or ($osBuild -lt 22000 -and $osBuild -lt 19045)) {
        throw ('当前系统版本过低：' + $operatingSystem.Caption + '（Build ' + $osBuild + '）。要求 Windows 10 22H2（Build >= 19045）或 Windows 11 23H2 及更新（Build >= 22631），请先更新系统。')
    }
    Write-EnvPass ('[2/6] 操作系统满足要求：' + $operatingSystem.Caption + '（Build ' + $osBuild + '）。')
}
catch {
    Write-EnvFail ('[2/6] ' + $_.Exception.Message)
}

# [3/6] WSL：商店版优先、NUL 剥离、版本三元组正则，要求 >= 2.1.5。
# 探针逻辑与 Assert-TpsSupportedWindowsHost 口径一致。
try {
    $wslCandidates = @()
    $systemWslError = ''
    if (-not [string]::IsNullOrWhiteSpace($env:ProgramFiles)) {
        $storeWsl = Join-Path $env:ProgramFiles 'WSL\wsl.exe'
        if (Test-Path -LiteralPath $storeWsl -PathType Leaf) {
            $wslCandidates += [IO.Path]::GetFullPath($storeWsl)
        }
    }
    try {
        $systemWsl = Get-TpsNativeSystemToolPath 'wsl.exe'
        if ($wslCandidates -notcontains $systemWsl) {
            $wslCandidates += $systemWsl
        }
    }
    catch {
        $systemWslError = $_.Exception.Message
    }
    $wslMatchedPath = $null
    $wslMatchedOutput = ''
    $wslFirstProbe = ''
    if ($wslCandidates.Count -eq 0) {
        $hint = '未找到任何可用的 wsl.exe 候选。'
        if (-not [string]::IsNullOrWhiteSpace($systemWslError)) {
            $hint += ('系统目录探针异常：' + $systemWslError)
        }
        throw ($hint + '请先安装 WSL 2.1.5 或更新版本。')
    }
    foreach ($candidate in $wslCandidates) {
        # 商店版 wsl.exe 向管道输出 UTF-16：先剥离 NUL 再匹配，否则版本号正则不可见。
        $probe = ((& $candidate --version 2>&1 | Out-String) -replace "\x00", "")
        if ([string]::IsNullOrWhiteSpace($wslFirstProbe)) {
            $wslFirstProbe = $probe
        }
        if ($LASTEXITCODE -eq 0 -and [regex]::Match($probe, '\d+\.\d+\.\d+').Success) {
            $wslMatchedPath = $candidate
            $wslMatchedOutput = $probe
            break
        }
    }
    if (-not $wslMatchedPath) {
        throw ('未探针到可用的 WSL 版本输出（要求 >= 2.1.5）。实际探针输出如下：' + "`r`n" + $wslFirstProbe)
    }
    $wslVersionText = [regex]::Match($wslMatchedOutput, '\d+\.\d+\.\d+').Value
    if ([Version]$wslVersionText -lt [Version]'2.1.5') {
        throw ('WSL 版本过低：当前 ' + $wslVersionText + '，要求 >= 2.1.5，请先升级 WSL。完整探针输出如下：' + "`r`n" + $wslMatchedOutput)
    }
    Write-EnvPass ('[3/6] WSL 版本满足要求：' + $wslVersionText + '（' + $wslMatchedPath + '）。')
}
catch {
    Write-EnvFail ('[3/6] ' + $_.Exception.Message)
}

# [4/6] D 盘存在且可写（创建临时文件再删除来验证）。
try {
    if (-not (Test-Path -LiteralPath 'D:\' -PathType Container)) {
        throw 'D 盘不存在，请先准备 D 盘后再运行装机流程。'
    }
    $probePath = Join-Path 'D:\' ('.tps-customer-env-write-test.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($probePath, 'tps-customer-env-write-test')
    }
    finally {
        if (Test-Path -LiteralPath $probePath) {
            Remove-Item -LiteralPath $probePath -Force
        }
    }
    Write-EnvPass '[4/6] D 盘存在且可写（临时探针文件已创建并删除）。'
}
catch {
    Write-EnvFail ('[4/6] ' + $_.Exception.Message)
}

# [5/6] Docker Desktop 是否已安装（缺失算警告，不是失败，后续装机流程会安装）。
try {
    $dockerProgramFiles = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)
    $desktopDefaultPath = ''
    if (-not [string]::IsNullOrWhiteSpace($dockerProgramFiles)) {
        $desktopDefaultPath = Join-Path $dockerProgramFiles 'Docker\Docker\Docker Desktop.exe'
    }
    $desktopExists = (-not [string]::IsNullOrWhiteSpace($desktopDefaultPath)) -and (Test-Path -LiteralPath $desktopDefaultPath -PathType Leaf)
    $dockerVersionText = ''
    $dockerCliExitCode = -1
    if ($null -ne (Get-Command docker -ErrorAction SilentlyContinue)) {
        $dockerVersionText = (((& docker --version 2>&1 | Out-String) -replace "\x00", "")).Trim()
        $dockerCliExitCode = $LASTEXITCODE
    }
    $dockerCliOk = ($dockerCliExitCode -eq 0) -and ($dockerVersionText -match '^Docker version \d+\.\d+\.\d+')
    if ($desktopExists -and $dockerCliOk) {
        Write-EnvPass ('[5/6] 已检测到 Docker Desktop：' + $dockerVersionText + '。')
    }
    else {
        $missingHints = @()
        if (-not $desktopExists) {
            $missingHints += '未找到默认安装路径下的 Docker Desktop'
        }
        if (-not $dockerCliOk) {
            $missingHints += '未能通过 docker --version 探针到 Docker'
        }
        Write-EnvWarning ('[5/6] ' + ($missingHints -join '；') + '。此项为警告：装机流程后面会安装 Docker Desktop，可以继续。')
    }
}
catch {
    Write-EnvWarning ('[5/6] Docker Desktop 检查遇到异常，按缺失处理（警告，不影响继续）：' + $_.Exception.Message)
}

# [6/6] PowerShell 版本：5.1+ 通过，7+ 最佳；低于 5.1 失败。
try {
    $powerShellVersion = $PSVersionTable.PSVersion
    if ($null -eq $powerShellVersion) {
        throw '无法获取 PowerShell 版本，要求 5.1 及以上。'
    }
    if ($powerShellVersion -lt [Version]'5.1') {
        throw ('PowerShell 版本过低：当前 ' + $powerShellVersion + '，要求 5.1 及以上。')
    }
    elseif ($powerShellVersion -ge [Version]'7.0') {
        Write-EnvPass ('[6/6] PowerShell 版本最佳：' + $powerShellVersion + '（7+）。')
    }
    else {
        Write-EnvPass ('[6/6] PowerShell 版本满足要求：' + $powerShellVersion + '（5.1+）。')
    }
}
catch {
    Write-EnvFail ('[6/6] ' + $_.Exception.Message)
}

Write-Host ''
Write-Host ("预检汇总：通过 {0} 项，警告 {1} 项，失败 {2} 项。" -f $script:PassCount, $script:WarningCount, $script:FailCount)
if ($script:FailCount -gt 0) {
    Write-Host '存在未通过项，请按上方红色提示处理后重新运行本脚本。' -ForegroundColor Red
    exit 1
}
if ($script:WarningCount -gt 0) {
    Write-Host '全部关键项通过（存在警告，不影响继续装机流程）。' -ForegroundColor Yellow
}
else {
    Write-Host '全部检查项通过。' -ForegroundColor Green
}
exit 0
