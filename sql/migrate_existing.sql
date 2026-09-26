-- ============================================================
-- 已有库迁移：persons 冗余列 / 子表 UNIQUE / stats_snapshot
-- 兼容 TiDB 7+ 与 MySQL 8
--
-- TiDB 7+ 支持 ADD COLUMN IF NOT EXISTS / ADD INDEX IF NOT EXISTS。
-- MySQL 8 若报语法错误，去掉 IF NOT EXISTS。
-- 普通 ADD COLUMN / ADD UNIQUE 重复执行可能报已存在。
-- ============================================================

USE people_search;

-- ------------------------------------------------------------
-- 1. persons：内容哈希 + 冗余计数 + keyset 索引
-- ------------------------------------------------------------
ALTER TABLE persons
    ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS phone_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS email_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS alias_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS relative_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS associate_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS prev_addr_count INT NOT NULL DEFAULT 0;

-- 重复执行可能报已存在
ALTER TABLE persons
    ADD INDEX IF NOT EXISTS idx_persons_name_id (full_name, person_id);

-- ------------------------------------------------------------
-- 2. 子表加 UNIQUE 前：若已有重复行需先去重，否则 ADD UNIQUE 会失败。
-- 以下 DELETE 为可选示例，按 MIN(id) 保留一行；确认后再取消注释执行。
-- ------------------------------------------------------------

-- DELETE a FROM aliases a
-- INNER JOIN (
--     SELECT person_id, alias_name, MIN(id) AS keep_id
--     FROM aliases
--     GROUP BY person_id, alias_name
--     HAVING COUNT(*) > 1
-- ) d ON a.person_id <=> d.person_id AND a.alias_name <=> d.alias_name
-- WHERE a.id <> d.keep_id;

-- DELETE a FROM previous_addresses a
-- INNER JOIN (
--     SELECT person_id, street, zip_code, MIN(id) AS keep_id
--     FROM previous_addresses
--     GROUP BY person_id, street, zip_code
--     HAVING COUNT(*) > 1
-- ) d ON a.person_id <=> d.person_id AND a.street <=> d.street AND a.zip_code <=> d.zip_code
-- WHERE a.id <> d.keep_id;

-- DELETE a FROM phone_numbers a
-- INNER JOIN (
--     SELECT person_id, phone_number, MIN(id) AS keep_id
--     FROM phone_numbers
--     GROUP BY person_id, phone_number
--     HAVING COUNT(*) > 1
-- ) d ON a.person_id <=> d.person_id AND a.phone_number <=> d.phone_number
-- WHERE a.id <> d.keep_id;

-- DELETE a FROM email_addresses a
-- INNER JOIN (
--     SELECT person_id, email, MIN(id) AS keep_id
--     FROM email_addresses
--     GROUP BY person_id, email
--     HAVING COUNT(*) > 1
-- ) d ON a.person_id <=> d.person_id AND a.email <=> d.email
-- WHERE a.id <> d.keep_id;

-- DELETE a FROM relatives a
-- INNER JOIN (
--     SELECT person_id, relative_name, MIN(id) AS keep_id
--     FROM relatives
--     GROUP BY person_id, relative_name
--     HAVING COUNT(*) > 1
-- ) d ON a.person_id <=> d.person_id AND a.relative_name <=> d.relative_name
-- WHERE a.id <> d.keep_id;

-- DELETE a FROM associates a
-- INNER JOIN (
--     SELECT person_id, associate_name, MIN(id) AS keep_id
--     FROM associates
--     GROUP BY person_id, associate_name
--     HAVING COUNT(*) > 1
-- ) d ON a.person_id <=> d.person_id AND a.associate_name <=> d.associate_name
-- WHERE a.id <> d.keep_id;

-- ------------------------------------------------------------
-- 3. 子表 UNIQUE（保留原有 AUTO_RANDOM PK 与非唯一索引）
-- 重复执行可能报已存在
-- uk_prev_addr 使用 street(191)；若 TiDB 不支持 UNIQUE 前缀长度，
-- 改为：ADD UNIQUE KEY uk_prev_addr (person_id, street, zip_code)
-- ------------------------------------------------------------
ALTER TABLE aliases
    ADD UNIQUE KEY uk_aliases (person_id, alias_name);

ALTER TABLE previous_addresses
    ADD UNIQUE KEY uk_prev_addr (person_id, street(191), zip_code);

ALTER TABLE phone_numbers
    ADD UNIQUE KEY uk_phones (person_id, phone_number);

ALTER TABLE email_addresses
    ADD UNIQUE KEY uk_emails (person_id, email);

ALTER TABLE relatives
    ADD UNIQUE KEY uk_relatives (person_id, relative_name);

ALTER TABLE associates
    ADD UNIQUE KEY uk_associates (person_id, associate_name);

-- ------------------------------------------------------------
-- 4. 单行摘要表（id 固定为 1）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stats_snapshot (
    id          INT PRIMARY KEY,
    persons     BIGINT NOT NULL DEFAULT 0,
    phones      BIGINT NOT NULL DEFAULT 0,
    emails      BIGINT NOT NULL DEFAULT 0,
    prev_addr   BIGINT NOT NULL DEFAULT 0,
    relatives   BIGINT NOT NULL DEFAULT 0,
    associates  BIGINT NOT NULL DEFAULT 0,
    aliases     BIGINT NOT NULL DEFAULT 0,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

INSERT IGNORE INTO stats_snapshot
    (id, persons, phones, emails, prev_addr, relatives, associates, aliases)
VALUES
    (1, 0, 0, 0, 0, 0, 0, 0);
