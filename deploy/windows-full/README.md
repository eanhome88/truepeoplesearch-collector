# Windows x64 完整运行包（D 盘）

这套脚本将完整项目固定为一种部署方案：

- Windows 10 22H2（build 19045）或 Windows 11 23H2（build 22631）及更新的受支持 x64 客户端；
- WSL 2.1.5 或更新；Windows Server 2019/2022 不在 Docker Desktop 官方支持范围，脚本会在修改主机前拒绝继续；
- 客户现场需自行确认其 Docker Desktop 商业许可满足 Docker 的现行条款；
- 根目录固定为 `D:\TruePeopleSearch`，不会默认回退到 C 盘；
- MySQL 8.4.11 + Redis 7.4.8，不再在 TiDB/MySQL 和不同 Redis 版本之间自动切换；
- 数据位于 `D:\TruePeopleSearch\data`，日志位于 `D:\TruePeopleSearch\logs`，备份位于 `D:\TruePeopleSearch\backups`；
- MySQL/Redis 仅绑定 `127.0.0.1`；
- 默认启动数据库、Redis 和本机安全面板，不启动 feeder、worker、代理测试或任何采集任务。

## 完整包必须包含

`scripts/package_windows_full_bundle.py` 从显式白名单打包应用文件，并要求外部 `vendor-manifest.json` 提供以下经审核的 Windows x64 资产：

1. Python 3.12.10 x64 安装程序（必须记录 Authenticode 证书指纹）；
2. Docker Desktop x64 安装程序（必须记录 Authenticode 证书指纹）；
3. 完整 Windows x64 wheelhouse；
4. Chromium `chromium-1243.zip`；
5. 分别标记为 `tps-offline/mysql:8.4.11-amd64` 和 `tps-offline/redis:7.4.8-alpine-amd64` 的 Linux/amd64 离线镜像 tar。

只有哈希清单不能证明发布者身份；Windows 验收还会核对安装程序的 Authenticode 证书指纹。代理账号、密码、`.env`、数据库、Redis 队列、日志和旧运行目录均不得进入发布包。

Docker Desktop 记录还必须包含 `desktop_product_version`、`docker_cli_version`、`compose_version` 和 `engine_version` 四个精确版本。安装完成后脚本会固定检查受保护的 Program Files 路径、发布者证书和这四个版本；Docker Desktop 被自动升级或替换后会停止验收，直到新版本重新审核并生成新包。

## 发布工程师打包

只能从已审核且干净的 Git 提交生成客户包；供应商清单必须固定为供应商目录根部的 `vendor-manifest.json`：

```powershell
python scripts\package_windows_full_bundle.py `
  --repo-root . `
  --vendor-root D:\ReviewedVendor `
  --vendor-manifest D:\ReviewedVendor\vendor-manifest.json `
  --output D:\Release\TruePeopleSearch-Full-Windows.zip
```

打包器只接受显式应用白名单，把每个供应商输入先复制到一次性 staging 后再校验和写包，并生成同名 `.sha256` 文件。`--allow-dirty` 仅供内部诊断；此类包的 `source_dirty=true`，客户机上的 `Test-FullBundle.ps1` 会明确拒绝。

## 安装顺序

1. 先用可信的独立渠道获得 ZIP 的 SHA-256，再与 `Get-FileHash` 结果比对。ZIP 旁边的 `.sha256` 文件本身不是独立信任根。哈希通过后，把 ZIP 解压到空的 `D:\TruePeopleSearch`。
2. 先执行只读验证：

   ```powershell
   powershell -ExecutionPolicy Bypass -File D:\TruePeopleSearch\app\deploy\windows-full\Test-FullBundle.ps1
   ```

3. 初始化 D 盘目录和本机密钥：

   ```powershell
   powershell -ExecutionPolicy Bypass -File D:\TruePeopleSearch\app\deploy\windows-full\Initialize-Runtime.ps1
   ```

4. Docker Desktop 安装属于主机变更，需由现场授权人员确认并在“以管理员身份运行”的 PowerShell 中执行。用已核验的 Docker 签名指纹执行：

   ```powershell
   powershell -ExecutionPolicy Bypass -File D:\TruePeopleSearch\app\deploy\windows-full\Install-DockerDesktop.ps1 `
     -ExpectedDockerSignerThumbprint <40位已核验指纹>
   ```

   该脚本使用 Docker 官方 `--wsl-default-data-root` 参数将 WSL 数据磁盘定位到 `D:\TruePeopleSearch\runtime\docker-data`。首次启动 Docker Desktop 并完成许可协议确认后，后续安装、启动和验收脚本都会强制检查 D 盘下的 WSL 虚拟磁盘、本机 Linux 命名管道及 Docker Desktop 自带且签名匹配的 CLI/Compose，不通过就停止。
5. 使用已核验的 Python 签名指纹安装离线运行时，并加载 amd64 镜像：

   ```powershell
   powershell -ExecutionPolicy Bypass -File D:\TruePeopleSearch\app\deploy\windows-full\Install-OfflineRuntime.ps1 `
     -ExpectedPythonSignerThumbprint <40位已核验指纹>
   ```

   Python、wheelhouse 和两套 Chromium 目录会生成逐文件运行时清单；该清单同时绑定完整 `bundle-manifest.json` 的 SHA-256。替换应用包或供应商资产后必须重新安装运行时，不能沿用旧清单。

6. 启动基础服务和安全面板：

   ```powershell
   powershell -ExecutionPolicy Bypass -File D:\TruePeopleSearch\app\deploy\windows-full\Start-Stack.ps1
   ```

7. 运行完整验收：

   ```powershell
   powershell -ExecutionPolicy Bypass -File D:\TruePeopleSearch\app\deploy\windows-full\Test-Stack.ps1
   ```

8. 停止面板（默认不停 MySQL/Redis）：

   ```powershell
   powershell -ExecutionPolicy Bypass -File D:\TruePeopleSearch\app\deploy\windows-full\Stop-Stack.ps1
   ```

需同时停止基础服务时，显式增加 `-StopInfrastructure`。该操作不删除数据卷、队列、日志或备份。

## 一键启动（推荐给客户）

环境装好后，客户日常只需要一个脚本：按顺序拉起基础栈和采集，全中文提示：

```powershell
powershell -ExecutionPolicy Bypass -File D:\TruePeopleSearch\app\deploy\windows-full\Start-Customer.ps1
```

- 基础栈已在跑就跳过，不会重复启动；
- 没配代理会停下来告诉客户往 `config\runtime.env` 加哪一行，面板不受影响；
- 只要面板不要采集时加 `-NoCollection`。

## 采集

基础栈默认只启动安全面板，不启动采集。采集是独立的、已评审的操作，需要客户现场先配好代理再手动启动：

1. 先完成上面的 1–7 步，`Test-Stack.ps1` 通过；
2. 把代理填进 `D:\TruePeopleSearch\config\runtime.env`（至少填 `CLOUDBYPASS_PROXY`、`PROXY_TUNNEL`、`PROXY_FILE`、`PROXY_API_URL` 其中一项，上游 API 会在启动时拉取并定时补充新出口；可选 `TPS_CONCURRENCY=1-64`，默认 4，8 以上为高吞吐模式，先按主机 CPU/内存 sizing；自研过 CF 时配 `TPS_OWN_CF=1` 加 `TPS_CF_SOLVER`，契约见 `docs/1M-runbook.md` §3.1，缺 `TPS_CF_SOLVER` 会拒绝启动）。代理凭据只存在客户本机，永远不会进发布包；
3. 启动采集：

   ```powershell
   powershell -ExecutionPolicy Bypass -File D:\TruePeopleSearch\app\deploy\windows-full\Start-Collector.ps1
   ```

   该脚本会先确认面板与 MySQL/Redis 健康、代理已配置，再以 `customer-collector` 模式启动 supervisor（只带 worker，不重起面板），进程记录落在 `runtime\collector-process.json`；
4. 停止采集（队列、数据、日志都保留，基础栈继续跑）：

   ```powershell
   powershell -ExecutionPolicy Bypass -File D:\TruePeopleSearch\app\deploy\windows-full\Stop-Collector.ps1
   ```

在未确认数据授权、目标站规则、当前限流状态、代理配置和小样本成功率前，不应启动采集。进程存活、端口可连或 HTTP 200 不等于正常入库；验收时必须分开检查数据库计数、队列状态和实际新增速率。
