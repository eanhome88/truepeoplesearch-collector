-- ============================================================
-- TiDB 建表方案 — TruePeopleSearch 人物数据
-- 兼容 MySQL 8.0 协议，直接在 TiDB / MySQL 中执行
-- 数据库：people_search
--
-- 生产环境建议对大表使用 PRE_SPLIT_REGIONS
-- （例如配合 SHARD_ROW_ID_BITS / PRE_SPLIT_REGIONS 预拆分 Region），
-- 此处不写强制语句，避免 Docker 单机 / 非 TiDB 环境执行失败。
-- MySQL 8 请将 AUTO_RANDOM 替换为 AUTO_INCREMENT。
-- ============================================================

CREATE DATABASE IF NOT EXISTS people_search;
USE people_search;

-- 1. 人物主表
CREATE TABLE IF NOT EXISTS persons (
    person_id          VARCHAR(64) PRIMARY KEY,   -- TruePeopleSearch 内部 ID
    full_name          VARCHAR(200) NOT NULL,
    age                INT,
    birth_month        INT,                        -- 出生月（1-12）
    birth_year         INT,                        -- 出生年
    current_city       VARCHAR(100),
    current_state      VARCHAR(10),
    current_zip        VARCHAR(20),
    marital_status     VARCHAR(20),                -- 已知婚姻状态
    source_url         TEXT,
    content_hash       VARCHAR(64) NULL,           -- 内容哈希，便于变更检测
    phone_count        INT NOT NULL DEFAULT 0,     -- 冗余计数
    email_count        INT NOT NULL DEFAULT 0,
    alias_count        INT NOT NULL DEFAULT 0,
    relative_count     INT NOT NULL DEFAULT 0,
    associate_count    INT NOT NULL DEFAULT 0,
    prev_addr_count    INT NOT NULL DEFAULT 0,
    scraped_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_persons_name (full_name),
    INDEX idx_persons_city (current_city, current_state),
    INDEX idx_persons_age (age),
    INDEX idx_persons_name_id (full_name, person_id)  -- 面板 keyset 翻页
);

-- 2. 别名表（1:N）
CREATE TABLE IF NOT EXISTS aliases (
    id         BIGINT AUTO_RANDOM PRIMARY KEY,
    person_id  VARCHAR(64),
    alias_name VARCHAR(200) NOT NULL,
    UNIQUE KEY uk_aliases (person_id, alias_name),
    INDEX idx_aliases_person (person_id),
    INDEX idx_aliases_name (alias_name)
);

-- 3. 当前地址 + 房产详情（1:1）
CREATE TABLE IF NOT EXISTS current_addresses (
    person_id          VARCHAR(64) PRIMARY KEY,
    street             VARCHAR(200),
    unit               VARCHAR(50),
    city               VARCHAR(100),
    state              VARCHAR(10),
    zip_code           VARCHAR(20),
    county             VARCHAR(100),
    estimated_value    DECIMAL(12,2),
    estimated_equity   DECIMAL(12,2),
    bedrooms           VARCHAR(10),
    bathrooms          INT,
    square_feet        INT,
    year_built         INT,
    last_sale_amount   DECIMAL(12,2),
    last_sale_date     DATE,
    occupancy_type     VARCHAR(50),
    ownership_type     VARCHAR(50),
    land_use           VARCHAR(100),
    property_class     VARCHAR(50),
    subdivision        VARCHAR(200),
    lot_square_feet    INT,
    apn                VARCHAR(100),
    school_district    VARCHAR(200),
    hoa_fee_monthly    DECIMAL(10,2),
    lived_since        DATE,
    lived_until        DATE
);

-- 4. 过往地址表（1:N）
-- uk_prev_addr 使用 street(191) 前缀以兼容较短索引上限；
-- 若目标 TiDB 不支持 UNIQUE 前缀长度，改为 (person_id, street, zip_code)。
CREATE TABLE IF NOT EXISTS previous_addresses (
    id          BIGINT AUTO_RANDOM PRIMARY KEY,
    person_id   VARCHAR(64),
    street      VARCHAR(200),
    unit        VARCHAR(50),
    city        VARCHAR(100),
    state       VARCHAR(10),
    zip_code    VARCHAR(20),
    county      VARCHAR(100),
    lived_from  DATE,
    lived_to    DATE,
    UNIQUE KEY uk_prev_addr (person_id, street(191), zip_code),
    INDEX idx_prev_addr_person (person_id),
    INDEX idx_prev_addr_location (city, state)
);

-- 5. 电话号码表（1:N）
CREATE TABLE IF NOT EXISTS phone_numbers (
    id              BIGINT AUTO_RANDOM PRIMARY KEY,
    person_id       VARCHAR(64),
    phone_number    VARCHAR(20) NOT NULL,
    line_type       VARCHAR(20),           -- Wireless / Landline
    carrier         VARCHAR(100),           -- AT&T / T-Mobile 等
    is_primary      BOOLEAN DEFAULT FALSE,
    last_reported    DATE,
    UNIQUE KEY uk_phones (person_id, phone_number),
    INDEX idx_phones_person (person_id),
    INDEX idx_phones_number (phone_number)
);

-- 6. 邮箱表（1:N）
CREATE TABLE IF NOT EXISTS email_addresses (
    id          BIGINT AUTO_RANDOM PRIMARY KEY,
    person_id   VARCHAR(64),
    email       VARCHAR(200) NOT NULL,
    UNIQUE KEY uk_emails (person_id, email),
    INDEX idx_emails_person (person_id),
    INDEX idx_emails_address (email)
);

-- 7. 亲属表（1:N）
CREATE TABLE IF NOT EXISTS relatives (
    id            BIGINT AUTO_RANDOM PRIMARY KEY,
    person_id     VARCHAR(64),
    relative_name VARCHAR(200) NOT NULL,
    relative_age  INT,
    UNIQUE KEY uk_relatives (person_id, relative_name),
    INDEX idx_relatives_person (person_id),
    INDEX idx_relatives_name (relative_name)
);

-- 8. 关联人表（1:N）
CREATE TABLE IF NOT EXISTS associates (
    id              BIGINT AUTO_RANDOM PRIMARY KEY,
    person_id       VARCHAR(64),
    associate_name  VARCHAR(200) NOT NULL,
    associate_age   INT,
    UNIQUE KEY uk_associates (person_id, associate_name),
    INDEX idx_associates_person (person_id),
    INDEX idx_associates_name (associate_name)
);

-- 9. 单行摘要（id 固定为 1）
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

-- ============================================================
-- 视图：一人全貌（便于快速查询）
-- ============================================================
CREATE OR REPLACE VIEW v_person_full_profile AS
SELECT
    p.person_id, p.full_name, p.age, p.birth_month, p.birth_year,
    p.current_city, p.current_state, p.marital_status,
    ca.street AS current_street, ca.unit AS current_unit,
    ca.zip_code AS current_zip, ca.county AS current_county,
    ca.estimated_value, ca.square_feet, ca.year_built, ca.apn,
    (SELECT GROUP_CONCAT(phone_number) FROM phone_numbers WHERE person_id = p.person_id) AS all_phones,
    (SELECT GROUP_CONCAT(email) FROM email_addresses WHERE person_id = p.person_id) AS all_emails,
    (SELECT GROUP_CONCAT(alias_name) FROM aliases WHERE person_id = p.person_id) AS all_aliases,
    (SELECT COUNT(*) FROM previous_addresses WHERE person_id = p.person_id) AS prev_address_count,
    (SELECT COUNT(*) FROM relatives WHERE person_id = p.person_id) AS relative_count,
    (SELECT COUNT(*) FROM associates WHERE person_id = p.person_id) AS associate_count
FROM persons p
LEFT JOIN current_addresses ca ON p.person_id = ca.person_id;
