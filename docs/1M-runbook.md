# 日 100 万页运行手册（1M-runbook）

> 分支：`main` ｜ 仓库：`/Users/wangqi/Desktop/22`
> 适用链路：**单机强机 + 自研过 CF 轻量链路（`page_sec ≈ 3~4`）是首选**。
> 契约优先说明：环境变量 `TPS_OWN_CF / TPS_IP_REST_SEC / TPS_SESSION_RECYCLE / TPS_FP_REST_SEC / TPS_LANE_WARM_SEC`
> 由兄弟智能体正在落地，若与现状代码有出入，**以契约为准**，代码侧以 `scripts/tps_scale.py`、
> `scripts/distributed_worker.py`、`scripts/proxy_pool.py` 的已提交定义为参照。

## 1. 目标与数学

### 1.1 公式（出自 `scripts/tps_scale.py`）

```python
need_pages = ceil(1_000_000 / 86400 * page_sec)   # browsers_for()：目标并发页
chrome_proc = ceil(need_pages / TABS_PER_CHROME)   # chrome_process_count()，TABS_PER_CHROME = 4
capacity    = browsers / page_sec * 86400          # daily_capacity()
```

- 基准速率：`1_000_000 / 86400 ≈ 11.57 页/秒`。
- 单机预算（`host_browser_budget()`，16 核 32G 主机）：
  - 内存侧：`(32 − 4) // 0.6 = 46`（`PER_BROWSER_GB=0.6`，`RESERVE_GB=4`）；
  - CPU 侧：`16 × 3 = 48`（`BROWSERS_PER_CORE=3`）；
  - 上限 `MAX_BROWSER_CONCURRENCY=128`，取最小值 → **单机预算 46 并发页**。
- `resolve_browsers()` 规则：不传 `--concurrency` 按 `min(need, budget)` 拉起；
  显式传值会被钳制到 `MAX_BROWSER_CONCURRENCY=128`（钳制时打 `[SCALE]` 日志）。

### 1.2 三档 page_sec 下的需求

| page_sec | 并发页 need | Chrome 进程数（÷4 上取整） | 单机 46 页日能力 |
| --- | --- | --- | --- |
| 3（自研 CF 轻量链路） | `ceil(11.57×3)` = **35** | **9** | 46/3×86400 ≈ **132 万** ✅ 单机可吃下 |
| 4（自研 CF 轻量链路） | `ceil(11.57×4)` = **47** | **12** | 46/4×86400 ≈ **99.4 万** ⚠️ 单机差 1 页，基本贴线 |
| 8（暖浏览器默认 `WARM_PAGE_SEC=8`） | `ceil(11.57×8)` = **93** | **24** | 46/8×86400 ≈ **49.7 万** ❌ 单机不够 |

### 1.3 1 机 / 2 机 / 4 机配置表（主机均为 16 核 32G，单机预算 46）

| 档位 | 主机数 | page_sec | 总并发页 | 每机 `--concurrency` | 每机 Chrome≈ | 合计日能力 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 机（**首选**） | 1 | 3 | 35 | 35 | 9 | ≈132 万 | 自研 CF 轻量链路，单机强机搞定，留约 30% 余量 |
| 2 机 | 2 | 4 | 47 | 24 + 23（或每机 24） | 6 | ≈199 万 | page_sec 退化到 4 时的稳妥档，每机内存 24×0.6+4≈18.4G |
| 4 机 | 4 | 8 | 93 | 每机 24（合计 96） | 6 | ≈199 万 | 最重链路（page_sec=8）兜底档；2 机在此档合计 92 页贴线，故取 4 机留余量 |

说明：

- `--concurrency` 即该进程的长连接浏览器数（`≈` 并发页），多机时各机数值相加 ≥ 总并发页即可。
- 自研 CF 链路把 `page_sec` 从 8 压到 3~4，是“机器数 ÷4”的关键，不要用堆机器代替优化链路。
- 上游池子行数建议 ≥ 单机 `--concurrency × 2`（一条 IP 绑一个浏览器，429 后要冷却，池子得有轮换余量，见 §2）。

## 2. 代理来源（首选 `PROXY_API_URL` 上游拉取，`PROXY_FILE` 为备选 pin 住）

客户机从上游供应商 API 拉新出口，启动拉一次、运行中每 `PROXY_API_REFRESH_SEC`
（默认 600、下限 120）热合并进池，不停机。API 返回文本（每行一个）或 JSON
（list，或 data/list/proxies/ips/hosts/results 下的数组，元素为字符串或
{host,port,user,pass} 对象）都吃；拉失败用本地缓存
`data/proxy_api_cache.txt` 顶上，无缓存则拒绝启动。

- 每行一个代理 URL，`http(s)://user:pass@host:port` 或 `socks5(h)://…`；
- 空行跳过，`#` 开头为注释；无 scheme 时自动补 `http://`；
- 美国独立 IP：各行 host 各不相同（`shared_host` 为 False 才是真独立出口）；
- 去重：重复 URL 只保留一条；**不要使用隧道网关地址**（隧道每次请求换出口，做不了粘性绑定）。

示例（`C:\tps\proxies\us-dedicated.txt`，账号密码已脱敏，3 行示例，按需扩到 §1.3 建议行数）：

```text
# 美国独立 IP，每行一个代理 URL（user:pass 已脱敏，真实文件填真实密码）
http://tps-user-01:****@us-ip-01.example.invalid:8080
http://tps-user-02:****@us-ip-02.example.invalid:8080
http://tps-user-03:****@us-ip-03.example.invalid:8080
```

校验（只读，不改代码）：行数 `find /c /v ""`（Win）/ `grep -cve '^\s*(#|$)'`，
且各行 host 互不相同。

## 3. 环境变量表

| 变量 | 代码默认值 | 1M 建议值 | 说明 |
| --- | --- | --- | --- |
| `PROXY_API_URL` | 无 | 上游供应商 API 地址（客户机本地配，永不进包） | 启动拉取 + 定时热合并；失败用缓存顶，无缓存拒绝启动 |
| `PROXY_API_REFRESH_SEC` | `600`（下限 120） | `600` | 上游拉取间隔秒数 |
| `PROXY_API_CACHE` | `data/proxy_api_cache.txt` | 默认 | 上游出口本地缓存（包外，不进发布包） |
| `TPS_OWN_CF` | `0` | 求解器接好后才填 `1` | 自研过 CF：**不开浏览器**，凭证来自 `TPS_CF_SOLVER`，页面走 curl_cffi 同代理同 TLS 指纹；没配求解器拒绝启动 |
| `TPS_CF_SOLVER` | 无 | `module:callable` / `http://…/solve` / `cmd:…` | 自研求解器入口，契约见 §3.1 |
| `TPS_CF_SOLVER_TIMEOUT` | `45` | `45` | 单次求解超时秒数 |
| `TPS_IMPERSONATE` | 按求解器 UA 自动挑 | 不配 | 强制 curl_cffi TLS 目标（如 `chrome131`），只在 UA 版本和指纹对不上时用 |
| `USE_CLOUDBYPASS` | `1` | `0` | 不用穿云网关，走自研 CF 轻量链路 |
| `PROXY_FILE` | 无（显式文件优先于 API） | 不配（配了会压住 API） | pin 住某批 IP 才用，日常走 API |
| `TPS_CONCURRENCY` | `4`（启动脚本上限 64，8 以上告警） | sizing 探针值（≤48） | 按主机 CPU/内存 + 出口数三取小 |
| `TPS_CLAIM_GAP_SEC` | `3`（限流多了自适应拉大，封顶 18） | `0.5`（单机）/`3`（多机） | 领取间隔基线秒数 |
| `TPS_IP_REST_SEC` | `4200`（下限 60） | `1800` | 单 IP 429 后冷却秒数 |
| `TPS_SESSION_RECYCLE` | `120`（下限 20） | `120` | 会话复用页数，超限重建防指纹衰减 |
| `TPS_FP_REST_SEC` | `45` | `45` | 指纹/验证码死亡本组短休，全局不停 |
| `TPS_LANE_WARM_SEC` | `15` | `15` | 换到备用出口后本组热身秒数 |
| `TPS_REDIS_HOST` / `TPS_REDIS_PORT` / `TPS_REDIS_PASSWORD` | `127.0.0.1:6379` | 按实际 Redis | 多机必须指向**同一个** Redis（见 §5） |

限流策略（已落地）：

- 有备用独立出口 → 只换道（`cool()` 改绑休息好的 IP），**不停全局**；
- 无备用出口 → 全局等（`seconds_until_ready()`，最多 600 秒）后重试；
- 验证码 → 同 IP 重刷指纹（连击才重开浏览器），不消耗备用 IP；
- 隧道拆分的多道（同 host）→ 仍按账号总量暂停 `20/40/60` 秒。

### 3.1 自研过 CF 求解器契约（`TPS_OWN_CF=1`）

worker 不开浏览器。每个组首次抓取前向求解器要一份凭证，之后所有页面用 curl_cffi
在**同一条代理**上发请求，TLS 指纹按凭证里的 Chrome 版本挑。凭证被拒（403/503/挑战页）
重解一次；再被拒按验证码上报：本组休 `TPS_FP_REST_SEC`，连续两次换出口。
`TPS_SESSION_RECYCLE` 页后整组重建、重新要凭证。

三种接法，收到的参数完全一样：

| 写法 | 例子 | 调用方式 |
| --- | --- | --- |
| Python | `TPS_CF_SOLVER=my_cf.solver:solve` | `solve(url=, proxy=, user_agent=, timeout=)`，同步/异步都行 |
| HTTP | `TPS_CF_SOLVER=http://127.0.0.1:9000/solve` | `POST` JSON 正文 |
| 命令行 | `TPS_CF_SOLVER=cmd:D:\tools\cf.exe --json` | stdin 收 JSON，stdout 回 JSON，exit 0 |

请求：

```json
{"url": "https://www.truepeoplesearch.com/find/person/xxx",
 "proxy": "http://user:pass@gw:1288",
 "user_agent": null,
 "timeout": 45}
```

`proxy` 是本组绑定的出口，`cf_clearance` 绑 IP，求解器**必须走同一出口**。
`user_agent` 为 `null` 是首解，求解器自选 UA；非空是重解，请沿用它。

响应（字段名宽松；可以整体包在 `data`/`result` 里）：

```json
{"cookies": {"cf_clearance": "...", "__cf_bm": "..."},
 "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ... Chrome/131.0.0.0 Safari/537.36",
 "html": "可选：求解器已经拿到该 url 正文就直接给，省一次请求",
 "ttl": 1500}
```

`cookies` 也接受 `[{"name":..,"value":..}]` 列表或 `"k=v; k2=v2"` 字符串。
`{"ok": false, "error": "..."}` 视为失败。既无 `cookies` 也无 `html` 视为失败。

凭证会同时按 `unblocker:warmed:{host}:{sid}` 发布到 Redis，`protocol_fetcher.py` 的协议 fleet 可直接复用。

## 4. 三档启动命令

公共环境（每台机器 PowerShell，先设好再起 worker）：

```powershell
$env:USE_CLOUDBYPASS = "0"
# 求解器没接好之前保持 0（内置浏览器过 CF）；接好后改 1 并配 TPS_CF_SOLVER
$env:TPS_OWN_CF = "0"
# $env:TPS_CF_SOLVER = "http://127.0.0.1:9000/solve"
$env:TPS_CLAIM_GAP_SEC = "3"
$env:TPS_IP_REST_SEC = "4200"
$env:TPS_SESSION_RECYCLE = "120"
$env:TPS_FP_REST_SEC = "300"
$env:TPS_LANE_WARM_SEC = "120"
```

### 4.1 一机档（首选，page_sec≈3）

```powershell
python scripts/distributed_worker.py --mode worker `
  --concurrency 35 --target-per-day 1000000 --page-sec 3 `
  --proxy-file C:\tps\proxies\us-dedicated.txt
```

### 4.2 二机档（page_sec≈4，每机一条）

```powershell
# 主机 A
python scripts/distributed_worker.py --mode worker `
  --concurrency 24 --target-per-day 1000000 --page-sec 4 `
  --proxy-file C:\tps\proxies\us-dedicated.txt
# 主机 B（同 Redis，见 §5）
python scripts/distributed_worker.py --mode worker `
  --concurrency 23 --target-per-day 1000000 --page-sec 4 `
  --proxy-file C:\tps\proxies\us-dedicated.txt
```

### 4.3 四机档（page_sec≈8，每机一条，共 4 台）

```powershell
# 主机 A/B/C/D 各一条（同 Redis），每机：
python scripts/distributed_worker.py --mode worker `
  --concurrency 24 --target-per-day 1000000 --page-sec 8 `
  --proxy-file C:\tps\proxies\us-dedicated.txt
```

注意：`--target-per-day` 只影响 `[SCALE]` 日志里的规划提示，不限速；
`--concurrency` 缺省则按本机预算拉起（16C32G 上即 46），显式传值超出 128 会被钳制。

## 5. Redis / DB / 队列注意事项

- **多机同 Redis**：所有 worker 的 `TPS_REDIS_HOST/PORT/PASSWORD` 指向同一实例；
  队列键 `tps:pending / tps:processing / tps:job:* / tps:dlq / tps:seen`（见 `scripts/tps_queue.py`）天然多机共享。
- **feeder 节奏**：先一台机器 `--mode feed --file urls.txt` 一次性灌队（`feed` 幂等：`tps:seen` 去重），
  worker 全起好后再看 `pending` 增长；**不要多机同时 feed 同一份 URL 文件**。
- **租约与 recover**：`claim` 原子移动 `pending→processing` 并登记租约；
  worker 异常退出后用 `--mode recover` 把过期租约搬回 `pending`（`RECOVER_INTERVAL_SEC=15` 也有周期逻辑，别和手工 recover 打架）。
- **DLQ 观察**：出口 429 走 `release()`（回 `pending`，不进死信）；只有达到 `MAX_ATTEMPTS` 才进 `tps:dlq`。
  `tps:dlq` 持续增长 = 真失败（解析/封禁），先 `peek_dlq` 看错因再扩机器——堆机器治不好 DLQ。
- **DB**：persons 写库是各 worker 直写 TiDB，保证 DB 连接数 ≥ 总浏览器数（4 机档约 96），慢查询先查索引再加机器。
- **状态查看**：`python scripts/distributed_worker.py --mode stats` 看 `pending/processing/dlq` 三数。

## 6. Cline 部署步骤模板（Windows 全量包）

> 只更新 worker 相关文件与 `Common.ps1` 的配套改动；SHA/长度**不编造**，
> 用占位符表示，发布时填入 `Get-FileHash -Algorithm SHA256` 实测值。

```powershell
# 0. 本机确认分支干净（有未提交改动则先停，不要带病发版）
git status --short
git rev-parse HEAD

# 1. 复制文件到 bundle（示例为 worker 与公共模块，按本次清单增减行）
Copy-Item scripts\distributed_worker.py D:\TruePeopleSearch\app\scripts\distributed_worker.py -Force
Copy-Item scripts\proxy_pool.py        D:\TruePeopleSearch\app\scripts\proxy_pool.py -Force

# 2. 验 SHA（把实测值填回占位符，再写清单）
Get-FileHash D:\TruePeopleSearch\app\scripts\distributed_worker.py -Algorithm SHA256  # __WORKER_SHA__ / __WORKER_SIZE__
Get-FileHash D:\TruePeopleSearch\app\scripts\proxy_pool.py -Algorithm SHA256          # __COMMON_SHA__ / __COMMON_SIZE__

# 3. 更新 bundle-manifest.json：改对应 entries 的 sha256/size（schema_version=1，source_dirty=false，commit 为 40 位 hex）
# 4. 全量校验（允许运行时状态目录存在）
.\deploy\windows-full\Test-FullBundle.ps1 -BundleRoot D:\TruePeopleSearch -AllowRuntimeState

# 5. 重启 worker（先停后起；多机逐台滚，每台间隔 ≥2 分钟）
# Stop: Ctrl+C 或 SIGTERM（worker 会停 claim 并等待在飞页面，约 SHUTDOWN_WAIT_SEC=6s）
python scripts\distributed_worker.py --mode worker --concurrency <本机值> --target-per-day 1000000 --page-sec <3|4|8> --proxy-file C:\tps\proxies\us-dedicated.txt

# 6. 10 分钟观察（见 §7），指标不达标则按 §8 回滚
```

## 7. 验证标准（起量后 10 分钟）

| 信号 | 命令 / 位置 | 通过线 |
| --- | --- | --- |
| 429/限流占比 | worker 日志中 `429 / rate_limit / cool` 行占比 | < 5%，且有备用出口时只换道、无全局停顿 |
| persons 增长 | DB `persons` 表 `count(*)` 对比 10 分钟前 | 增速 ≈ 11.6×(60×10)/机器分摊，单机约 6.9k/10min |
| pending 下降 | `--mode stats` 的 `tps:pending` | 单调下降（feeder 停灌后）；`tps:processing` ≈ 总并发页 |
| DLQ | `--mode stats` 的 `tps:dlq` | 零增长；有增长先 `peek_dlq` 看错因 |
| 容量对账 | worker 启动 `[SCALE]` 行 | `capacity ≥ 1,000,000`，`clamped` 未出现 |

## 8. 回滚步骤

1. 停新 worker（Ctrl+C / SIGTERM，等待在飞页面落定）。
2. 把旧文件拷回 `D:\TruePeopleSearch\app\scripts\`（用发版前备份的旧包，不要手改）。
3. 按旧包实测 SHA/size 改回 `bundle-manifest.json`（`source_dirty=false` 保持）。
4. 重跑 `Test-FullBundle.ps1 -AllowRuntimeState`，通过才算回滚完成。
5. 用旧参数重启 worker，观察 10 分钟（同 §7），确认 `pending` 恢复下降、`dlq` 零增长。
