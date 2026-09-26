# TruePeopleSearch 大规模抓取 + TiDB 存储部署指南

## 架构总览

```
Scrapling (StealthyFetcher)     Redis (tps: 可靠队列)      TiDB (存储)
      |                            |                       |
      +- Worker 1 (concurrency) ---+                       |
      +- Worker 2 -----------------+                       |
      +- Worker N -----------------+                       |
      |                            |                       |
      v                            v                       v
   抓取页面                    BLMOVE + ZSET 租约        8张表 + 摘要表
   过 Cloudflare               过期回收 / tps:dlq        3亿行无压力
```

队列约定（实现目标，禁止 `LPOP`）：`BLMOVE` 把任务从 `tps:pending` 原子搬到 `tps:processing`，`ZSET` 记租约，过期由 `--mode recover` 回收，超限进死信 `tps:dlq`。feed 按 `person_id` 去重。

## 文件结构

```
22/
├── sql/
│   ├── tidb_schema.sql          # TiDB 建表 (8张表 + 摘要表 + 视图)
│   ├── migrate_existing.sql     # 已有库迁移（新列 / UNIQUE / stats_snapshot）
│   └── tiflash.sql              # 可选：TiFlash 副本（单机 Docker 没有 TiFlash）
├── scripts/
│   ├── scrape_to_tidb.py        # 抓取+入库主脚本
│   ├── distributed_worker.py    # 分布式 Worker（feed|worker|recover|stats）
│   ├── tps_queue.py             # Redis 可靠队列（前缀 tps:）
│   ├── tps_metrics.py           # 队列/抓取指标
│   ├── test_tps_queue.py        # 队列单测
│   └── test_tps_metrics.py      # 指标单测
├── tools/
│   ├── db_viewer.py             # 命令行查看工具 (交互式 + CLI)
│   ├── dashboard_api.py         # Web 可视化面板后端 (Flask)
│   └── dashboard.html           # 可视化面板前端
├── data/
│   ├── jamie_perez_profile.md   # 示例：Jamie Perez 完整信息
│   └── urls.txt                 # URL 列表 (你自己创建)
├── start.sh                     # 一键启动脚本
└── README.md                    # 本文件
```

## 一、安装 TiDB

### 方式 1：Docker（推荐，最快）

```bash
docker run --name tidb \
  -p 4000:4000 -p 10080:10080 \
  -d pingcap/tidb:latest

# 验证
mysql -h 127.0.0.1 -P 4000 -u root -e "SELECT version()"
```

### 方式 2：TiUP（生产部署）

```bash
curl --proto '=https' --tlsv1.2 -sSf https://tiup-mirrors.pingcap.com/install.sh | sh
source ~/.bash_profile
tiup playground
```

### 方式 3：TiDB Cloud（免运维）

注册 tidbcloud.com，获取连接串，修改 scripts/scrape_to_tidb.py 中的 TIDB_CONFIG。

## 二、安装 Redis

```bash
# Docker
docker run --name redis -p 6379:6379 -d redis:latest

# macOS
brew install redis && redis-server
```

## 三、初始化数据库

```bash
# 新库建表
mysql -h 127.0.0.1 -P 4000 -u root < sql/tidb_schema.sql
```

已有库（之前按旧 schema 建过表）再跑一次迁移，补冗余列、子表 UNIQUE、`stats_snapshot`：

```bash
mysql -h 127.0.0.1 -P 4000 -u root < sql/migrate_existing.sql
```

新库若已执行完整的 `tidb_schema.sql`，迁移脚本大多是幂等的；重复执行时个别 `ADD UNIQUE` 可能报已存在，可忽略。

可选 TiFlash（**单机 Docker `pingcap/tidb` 没有 TiFlash**，`SET TIFLASH REPLICA` 会失败，仅已部署 TiFlash 的集群使用）：

```bash
mysql -h 127.0.0.1 -P 4000 -u root < sql/tiflash.sql
```

## 四、安装 Python 依赖

```bash
pip install "scrapling[fetchers]" "mysql-connector-python>=9.2" redis tabulate flask psutil
scrapling install   # 安装 Playwright 浏览器
```

## 五、使用

### 单条抓取

```bash
cd scripts
python3 scrape_to_tidb.py \
  --url "https://www.truepeoplesearch.com/find/person/px82l44nur68u2l2l8n60"
```

### 批量抓取（可靠队列）

Worker 模式：`--mode feed|worker|recover|stats`。消费侧禁止 `LPOP`：`BLMOVE pending → processing`，`ZSET` 租约，过期回收，死信 `tps:dlq`。feed 按 `person_id` 去重。`--concurrency` 是常驻浏览器个数，不是“一条 URL 一个线程”。每 4 个并发页共用一个 Chrome、各自一个标签页，Cloudflare 只在这个 Chrome 第一次打开时解一次。实测同一出口上，首条约 2 分钟，之后约 8 秒且不再出验证。按 8 秒估算，一天 300 万条需要大约 278 个浏览器；冷启动线程要 1000 个以上。一个 IP 撑不住这么多并发，应按浏览器配粘性 IP，不要每条请求换 IP。不传 `--concurrency` 时按本机预算拉起：每个浏览器约 0.6GB（给系统和 TiDB 留 4GB），同时不超过每核 3 个，避免 16 核机器被上百个 Chrome 打满。一台不够就多台机器跑多个 worker，共用同一个 Redis。`Ctrl+C` / SIGTERM 停领取，短暂等待在飞页面，未完成任务 `nack` 回队。

```bash
# 1. 创建 URL 列表
cat > data/urls.txt << 'EOF'
https://www.truepeoplesearch.com/find/person/px82l44nur68u2l2l8n60
https://www.truepeoplesearch.com/find/person/abc123def456
EOF

# 2. 灌入 Redis 队列（按 person_id 去重）
cd scripts
python3 distributed_worker.py --mode feed --file ../data/urls.txt

# 3. 启动 Worker（常驻浏览器；省略 --concurrency 则按内存预算）
python3 distributed_worker.py --mode worker --concurrency 20

# 4. 回收过期租约（也可由 worker 周期调用）
python3 distributed_worker.py --mode recover

# 5. 查看队列深度 / 指标
python3 distributed_worker.py --mode stats
```

`Ctrl+C` 即可优雅停 Worker，不要直接 `kill -9`。

### 数据库查看

```bash
cd tools

# 交互式菜单
python3 db_viewer.py

# 命令行查询
python3 db_viewer.py --search "Jamie Perez"
python3 db_viewer.py --phone "(303) 210-9670"
python3 db_viewer.py --email "gmail.com"
python3 db_viewer.py --city "Thornton"
python3 db_viewer.py --stats
python3 db_viewer.py --person "px82l44nur68u2l2l8n60"
```

### Web 可视化面板

仍用原来的 Flask 后端：

```bash
cd tools
python3 dashboard_api.py
# 浏览器打开 http://localhost:5000
```

macOS 上 **5000 口常被 AirPlay Receiver 占用**，改端口即可：

```bash
cd tools
python3 dashboard_api.py --port 5055
# 浏览器打开 http://localhost:5055
```

面板功能：
- **概览页**：统计卡片 + 城市分布/年龄分布图表 + 最近抓取
- **人物列表**：分页浏览、按姓名搜索、点击查看详情
- **全局搜索**：同时搜姓名/电话/邮箱
- **图表分析**：城市分布 Top 20 + 年龄分布
- **最近抓取**：最新入库的人物
- **人物详情**：别名、地址、电话、邮箱

### Redis 键前缀 `tps:`

| 键 | 类型 | 说明 |
|----|------|------|
| `tps:pending` | LIST | 待领取 job_id |
| `tps:processing` | LIST | 已领取、未 ack 的 job_id |
| `tps:leases` | ZSET | job_id → lease_until（unix） |
| `tps:job:{id}` | STRING | job JSON |
| `tps:dlq` | LIST | 死信 job_id |
| `tps:seen` | SET | 已入队 `person_id`（feed 去重） |
| `tps:recover_lock` | STRING | 过期回收分布式锁 |
| `tps:metrics:*` | STRING | 计数 / 延迟（见 `tps_metrics.py`） |

旧键 `tps_urls` 不再 `LPOP`。feed 侧可将遗留 URL `drain_legacy` 进新队列。

### 测试

```bash
cd scripts && python3 test_tps_queue.py && python3 test_tps_metrics.py
```

## 六、数据库查看方式汇总

| 工具 | 类型 | 免费 | 说明 |
|------|------|------|------|
| **dashboard_api.py** | Web 面板 | 免费 | 自己的，浏览器打开 localhost:5000（macOS 可 `--port 5055`） |
| **db_viewer.py** | 命令行 | 免费 | 自己的，交互式菜单 |
| **mysql 命令行** | CLI | 免费 | `mysql -h 127.0.0.1 -P 4000 -u root` |
| **DBeaver** | 桌面 GUI | 免费 | https://dbeaver.io |
| **MySQL Workbench** | 桌面 GUI | 免费 | Oracle 官方 |
| **TiDB Cloud** | Web | 免费版 | 注册即用，自带 SQL Editor |

## 七、TiDB 免费方案

| 方案 | 免费额度 | 适合 |
|------|---------|------|
| **Docker 本地部署** | 完全免费，无限制 | 开发、小规模 |
| **TiUP 自建** | 完全免费 | 生产集群 |
| **TiDB Cloud Serverless** | 5GB 行存+5GB 列存免费 | 云端开发 |

## 八、容量规划

| 数据量 | TiDB 节点 | 磁盘 | 内存 | 月成本 |
|--------|----------|------|------|--------|
| 300 万人 (~3.5亿行) | 3 节点 | ~100 GB | ~30 GB | ~$300 |
| 1000 万人 (~12亿行) | 5 节点 | ~350 GB | ~60 GB | ~$600 |
| 5000 万人 (~60亿行) | 10 节点 | ~2 TB | ~150 GB | ~$1500 |

## 本地面板运行与验证

当前 `start.sh` 是本地面板启动器，需要已有 Python 及 `flask`、`mysql-connector-python >= 9.2`、`redis` 依赖；安全启停子进程还需要 `psutil`，以及已初始化的数据库和 Redis。数据库就绪检查需要该驱动版本以支持有限的读写等待时间。它不再自动建库或迁移，不安装依赖、不创建或替换容器、不下载镜像，也不启动采集任务。前文的独立脚本用法保留；面板当前默认地址为 `http://127.0.0.1:5001`。

```bash
# 只读检查依赖、已有数据库表结构、Redis 和容器挂载元数据；不启动服务
bash start.sh --check

# 必要时恢复本机已经存在且停止的 tidb / redis 容器，检查通过后前台启动面板
bash start.sh --timeout 30
```

服务不可用或数据库结构缺失时，启动器返回非零状态，不输出“系统就绪”。它不会操作远程 Docker 来恢复容器。启动后以终端显示的实际地址为准：默认端口被占用时，会在起始端口及随后 19 个端口内选择空闲端口；都不可用则退出。直接运行 `python3 tools/dashboard_api.py --host 127.0.0.1 --port 5055` 的用法继续支持。

可通过环境变量配置：

| 变量 | 默认值或用途 |
|------|-------------|
| `TPS_PYTHON`、`TPS_STARTUP_TIMEOUT` | Python 解释器（`python3`）、服务就绪重试等待秒数（`30`，范围 1–300） |
| `TPS_DB_HOST`、`TPS_DB_PORT` | 数据库地址（`127.0.0.1`）、端口（`4000`） |
| `TPS_DB_USER`、`TPS_DB_PASSWORD`、`TPS_DB_NAME` | 用户（`root`）、密码（空）、已有数据库名（`people_search`） |
| `TPS_REDIS_HOST`、`TPS_REDIS_PORT` | Redis 地址（`127.0.0.1`）、端口（`6379`） |
| `TPS_DASHBOARD_HOST`、`TPS_DASHBOARD_PORT` | 面板监听地址（`127.0.0.1`）、起始端口（`5001`） |

面板只接受 `127.0.0.1`、`::1` 或 `localhost` 监听地址；其他地址会拒绝启动。面板启动后台任务时会先核对任务继承的 Redis 目标与面板配置，并对使用数据库的任务核对当前代码中可验证的数据库目标；目标不一致或无法确认时拒绝启动，停止和只读状态仍可使用。现有后台数据库配置是固定值，因此更改面板的 `TPS_DB_*` 并不会自动更改后台目标。

请求入口还会核验本机 Host，写操作如果带有 Origin 头则必须来自当前面板地址；所有 `/api/` 响应都带 `Cache-Control: no-store`。代理连接测试只允许原固定测试目标，不跟随跳转，超时限 3–20 秒，最多流式读取 256 KiB 响应。代理配置保存需要 Redis 可用；同机写入会串行执行，文件替换失败时会尝试有条件回滚 Redis。两个存储无法构成严格原子事务；进程崩溃或回滚失败后仍须核对两侧配置。

进程停止只针对由面板记录且身份匹配的任务；缺少 `psutil` 或记录已过期时会拒绝按 PID 发送信号。同一任务的启停操作使用 Redis 锁串行执行，拿不到锁时明确拒绝操作；启动登记失败会清理本次创建的进程。现有代理配置、数据文件、日志和本目录的 `dump.rdb` 已设为仅当前用户可读写，新写入的代理配置和日志也会保持此权限；Redis 重新生成 `dump.rdb` 后应复查权限。面板返回的代理配置、常见失败响应与日志预览会隐藏凭据。直接在命令行参数传入代理密码的独立脚本仍可能被本机同用户进程看到，不应把密码放在命令行中。

`GET /api/health` 只表示面板进程可响应；`GET /api/ready` 与启动器共用数据库及 Redis 检查，使用 `LIMIT 0` 核验九张既有表且不读取记录，依赖未就绪时返回 `503`。启动器的单次服务探测最多 4 秒，每条 Docker 命令最多 10 秒，诊断中的 URL 用户名和密码会脱敏；`--timeout` 是恢复服务后的重试等待期限，不是全部启动过程的总时限。

挂载检查只报告现有同名容器的镜像、卷名和目标目录，不代表当前连接必然由该容器提供，也不能单独证明数据持久化已验证。升级或重装前仍须核对实际数据目录、命名卷归属、固定镜像版本，并完成备份与恢复验证；本轮没有更换现有卷或迁移数据。目前未提供 macOS / Windows 安装包，Bash 启动器也不是 Windows 原生安装程序。

日志预览每次最多读取末尾 48 KiB，遇到并发截断最多重试一次。超过 10 MiB 且确认没有进程打开的旧日志，下次启动相应任务前会尽力只保留末尾 2 MiB，早期日志可能被清理；活跃日志或无法核验打开状态时跳过。这不是运行中的日志大小硬上限。

面板已移除 Google Fonts 外部字体请求；现有数据仍通过配置的 TiDB / Redis 读取，默认都为本机。此次调整不代表客户端打包或改用 SQLite 已完成。

本轮新增检查使用模拟服务与命令，不需要真实数据库或 Docker，也不会触发采集：

```bash
node --test tests/test_dashboard_*.cjs
python3 -B -m unittest discover -s tests -p 'test_local_*.py'
python3 -B -m unittest discover -s tests -p 'test_proxy_config_security.py'
python3 -B scripts/test_tps_control.py
```

### 通用前端资源优化

状态文字只有变化时才更新 DOM；页面切换保留原淡入动画，取消为重启动画而同步读取布局。通用请求层在超时、切页或退出后主动结束等待并清理自己的计时器、监听器及共享请求记录，迟到响应不会影响新请求。带独立取消信号、请求头或缓存配置的请求不与普通 GET 合并；不同超时设置也各自生效。写入请求仍不自动重试，超时仅表示结果尚未确认。

合成回归与浏览器检查中，100 次相同文字刷新由 100 次 DOM 写入降为 0 次；100 次页面渲染的主动同步布局读取由 100 次降为 0 次。1000 次模拟导航后，每轮请求计时器和取消监听器均已清理。这里测量的是前端操作次数和运行层资源生命周期，不代表整体 CPU、内存或数据库吞吐的提升倍数；也不能强制回收不响应取消信号的外部请求实现。本轮未改采集、聚合或个人信息查询逻辑。

四处只读状态轮询现在会在连续失败时逐步延长检查间隔，最长 30 秒；成功后恢复原频率。隐藏窗口继续暂停轮询，并通过同一个可见性监听暂停 CSS 动画。返回前台时立即安排一次检查，已有请求仍在进行时不叠加请求。此机制不依赖外网在线状态，本地服务在无外网时仍可访问，也不会自动重试写入操作。代价是服务恢复后的自动状态更新可能需要等待下一轮，另加请求自身的耗时。

在 2 秒基础间隔、请求即时失败的模拟时钟测试中，60 秒内自动轮询由 30 次减少为 5 次（第 2、6、14、30、60 秒，不含手动首屏请求）。新增 14 项轮询退避测试和 3 项可见性测试，前端回归共 55 项通过；浏览器模拟隐藏/显示事件验证动画为 `running → paused → running`，减少动态效果设置下仍为 `none`。这些是受控功能和操作次数验证，不代表已测得真实后台 CPU 降幅。

### 静态资源与动画绘制优化

`dashboard.html` 的样式和应用脚本已拆为 `dashboard.css` 与 `dashboard-app.js`，脚本内容与拆分前一致。首页继续 `no-store`，HTML 从 152,288 字节降为 4,942 字节；三个固定白名单资源（CSS、应用脚本、通用运行层）使用 ETag 和 `no-cache`，浏览器每次核验版本，未变化返回无正文的 `304`。实际浏览器刷新已确认三个资源均复用缓存；更新文件后旧 ETag 返回新内容的测试通过。收益针对再次打开或刷新时的传输，不代表首次加载或持续运行速度提高相同比例；部署时需一并保留 `tools` 下这三个资源文件。

在线提示光晕和加载占位高光改为伪元素的位移、缩放、透明度动画，避免逐帧改动阴影和背景位置。保留隐藏暂停、减少动态效果设置、圆角裁切及点击穿透。Chromium 在相同的 6 个占位块、4 个状态点合成场景中，预热 1 秒后记录 3 秒，旧版 180 次 Paint、新版 0 次；交替重复两轮一致，动画位移和截图检查正常。这只证明该受控动画场景减少主线程绘制，合成和 GPU 工作仍然存在，不代表整个应用零开销。

通用运行层销毁后不再接受新轮询或重建页面控制器，修复异步完成后误注册造成的对象残留；模拟 1,000 次迟到注册由保留 1,000 个轮询对象降为 0 个。前端与面板 API 的模拟回归已分别通过。浏览器用模拟数据连续完成 100 次页面重建，事件监听器始终 19 个；主动垃圾回收后的 JS 堆在暖身后约 1.02 MB，未见随每轮成比例增加。此检查持续时间较短，不替代数小时运行和真实负载测量。所有验证均未读取真实业务数据或启动采集。

## 九、生产级可运营与守护体系 (Production Reliability)

本系统已补齐生产运营所需的关键基础机制：

### 1. 进程自愈与后台守护 (`supervisor.sh`)
通过 Supervisor 统一守护抓取 Worker 与 Dashboard 控制面板，崩溃自动检测拉起，带有故障退避，防止狂刷重启：

```bash
# 启动守护进程（默认并发 2，可根据机器配置调大）
./supervisor.sh start --concurrency 4

# 查看运行状态
./supervisor.sh status

# 平稳停止所有服务（向所有子进程发送 SIGTERM，在飞任务平稳归还）
./supervisor.sh stop

# 重启服务
./supervisor.sh restart
```

### 2. 代理风控自适应熔断机制 (Adaptive Circuit Breaker)
当目标站（如 TruePeopleSearch）强化风控、出现连续验证码拦截或代理出口质量恶化时：
* 触发连续验证码换 IP 后，自动实施阶梯退避冷却（2s → 5s → 15s → 30s → 60s），避免盲目疯狂轮换 IP 导致瞬间抽干上游代理池或触发网关断开 (`ERR_CONNECTION_CLOSED`)；
* 单个 Chrome 组成功抓取后立即清零惩罚，恢复正常抓取节奏。

### 3. 多渠道告警通知 (`scripts/tps_alert.py`)
支持通过配置环境变量 `TPS_ALERT_WEBHOOK` 接入企业办公平台报警：
* **支持平台**：钉钉 (DingTalk)、企业微信 (WeCom)、飞书 (Feishu)、通用 JSON Webhook；
* **告警触发场景**：死信队列 (DLQ) 积压 > 50 条、连续频繁触发验证码（风控/代理枯竭预警）、服务持续崩溃超限；
* **防抖节流**：内置 5 分钟冷却防抖机制，杜绝故障期间产生告警风暴。

```bash
# 启动时配置告警 Webhook 示例
export TPS_ALERT_WEBHOOK="https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN"
./supervisor.sh start
```

## 十、客户端独立安装部署与云端自动升级指南 (Client Deployment & Auto-Update)

针对客户本地独立部署（Local Database + Local Queues + Web Dashboard），系统已提供完整的单机一键部署包与全自动在线热更新机制。

### 1. 客户本地独立数据库与消息队列 (`deploy/docker-compose.yml`)

在客户电脑上无需安装复杂服务，内置单容器 PingCAP TiDB 单机版引擎（完全兼容 MySQL 8.0 语法、支持千万级高并发与持久化存储）与 Redis 7 缓存队列：

```bash
# 启动本地 TiDB (端口 4000) 与 Redis (端口 6379)
docker compose -f deploy/docker-compose.yml up -d

# 自动初始化 8 张核心数据表与索引
python3 deploy/init_db.py
```

### 2. Windows 客户一键安装与启动向导

针对使用 Windows 电脑的客户，提供了开箱即用的批处理脚本：
* **全新安装**：双击运行 `deploy/install.bat`，脚本将自动配置 Python 虚拟环境、拉起本地 TiDB 与 Redis 并初始化建表；
* **一键启动**：双击根目录 `start_client.bat`，自动在后台拉起 Supervisor 集群与可视化面板，并自动在浏览器打开控制台 `http://127.0.0.1:5001`；
* **平稳关闭**：双击根目录 `stop_client.bat`，平稳释放所有爬虫进程与数据库连接。

### 3. Linux / macOS 客户一键安装与启动向导

```bash
# 一键安装环境与数据库
chmod +x deploy/install.sh && ./deploy/install.sh

# 启动后台守护集群
./supervisor.sh start

# 浏览器访问控制台
open http://127.0.0.1:5001
```

### 4. Git 版本库关联与云端自动推送流程 (开发者端)

当前项目已完成 Git 仓库初始化 (`main` 分支) 并创建了企业级过滤规则 `.gitignore`。
如需与 GitHub 或 Gitee 仓库关联，只需在开发机执行：

```bash
# 1. 关联云端远程仓库 (将 your-org/repo 替换为您的真实仓库地址)
git remote add origin https://gitee.com/your-org/truepeoplesearch.git
# 或者 GitHub:
# git remote add origin https://github.com/your-org/truepeoplesearch.git

# 2. 推送初始版本到云端
git branch -M main
git push -u origin main
```

**后续更新发布流程**：
1. 修改代码或在 `version.json` 中更新版本号与更新日志；
2. 提交并推送到远端：
   ```bash
   git commit -am "feat: 升级反爬策略与性能优化"
   git push origin main
   ```

### 5. 客户安装后自动检测更新与一键平滑升级 (客户端)

客户安装系统后，拥有无感知的更新提示与无损平滑升级能力：
* **自动检测**：客户打开仪表盘面板时，系统会在后台静默检测云端是否有新代码或新版本；
* **顶部提醒横幅**：发现新版本后，面板顶部将以平滑动画浮现 `📢 发现系统新版本！包含反爬规则更新与性能优化` 提醒条，且右上角版本徽章呈现橙色呼吸呼吸光晕；
* **在线一键升级**：
  * 客户点击「🚀 立即一键升级」；
  * 系统弹出升级弹窗并提供实时终端控制台，自动在后台安全执行 `git pull` 同步最新代码、校验数据库增量更新并热重载 Supervisor 守护集群；
  * 升级成功后控制台自动刷新，平滑接入最新版本，零停机且不丢失正在执行的任务队列！
* **命令行离线备用更新**：
  * Windows 客户双击 `deploy/update.bat`；
  * Linux/macOS 客户执行 `./deploy/update.sh`。


