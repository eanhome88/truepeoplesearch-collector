[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string]$InstallRoot = 'D:\TruePeopleSearch'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

$root = Get-NormalizedInstallRoot $InstallRoot
Assert-TpsSupportedWindowsHost
$null = Assert-TpsProtectedDirectoryAcl $root
$environmentPath = Join-Path $root 'config\runtime.env'
$configuration = Read-TpsRuntimeEnvironment $environmentPath $root
Set-TpsProcessEnvironment $configuration
Assert-TpsBusinessStorageOnD $root
$appRoot = Get-TpsAppRoot $root

# 1) 包自验（已初始化机器必须放行顶层运行时目录）。
$bundleVerifier = Join-Path $appRoot 'deploy\windows-full\Test-FullBundle.ps1'
& $bundleVerifier -BundleRoot $root -AllowRuntimeState

# 2) 运行时完整性：与 Install-OfflineRuntime.ps1 同款三项验证。
#    任何一项不过即抛错，不写清单（缺文件就老实重装运行时）。
$pythonExe = Join-Path $root 'runtime\python\python.exe'
$venvPython = Join-Path $root 'runtime\.venv\Scripts\python.exe'
& $pythonExe -I -B -c "import platform, sys; raise SystemExit(0 if sys.version_info[:3] == (3, 12, 10) and platform.machine().lower() in {'amd64', 'x86_64'} else 1)"
if ($LASTEXITCODE -ne 0) {
    throw 'The D-drive Python runtime is not the approved Python 3.12.10 x64 target.'
}
& $venvPython -I -B -c 'import flask, mysql.connector, redis, psutil, scrapling, curl_cffi, httpx'
if ($LASTEXITCODE -ne 0) {
    throw 'The installed Python runtime failed its import check.'
}
& $venvPython -I -B -m pip --isolated check
if ($LASTEXITCODE -ne 0) {
    throw 'The installed offline Python dependency closure is inconsistent.'
}
$browserSmoke = @'
from playwright.sync_api import sync_playwright
from patchright.sync_api import sync_playwright as sync_patchright

for launcher in (sync_playwright, sync_patchright):
    with launcher() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("data:text/html,<title>offline-smoke</title>")
        if page.title() != "offline-smoke":
            raise RuntimeError("offline Chromium page smoke failed")
        browser.close()
'@
# Windows PowerShell 5.1 调用本机程序时会吃掉 -c 参数里的双引号，烟测必须落成文件再执行。
$smokePath = Join-Path $root 'runtime\temp\browser-smoke.py'
$smokeWritten = $false
try {
    [IO.File]::WriteAllText($smokePath, $browserSmoke, (New-Object Text.UTF8Encoding($false)))
    $smokeWritten = $true
    & $venvPython -I -B $smokePath
    if ($LASTEXITCODE -ne 0) {
        throw 'The pinned Chromium revision failed its offline Playwright/Patchright launch smoke.'
    }
}
finally {
    if ($smokeWritten -and (Test-Path -LiteralPath $smokePath)) {
        Remove-Item -LiteralPath $smokePath -Force
    }
}

# 3) 三项全过才重绑：枚举当前运行时文件并绑定当前包指纹。
if ($PSCmdlet.ShouldProcess((Join-Path $root 'runtime\runtime-manifest.json'), 'Rebind the managed runtime manifest to the current bundle')) {
    Write-TpsRuntimeManifest $root
    Assert-TpsRuntimeManifest $root
}

Write-Host 'Runtime manifest rebound to the current bundle.'
Write-Host 'No database, queue, dashboard, or collection job was started.'
