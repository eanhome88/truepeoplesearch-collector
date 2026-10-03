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

## 采集边界

Windows 完整包会保留采集源码，但当前只启动安全面板：Windows 版进程启停、身份校验和异常恢复尚未完成专项验收，因此不提供解锁采集按钮的参数。不应手动绕过这个边界。

在未确认数据授权、目标站规则、当前限流状态、代理配置和小样本成功率前，不应启动 worker。进程存活、端口可连或 HTTP 200 不等于正常入库；验收时必须分开检查数据库计数、队列状态和实际新增速率。
