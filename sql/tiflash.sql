-- ============================================================
-- 可选运维脚本：TiFlash 副本 / 读引擎 / 统计 / 预分裂
-- 可重复执行。不改表结构，不给子表加副本。
--
-- Docker 单机默认镜像 pingcap/tidb 只有 TiDB（unistore），
-- 没有 TiFlash。ALTER TABLE ... SET TIFLASH REPLICA 会失败。
-- 本脚本仅供已部署 TiFlash 的集群使用。
--
-- 依赖：people_search.persons（tidb_schema.sql）
--       people_search.stats_snapshot（sql/migrate_existing.sql）
-- ============================================================

USE people_search;

-- ------------------------------------------------------------
-- 1. 仅给 persons / stats_snapshot 加 1 个 TiFlash 副本
--    不要给下面 8 张子表加副本（点查走 TiKV，避免浪费副本）：
--    aliases, current_addresses, previous_addresses,
--    phone_numbers, email_addresses, relatives, associates
--    （以及任何其它子表）
--    已是 REPLICA 1 时再执行一次是幂等的。
-- ------------------------------------------------------------
ALTER TABLE persons SET TIFLASH REPLICA 1;
ALTER TABLE stats_snapshot SET TIFLASH REPLICA 1;

-- 可选：查看副本进度（AVAILABLE=1 后再把面板聚合切到 TiFlash）
-- SELECT TABLE_SCHEMA, TABLE_NAME, REPLICA_COUNT, AVAILABLE, PROGRESS
-- FROM information_schema.tiflash_replica
-- WHERE TABLE_SCHEMA = 'people_search';

-- ------------------------------------------------------------
-- 2. 读引擎约定（会话级，按请求类型设置）
--
-- 面板聚合（COUNT / GROUP BY / 扫 persons 或读 stats_snapshot）：
--   优先 TiFlash，缺副本时回退 TiKV。
--   可选用 AS OF TIMESTAMP / tidb_read_staleness 做知晓读，
--   降低与写入的冲突，适合仪表盘。
--
-- 人物详情点查（按 person_id 取主表 + 子表）：
--   必须留在 TiKV，不要把 tidb_isolation_read_engines 设成仅 tiflash。
-- ------------------------------------------------------------

-- —— 面板聚合会话（示例）——
-- SET SESSION tidb_isolation_read_engines = 'tiflash,tikv';
-- SET SESSION tidb_read_staleness = '-15s';
-- SELECT COUNT(*) FROM persons;
-- SELECT * FROM stats_snapshot WHERE id = 1;
-- SELECT current_state, COUNT(*) AS cnt
-- FROM persons
-- AS OF TIMESTAMP DATE_SUB(NOW(), INTERVAL 15 SECOND)
-- GROUP BY current_state;

-- —— 人物详情点查会话（必须 TiKV）——
-- SET SESSION tidb_isolation_read_engines = 'tikv';
-- SELECT * FROM persons WHERE person_id = ?;
-- SELECT * FROM aliases WHERE person_id = ?;
-- SELECT * FROM current_addresses WHERE person_id = ?;

-- ------------------------------------------------------------
-- 3. 更新 persons 统计信息（优化器选 TiFlash / 索引）
-- ------------------------------------------------------------
ANALYZE TABLE persons;

-- ------------------------------------------------------------
-- 4. 可选预分裂（默认注释掉：Docker 单机 / unistore 会失败）
--    集群写入打散 region 时再按需打开。
--    persons 为 VARCHAR 聚簇主键时 SHARD_ROW_ID_BITS 可能不生效，
--    仅作运维示例。
-- ------------------------------------------------------------
-- ALTER TABLE persons SHARD_ROW_ID_BITS = 4 PRE_SPLIT_REGIONS = 4;
-- ALTER TABLE stats_snapshot SHARD_ROW_ID_BITS = 2 PRE_SPLIT_REGIONS = 2;

-- ------------------------------------------------------------
-- 5. 刷新 stats_snapshot（单行 id=1）
--    从 persons 与子表 COUNT(*) 聚合，可重复执行。
--    REPLACE 与 INSERT ... ON DUPLICATE KEY UPDATE 二选一即可。
-- ------------------------------------------------------------

REPLACE INTO stats_snapshot
    (id, persons, phones, emails, prev_addr, relatives, associates, aliases)
SELECT
    1,
    (SELECT COUNT(*) FROM persons),
    (SELECT COUNT(*) FROM phone_numbers),
    (SELECT COUNT(*) FROM email_addresses),
    (SELECT COUNT(*) FROM previous_addresses),
    (SELECT COUNT(*) FROM relatives),
    (SELECT COUNT(*) FROM associates),
    (SELECT COUNT(*) FROM aliases);

-- 等价写法（保留 updated_at 由 ON UPDATE 刷新）：
-- INSERT INTO stats_snapshot
--     (id, persons, phones, emails, prev_addr, relatives, associates, aliases)
-- SELECT
--     1,
--     (SELECT COUNT(*) FROM persons),
--     (SELECT COUNT(*) FROM phone_numbers),
--     (SELECT COUNT(*) FROM email_addresses),
--     (SELECT COUNT(*) FROM previous_addresses),
--     (SELECT COUNT(*) FROM relatives),
--     (SELECT COUNT(*) FROM associates),
--     (SELECT COUNT(*) FROM aliases)
-- ON DUPLICATE KEY UPDATE
--     persons    = VALUES(persons),
--     phones     = VALUES(phones),
--     emails     = VALUES(emails),
--     prev_addr  = VALUES(prev_addr),
--     relatives  = VALUES(relatives),
--     associates = VALUES(associates),
--     aliases    = VALUES(aliases);
