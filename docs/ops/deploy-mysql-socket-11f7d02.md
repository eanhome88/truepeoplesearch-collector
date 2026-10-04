# MySQL socket 检查修复：转发给客户机 Cline

日期：2026-10-05。本文只更新一个已经存在的应用文件 `Common.ps1`。
原采集代码 pin `11c746b` 不变；该文件更新到已通过 Windows PowerShell 5.1 原生测试的 pin `11f7d028f8b16477c716ae5282a9c3ecc5916fae`。

例外只允许 `D:\TruePeopleSearch\data\mysql\mysql.sock`，要求非目录、长度 0、真实 reparse tag 为 `0x80000023`。查询失败、符号链接、junction、其他名称或位置仍拒绝。不会删除 socket 或业务数据。

请 Cline 以管理员 VS Code 执行下面整段。只下载已固定版本源码，校验 ZIP、文件和现有清单，备份后覆盖一个文件，更新其清单，再运行官方校验脚本。不改 runtime.env，不打印凭据，不启动采集。若采集正在运行，先用官方脚本停止；验收成功后回报，由用户协调后续采集。不要重跑旧步骤 A，它会覆盖回旧 Common.ps1。

```powershell
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator VS Code is required.' }

$root = 'D:\truepeoplesearch'
$common = Join-Path $root 'app\deploy\windows-full\Common.ps1'
$manifestPath = Join-Path $root 'bundle-manifest.json'
$oldSha = '437c98e6664d13fe274f9eae7372e9d967ddf1d62a56ae3066b7c7822af4011c'
$newSha = '4bdf4e37eeaa82f5a81ab544db0f50f36161d74f3b0366d529e3b79d6b1dbb8f'
$newSize = 39599
$pin = '11f7d028f8b16477c716ae5282a9c3ecc5916fae'
$zipSha = '75f1c5b34ff4f75017ce09917f012cfa68dacc476b5425023afc4e19122210a1'

. $common
Assert-NoReparsePoint $common
Assert-NoReparsePoint $manifestPath
$actual = (Get-FileHash -LiteralPath $common -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $oldSha -and $actual -ne $newSha) { throw 'Existing Common.ps1 differs from both reviewed versions; report its hash and stop.' }
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$entry = @($manifest.files | Where-Object { $_.path -eq 'app/deploy/windows-full/Common.ps1' })
if ($entry.Count -ne 1 -or $entry[0].sha256 -ne $actual) { throw 'Existing Common.ps1 manifest entry is missing, duplicated or mismatched.' }

$stage = Join-Path 'D:\tps-collector' ('socket-fix-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage -Force | Out-Null
Assert-NoReparsePoint $stage
$zip = Join-Path $stage 'pin.zip'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -UseBasicParsing -Uri ("https://github.com/eanhome88/truepeoplesearch-collector/archive/" + $pin + ".zip") -OutFile $zip
if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $zipSha) { throw 'Pinned ZIP hash mismatch.' }
Expand-Archive -LiteralPath $zip -DestinationPath $stage
$source = Join-Path $stage ("truepeoplesearch-collector-" + $pin + "\deploy\windows-full\Common.ps1")
if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $newSha -or
    (Get-Item -LiteralPath $source).Length -ne $newSize) { throw 'Pinned Common.ps1 hash or size mismatch.' }

if (Test-Path -LiteralPath (Join-Path $root 'runtime\collector-process.json')) {
    & (Join-Path $root 'app\deploy\windows-full\Stop-Collector.ps1') -Confirm:$false
}
$backup = Join-Path $stage 'backup'
New-Item -ItemType Directory -Path $backup | Out-Null
Copy-Item -LiteralPath $common -Destination (Join-Path $backup 'Common.ps1')
Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $backup 'bundle-manifest.json')
$runtimeManifest = Join-Path $root 'runtime\runtime-manifest.json'
if (Test-Path -LiteralPath $runtimeManifest) { Copy-Item -LiteralPath $runtimeManifest -Destination (Join-Path $backup 'runtime-manifest.json') }

Copy-Item -LiteralPath $source -Destination $common -Force
if ((Get-FileHash -LiteralPath $common -Algorithm SHA256).Hash.ToLowerInvariant() -ne $newSha) { throw 'Destination hash mismatch.' }
$entry[0].sha256 = $newSha
$entry[0].size = $newSize
[IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 12), (New-Object Text.UTF8Encoding($false)))

. $common
Assert-TpsStorageDirectoryTree (Join-Path $root 'data\mysql')
& (Join-Path $root 'app\deploy\windows-full\Test-FullBundle.ps1') -BundleRoot $root -AllowRuntimeState
& (Join-Path $root 'app\deploy\windows-full\Repair-RuntimeBinding.ps1') -Confirm:$false
& (Join-Path $root 'app\deploy\windows-full\Test-Stack.ps1')
Write-Host ("SOCKET_PATCH_OK sha={0} size={1} backup={2}" -f $newSha, $newSize, $backup)
```

回传 `SOCKET_PATCH_OK` 和三项官方校验结果。如果失败，回传报错以及 socket 的类型/tag/长度，停止，不删除数据、不改代理、不重跑旧部署步骤。该修复通过 Windows CI 不代表客户机已应用通过。

## 采集回执的必要修正

旧步骤 B 的 `Get-SuccessTotal` 用名称包含 success 的顶层字段匹配，会误把 `success_rate_pct` 当作累计数。
实际累计数位于 `metrics.counters.success`。仓库中的 `docs/ops/deploy-11c746b.md` 已修正，同时显式加载现场 runtime.env 到当前进程（不打印）、检测指标读取失败、计数归零及每个窗口是否真的增长。使用更新后的步骤 B，不要再复制桌面的旧 v3 文本。

`LOG200` 是滚动日志行计数，不能当请求数或挑战率。`cf` 和 `rl` 多只能提示需要诊断，不能证明唯一原因或排除代码。`METRICS_PROGRESS` 只代表指标持续增长；还需要客户 MySQL 的新增记录数、最新写入时间和同一窗口的流量增量，才能确认业务入库与每个成功页面的实际流量。未收到这些回执前，不宣称稳定采集、日采百万或已完成交付。
