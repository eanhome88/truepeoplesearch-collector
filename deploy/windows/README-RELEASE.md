# Windows 客户离线发布说明

这是经过白名单约束的 Windows 客户包，不是源码仓库镜像，也不支持客户机器自动拉取 Git。它只提供本机只读控制台的安全启动路径；不会自动启动后台作业、代理测试、在线更新、数据库迁移或容器。

## 交付物与发布门槛

一次交付必须包含同一版本的四个文件：

- `TruePeopleSearch-Windows.zip`
- `TruePeopleSearch-Windows.zip.sha256`
- `TruePeopleSearch-Windows-extract.ps1`
- `TruePeopleSearch-Windows-extract.ps1.sha256`

发布人必须从干净、固定的 Git 提交构建三份 Windows x64 启动器，签名（如适用）后再生成 `windows-launchers.manifest.json`。启动器清单记录的是二进制哈希、大小和来源提交；它不是代码签名证书。

发布包不得含真实配置、凭据、代理值、浏览器会话、数据库/队列内容、日志、缓存、Git 元数据或已有运行数据。

## 完整性与发布者身份是两项独立检查

SHA-256 只能证明接收到的字节与**预先通过可信渠道取得的期望哈希**相同。随 ZIP 一起收到的 `.sha256` 文件本身不证明发布者身份；不要仅因它位于同一目录就信任它。发布人应通过受控工单、已认证的发布公告或其他独立可信渠道提供 ZIP 和外部解压器的期望 SHA-256 值。外部解压器强制要求传入 ZIP 的可信期望哈希，缺少该值不会解压。

Authenticode 用于确认三份 Windows 启动器的发布者身份。客户若取得了经独立可信渠道确认的 40 位证书指纹，可在解压时传入 `-ExpectedAuthenticodeThumbprint`。校验器会拒绝未签名、无效、证书链不受信任或签名者指纹不匹配的启动器。

Authenticode 启动器校验不签署 ZIP、外部 PowerShell 解压器、Python 源码或配置模板；这些文件仍只由 SHA-256 完整性清单覆盖。未提供预期指纹时，解压仍可进行，但结果仅代表完整性已核对，**不代表发布者身份已认证**。实际 Windows 上的签名状态还取决于客户主机的证书信任与吊销策略，必须在目标环境验收。

## 发布工程流程

在受控构建机的干净发布提交上，将启动器构建到源码目录外的空目录：

```bash
scripts/build_windows_launchers.sh /secure-artifacts/tps-launchers
```

如发布策略要求 Authenticode，先签名三份 EXE，再刷新其来源清单；签名后的哈希必须进入清单：

```bash
scripts/write_windows_launcher_manifest.py \
  --repo-root . --exe-dir /secure-artifacts/tps-launchers --overwrite
```

最后生成 ZIP、ZIP 哈希、外部解压器和解压器哈希：

```bash
scripts/package_windows_release.py \
  --repo-root . \
  --exe-dir /secure-artifacts/tps-launchers \
  --output /secure-delivery/TruePeopleSearch-Windows.zip
```

工具会拒绝脏工作区、源码目录内的启动器工件、根目录旧 EXE、未知文件或与当前提交不一致的启动器。它会校验 PE32+ AMD64 结构，而不是只检查 `MZ` 文件头。工具不会联网、启动服务、构建依赖或部署到客户机器。

发布人还应在可信发布记录中给出：版本、Git 提交、ZIP 的期望 SHA-256、外部解压器的期望 SHA-256，以及（如已签名）预期 Authenticode 证书指纹。不要把这些值只放在可被同一传输链替换的 ZIP 目录中。

## 客户机器上的验证和解压

先用 Windows PowerShell 计算 ZIP 和外部解压器的 SHA-256，并与可信发布记录中的期望值逐字符比对：

```powershell
Get-FileHash C:\Delivery\TruePeopleSearch-Windows.zip -Algorithm SHA256
Get-FileHash C:\Delivery\TruePeopleSearch-Windows-extract.ps1 -Algorithm SHA256
```

上述命令只输出完整性哈希，不会验证发布者身份。确认无误后，使用外部解压器解压到一个不存在的新目录。以下示例同时要求三份启动器的 Authenticode 签名者与已确认的证书指纹一致：

```powershell
& C:\Delivery\TruePeopleSearch-Windows-extract.ps1 `
  -PackagePath C:\Delivery\TruePeopleSearch-Windows.zip `
  -Destination C:\TPS\versions\candidate `
  -ExpectedPackageSha256 '在此替换为可信发布记录中的64位ZIP哈希' `
  -ExpectedAuthenticodeThumbprint '0123456789ABCDEF0123456789ABCDEF01234567'
```

ZIP 哈希必须替换为本次可信发布记录中的连续 64 位十六进制字符；它不是随 ZIP 同目录 `.sha256` 文件的替代品，而是独立确认后的值。指纹值必须替换为本次发布记录中的真实值，且必须是连续的 40 位十六进制字符。若本次发布未签名，不要伪造或猜测指纹；不传该参数时系统会明确提示“未执行发布者认证”。

解压器会核对 ZIP 的 SHA-256，以及包内每个文件（包括启动器来源清单）的哈希；它只接受一个不存在的新目标目录，失败时不会替换现有版本。传入预期指纹时，它会把该值传给包内校验器并强制 Authenticode 验证。

如需在已产生运行状态的版本目录中复验文件，可由授权运维人员使用：

```powershell
& C:\TPS\versions\candidate\deploy\windows\Verify-Release.ps1 `
  -ReleaseRoot C:\TPS\versions\candidate `
  -AllowRuntimeState `
  -ExpectedAuthenticodeThumbprint '0123456789ABCDEF0123456789ABCDEF01234567'
```

`-AllowRuntimeState` 只放宽根目录本机 `.env`、`.venv`、运行数据、日志及 Python 生成的 `__pycache__` 文件；其余发布文件仍逐项核对 SHA-256，指定的启动器仍会做 Authenticode 验证。符号链接和其他未知文件仍会被拒绝。

## 首次安装与验收

这个离线升级包不包含 Python 依赖 wheelhouse、浏览器组件或容器镜像，也不会自行创建或迁移数据库。首次安装必须由授权人员在独立的主机准备流程中完成，并使用经过审核的 Windows wheelhouse 和客户确认的本机数据库/Redis。不要运行旧的在线安装、在线检查或自动更新脚本。

准备完成后，先运行只读检查：

```powershell
& C:\TPS\versions\candidate\deploy\windows\Test-HostReadiness.ps1
```

通过后仅可使用 `TruePeopleSearch.exe` 或 `start_client.bat` 启动本机控制台。启动器会强制客户安全模式和回环地址，并在打开浏览器前确认服务属于本次启动；页面中的写入、后台控制、代理测试和在线更新接口会被拒绝。由授权人员以 `.env` 的 `TPS_DASHBOARD_PORT` 验证：

```powershell
$dashboardPort = 5001 # 替换为 .env 中的 TPS_DASHBOARD_PORT
curl.exe -fsS "http://127.0.0.1:$dashboardPort/api/health"
curl.exe -fsS "http://127.0.0.1:$dashboardPort/api/ready"
curl.exe -fsS "http://127.0.0.1:$dashboardPort/api/system/version"
```

`Test-HostReadiness.ps1` 会只读、非执行地读取发布根目录 `.env` 中的 `TPS_DB_PORT` 与 `TPS_REDIS_PORT`；未设置时才使用 4000/6379。若需临时覆盖，可显式传入合法端口，例如 `-DatabasePort 4000 -RedisPort 6379`。配置文件为符号链接、过大、重复或端口非法时，检查会失败且不会启动任何服务。

客户停止器只会停止已验证为本次客户启动的本机 Dashboard 进程树；身份无法确认时会拒绝操作，不会按进程名或普通 PID 猜测目标。

## 升级与回滚

每次升级都解压到新的版本目录，保留旧程序目录和已验证的数据快照。只有可信哈希比对、清单验证、可选的发布者认证、本机就绪检查和客户验收全部通过后，才能由客户授权人员切换启动目录。任何一步失败时都保留旧版本；不要用 `git pull`、覆盖目录或页面内升级按钮重试。
