# 自建求解农场 Runbook（solver-farm）

> 范围：只讲自建求解器怎么起、怎么接 worker、怎么查错。术语与 `docs/1M-runbook.md` 第 3 节（求解器契约）及 `scripts/cf_solver.py` 头部注释一致。
> 约定：`TPS_OWN_CF=1` = 自研过 CF（不开浏览器）；`TPS_OWN_CF=0` = 内置浏览器过 CF。

## 1. 架构一句话 + 数据流

一句话：worker（`TPS_OWN_CF=1`）首次抓取前经 `scripts/cf_solver.py` 的 `CfSolver` 向 `TPS_CF_SOLVER` 要一份放行凭证（`cookies` + `User-Agent`），之后用 curl_cffi 在同一条代理出口上以同版本 Chrome TLS 指纹发协议请求。

数据流：

```text
distributed_worker（组绑定 proxy）
  -> CfSolver.solve(url=, proxy=, user_agent=, timeout=)
  -> 自建求解农场（cf_farm.py / FlareSolverr 兼容节点 / Byparr 节点）
  -> 返回 CfSolution{cookies(cf_clearance, __cf_bm…), user_agent, html?, ttl}
  -> worker 用 curl_cffi 同代理同指纹发请求（impersonate 按 UA Chrome 主版本自动挑，TPS_IMPERSONATE 可强制覆盖）
  -> 凭证同时按 unblocker:warmed:{host}[:{sid}] 发布到 Redis，protocol_fetcher.py 的协议 fleet 可直接复用
  -> 被拒（403/503/挑战页）重解一次；再被拒按验证码上报（本组休 TPS_FP_REST_SEC，连续两次换出口）；TPS_SESSION_RECYCLE 页后整组重建并重新要凭证
```

关键约束（契约原文）：`proxy` 是本组绑定的出口，`cf_clearance` 绑 IP，求解器必须走同一出口；`user_agent` 为 `null` 是首解（求解器自选 UA），非空是重解（请沿用它）。

## 2. TPS_CF_SOLVER 五种写法对照表

`TPS_CF_SOLVER_TIMEOUT` 默认 `45` 秒，单次求解超时。

| # | 写法 | 例子 | 调用方式（`scripts/cf_solver.py` 内） |
| --- | --- | --- | --- |
| 1 | Python 函数 | `TPS_CF_SOLVER=my_cf.solver:solve` | `solve(url=, proxy=, user_agent=, timeout=)`，同步/异步都行 |
| 2 | HTTP 服务 | `TPS_CF_SOLVER=http://127.0.0.1:9000/solve` | `POST` JSON 正文 `{url, proxy, user_agent, timeout}` |
| 3 | 命令行 | `TPS_CF_SOLVER=cmd:D:\tools\cf.exe --json` | stdin 收 JSON，stdout 回 JSON，`exit 0` 才认 |
| 4 | FlareSolverr 兼容 | `TPS_CF_SOLVER=flaresolverr:http://127.0.0.1:8191/v1` | 转调兼容 API `POST {cmd: request.get, url, proxy, maxTimeout}`，取 `solution{cookies, userAgent, response}` 转统一凭证；Byparr v2 同接口 |
| 5 | Byparr 别名 | `TPS_CF_SOLVER=byparr:http://127.0.0.1:8191/v1` | 同上，`byparr:` 只是别名，解析后与 `flaresolverr:` 同一路（Camoufox 内核） |

请求（五种写法收到的参数完全一样）：

```json
{"url": "https://www.truepeoplesearch.com/find/person/xxx",
 "proxy": "http://user:pass@gw:1288",
 "user_agent": null,
 "timeout": 45}
```

响应（字段名宽松，可整体包在 `data` / `result` 里；`cookies` 也接受 `[{"name":..,"value":..}]` 列表或 `"k=v; k2=v2"` 字符串；`{"ok": false, "error": "..."}` 视为失败；既无 `cookies` 也无 `html` 视为失败）：

```json
{"cookies": {"cf_clearance": "...", "__cf_bm": "..."},
 "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ... Chrome/131.0.0.0 Safari/537.36",
 "html": "可选：求解器已经拿到该 url 正文就直接给，省一次请求",
 "ttl": 1500}
```

备注：FlareSolverr 本体已过时别用；2026 年免费档里 Turnstile / Managed Challenge 成功率最高的是 Byparr（Camoufox 内核）。

## 3. 本地农场 cf_farm.py（启动 + /healthz）

约定入口为 `scripts/cf_farm.py`，对外暴露 `POST /v1`（FlareSolverr 兼容，给 `TPS_CF_SOLVER=flaresolverr:http://127.0.0.1:8191/v1` 用）与 `GET /healthz`。

线程模型（现状）：固定 lane 池 `TPS_FARM_THREADS`（默认 8）；同代理 sticky 同 lane；每代理 `Semaphore(1)` 串行 fetch、不持 entry 锁；连续失败超 3 次只代数 +1、属主下次懒建（不关 fetch 中会话）；表项 LRU（`TPS_FARM_MAX_ENTRIES` 默认 128）+ 闲置 TTL（`TPS_FARM_IDLE_TTL_S` 默认 600 秒）回收，只动无 fetch 表项；`GET /healthz` 走池外；同线程只持一个活会话（单 loop 约束）。

```powershell
python scripts/cf_farm.py --port 8191
curl.exe http://127.0.0.1:8191/healthz
$env:TPS_OWN_CF = "1"
$env:TPS_CF_SOLVER = "flaresolverr:http://127.0.0.1:8191/v1"
```

手工打一次求解：

```powershell
curl.exe -X POST http://127.0.0.1:8191/v1 `
  -H "Content-Type: application/json" `
  -d '{"cmd":"request.get","url":"https://www.truepeoplesearch.com/","maxTimeout":45000}'
```

通过线：`healthz` 200；`/v1` 回 `status:ok` 带 `cf_clearance`；worker 日志无 `TPS_CF_SOLVER` 相关错。

## 4. Byparr 落地步骤

待 Byparr 调研结论填入。

## 5. 故障排查表

| 现象 | 含义 | 对应命令 |
| --- | --- | --- |
| 无 solver 启动死（worker 起不来，直接抛错） | `TPS_CF_SOLVER` 没配或加载失败（`load_cf_solver_from_env` 启动期就死）。常见：`TPS_CF_SOLVER is empty`、`TPS_OWN_CF=1 but TPS_CF_SOLVER is not set`、模块导不进、HTTP/兼容地址不可达、cmd 跑不起来 | `echo $env:TPS_OWN_CF; echo $env:TPS_CF_SOLVER`（确认 `1` + 五种写法之一）；`python -c "import my_cf.solver"`（Python 写法才用，换成真实模块名）；`curl.exe http://127.0.0.1:8191/healthz`（HTTP/农场写法）；`curl.exe http://127.0.0.1:8191/v1`（`flaresolverr:`/`byparr:` 写法，确认节点在听）；`python scripts/cf_farm.py --port 9000` 前台重跑看报错 |
| 解不出（超时 / `ok:false` / 无凭证） | 求解器返回失败：`solver timed out`、`solver reported failure`、`solver returned neither cookies nor html`、`flaresolverr did not solve`、`solver command exit N`。常见：挑战太难、节点过载、`TPS_CF_SOLVER_TIMEOUT` 太小、代理不通、cmd 非 0 退出 | `curl.exe http://127.0.0.1:8191/healthz`（先看农场活没活）；手工 `POST /v1`（见 §3，`maxTimeout` 填 `45000`，看回包是超时还是明确失败）；`echo $env:TPS_CF_SOLVER_TIMEOUT`（太小就调大，默认 `45`）；cmd 写法前台手跑一遍 stdin/stdout JSON（确认 `exit 0` 且出 `cookies`/`html`）；`flaresolverr:`/`byparr:` 写法检查节点日志的 `status/message` |
| cookie 无效（拿到了凭证但 403/503/挑战页，重解一次还是被拒） | 凭证与出口/IP、UA/指纹对不上，或已过期。契约：首解后所有页面走同代理同指纹；`TPS_IMPERSONATE` 只在 UA 版本和指纹对不上时用；`ttl` 默认 `1500`；`TPS_SESSION_RECYCLE` 页后整组重建 | 确认求解与业务走同一 `proxy`（`cf_clearance` 绑 IP，换出口必须重解）；`echo $env:TPS_IMPERSONATE`（为空=按 UA 自动挑，非空=被强制覆盖，版本对不上先清空再试）；检查返回的 `user_agent` 是否被重解沿用（`null`=首解自选，非空=重解沿用）；检查 `ttl` / `TPS_SESSION_RECYCLE`（过期或超页就重建重解）；再被拒按验证码上报：本组休 `TPS_FP_REST_SEC`，连续两次换出口（`TPS_IP_REST_SEC` 冷却），不要堆机器 |

通用兜底：以上三类都先看 worker 启动前 50 行日志（`CfSolverError` 即答案），再按表打命令；`USE_CLOUDBYPASS` 保持 `0`（走自研链路，不要切穿云网关）。

## 6. 回滚

求解器链路异常且短时间修不好时，回退到内置浏览器过 CF：

```powershell
$env:TPS_OWN_CF = "0"
# $env:TPS_CF_SOLVER 留着不删也行，TPS_OWN_CF=0 时 worker 不读它
python scripts/distributed_worker.py --mode worker `
  --concurrency 35 --target-per-day 1000000 --page-sec 3 `
  --proxy-file C:\tps\proxies\us-dedicated.txt
```

回滚确认：worker 启动不再报 `TPS_CF_SOLVER` 相关错；`--mode stats` 的 `tps:pending` 恢复下降、`tps:dlq` 零增长（同 `docs/1M-runbook.md` §7 口径）。

## 7. 已知约束

- 动态隧道 429 是账号总量限流，换出口无用，全账号停 20/40/60 秒；固定出口 429 才冷却该 IP 5/15/45 分钟。
- 同一份凭证现实只够约 2 页，被拒重解。
- sticky 代理 10 分钟可漂 3 个出口 IP（绑 IP），复用前用 `scripts/egress_guard.py` 探一次，变了重解。
