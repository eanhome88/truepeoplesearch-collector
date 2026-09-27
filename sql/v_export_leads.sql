-- ============================================================
-- TruePeopleSearch 客户精准数据视图与字段迁移脚本
-- 兼容 Navicat 视图浏览与 1键 CSV 导出
-- ============================================================

USE people_search;

-- 1. 为主表补齐中文报表与竞品对标字段
ALTER TABLE persons
    ADD COLUMN IF NOT EXISTS first_name         VARCHAR(100) NULL COMMENT '名',
    ADD COLUMN IF NOT EXISTS middle_name        VARCHAR(100) NULL COMMENT '中间名',
    ADD COLUMN IF NOT EXISTS last_name          VARCHAR(100) NULL COMMENT '姓',
    ADD COLUMN IF NOT EXISTS gender             VARCHAR(10) DEFAULT '未知' COMMENT '性别',
    ADD COLUMN IF NOT EXISTS primary_phone      VARCHAR(30) NULL COMMENT '当前电话(优先无线手机号)',
    ADD COLUMN IF NOT EXISTS primary_phone_type VARCHAR(30) NULL COMMENT '当前电话类型(Wireless/Landline/Voip)',
    ADD COLUMN IF NOT EXISTS current_address    VARCHAR(500) NULL COMMENT '当前完整地址',
    ADD COLUMN IF NOT EXISTS address_duration   VARCHAR(100) NULL COMMENT '当前地址居住时长',
    ADD COLUMN IF NOT EXISTS all_phones         TEXT NULL COMMENT '全部电话列表(逗号分隔)',
    ADD COLUMN IF NOT EXISTS wireless_phone_1   VARCHAR(30) NULL COMMENT '移动号码1',
    ADD COLUMN IF NOT EXISTS wireless_phone_2   VARCHAR(30) NULL COMMENT '移动号码2',
    ADD COLUMN IF NOT EXISTS wireless_phone_3   VARCHAR(30) NULL COMMENT '移动号码3';

-- 2. 清理前期测试由于旧代码写入的 URL 污染数据
DELETE FROM persons WHERE person_id LIKE '%resultphone%' OR person_id LIKE 'http%' OR full_name LIKE '%TruePeopleSearch%';

-- 3. 创建与客户 Navicat 完全一致的中文视图 [人物主表]
CREATE OR REPLACE VIEW 人物主表 AS
SELECT
    person_id          AS `人物ID`,
    full_name          AS `全名`,
    gender             AS `性别`,
    age                AS `年龄`,
    primary_phone      AS `当前电话`,
    primary_phone_type AS `当前电话类型`,
    current_address    AS `当前地址`,
    address_duration   AS `当前地址时长`,
    last_name          AS `姓`,
    first_name         AS `名`,
    middle_name        AS `中间名`,
    all_phones         AS `电话列表`,
    wireless_phone_1   AS `移动号码1`,
    wireless_phone_2   AS `移动号码2`,
    wireless_phone_3   AS `移动号码3`
FROM persons;
