-- ============================================================
-- TiDB 3000万/日 (单日增量约 2.7 亿行) 高吞吐写入调优脚本
-- 
-- 针对高频并发写入优化：
-- 1. SHARD_ROW_ID_BITS = 4: 将自增/隐式 RowID 分片打散为 16 个分片 (避免单 Region 集中写入热点)
-- 2. PRE_SPLIT_REGIONS = 4: 预拆分 16 个 Region，新表直接多节点并发写
-- 3. AUTO_RANDOM: 主键完全随机化分布 (TiDB 专属特性，消除单调递增导致的所有写热点)
-- ============================================================

USE people_search;

-- 如果表已存在，可对支持的表开启 SHARD_ROW_ID_BITS
ALTER TABLE persons SHARD_ROW_ID_BITS = 4 PRE_SPLIT_REGIONS = 4;
ALTER TABLE aliases SHARD_ROW_ID_BITS = 4 PRE_SPLIT_REGIONS = 4;
ALTER TABLE current_addresses SHARD_ROW_ID_BITS = 4 PRE_SPLIT_REGIONS = 4;
ALTER TABLE previous_addresses SHARD_ROW_ID_BITS = 4 PRE_SPLIT_REGIONS = 4;
ALTER TABLE phone_numbers SHARD_ROW_ID_BITS = 4 PRE_SPLIT_REGIONS = 4;
ALTER TABLE email_addresses SHARD_ROW_ID_BITS = 4 PRE_SPLIT_REGIONS = 4;

-- 会话/集群级别写入加速建议（批量入库时可在应用层连接执行）：
-- SET @@tidb_batch_insert = 1;
-- SET @@tidb_dml_batch_size = 2000;
