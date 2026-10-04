Start-Collector 报 "did not claim its PID file"，我需要现场证据才能定位。下面是一段只读为主的诊断脚本，它会用和 Start-Collector 完全相同的方式把 supervisor 拉起来观察 50 秒，然后把这个探针进程树干净杀掉（这是我授权的例外，只杀探针自己启动的 PID 树）。跑完把全部输出原样贴给我，不要总结。

```powershell
$ErrorActionPreference = 'Continue'
$root = 'D:\truepeoplesearch'
$appRoot = Join-Path $root 'app'
. (Join-Path $appRoot 'deploy\windows-full\Common.ps1')
Write-Host '=== 1. file fingerprints (first 16 hex) ==='
foreach ($f in @('scripts\tps_supervisor.py','scripts\tps_control.py','deploy\windows-full\Start-Collector.ps1','deploy\windows-full\Stop-Collector.ps1')) {
    $p = Join-Path $appRoot $f
    $h = (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant().Substring(0,16)
    Write-Host "$h $f"
}
Write-Host '=== 2. old-path pid files? ==='
Get-ChildItem -LiteralPath (Join-Path $appRoot 'data') -Filter 'supervisor.pid*' -Force -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "OLD $($_.FullName) $($_.Length) $($_.LastWriteTime)" }
Get-ChildItem -LiteralPath (Join-Path $root 'runtime') -Filter 'supervisor.pid*' -Force -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "RT  $($_.FullName) $($_.Length) $($_.LastWriteTime)" }
Write-Host '=== 3. collector.out.log tail 60 ==='
Get-Content -LiteralPath (Join-Path $root 'logs\app\collector.out.log') -Tail 60 -ErrorAction SilentlyContinue
Write-Host '=== 4. venv python pid test ==='
$python = Get-TpsPythonPath $root
$pidOut = 'D:\tps-collector\pidtest.txt'
$t = Start-Process -FilePath $python -ArgumentList @('-I','-B','-c','import os,sys,time;print(os.getpid(),sys.executable,flush=True);time.sleep(3)') -PassThru -WindowStyle Hidden -RedirectStandardOutput $pidOut
$t.WaitForExit()
Write-Host ("launched PID={0} ; python says: {1}" -f $t.Id, ((Get-Content -LiteralPath $pidOut -Raw).Trim()))
Write-Host '=== 5. supervisor probe (50s) ==='
$configuration = Read-TpsRuntimeEnvironment (Join-Path $root 'config\runtime.env') $root
Set-TpsProcessEnvironment $configuration -IncludeServiceCredentials
$env:TPS_RELEASE_MODE = 'customer-collector'
$env:TPS_CONCURRENCY = '4'
$supOut = 'D:\tps-collector\probe.out.log'; $supErr = 'D:\tps-collector\probe.err.log'
Remove-Item $supOut,$supErr -Force -ErrorAction SilentlyContinue
$args = @('-I','-X','utf8','-B','-u',(Join-Path $appRoot 'scripts\tps_supervisor.py'),'start','--no-dashboard','--concurrency','4')
$sup = Start-Process -FilePath $python -ArgumentList $args -WorkingDirectory $appRoot -WindowStyle Hidden -RedirectStandardOutput $supOut -RedirectStandardError $supErr -PassThru
Write-Host ("probe launched PID={0}" -f $sup.Id)
for ($i = 1; $i -le 25; $i++) {
    Start-Sleep -Seconds 2
    $sup.Refresh()
    $alive = -not $sup.HasExited
    $files = Get-ChildItem -LiteralPath (Join-Path $root 'runtime') -Filter 'supervisor.pid*' -Force -ErrorAction SilentlyContinue | ForEach-Object {
        $c = ''
        if ($_.Name -eq 'supervisor.pid') { try { $c = (Get-Content -LiteralPath $_.FullName -Raw -ErrorAction Stop).Trim() } catch { $c = "ERR:$($_.Exception.Message)" } }
        "$($_.Name)[$c]"
    }
    $kids = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($sup.Id)" -ErrorAction SilentlyContinue | ForEach-Object { "$($_.ProcessId):$($_.Name)" })
    Write-Host ("t={0,2}s alive={1} files={2} children={3}" -f ($i*2), $alive, ($files -join ' '), ($kids -join ','))
    if (-not $alive) { break }
}
Write-Host '--- probe.out.log ---'; Get-Content -LiteralPath $supOut -Tail 40 -ErrorAction SilentlyContinue
Write-Host '--- probe.err.log ---'; Get-Content -LiteralPath $supErr -Tail 40 -ErrorAction SilentlyContinue
Write-Host '=== 6. cleanup probe tree ==='
if (-not $sup.HasExited) { & taskkill.exe /PID $sup.Id /T /F | Out-Host }
Start-Sleep -Seconds 2
Get-ChildItem -LiteralPath (Join-Path $root 'runtime') -Filter 'supervisor.pid*' -Force -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "LEFT $($_.Name) $($_.Length)" }
Write-Host 'DIAG DONE'
```

跑完后不要再启动采集，等我看完输出给下一步。
