#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TruePeopleSearch 客户精准数据导出工具 (对标竞品底表格式)
支持导出为 Excel/CSV (UTF-8-SIG 编码，原生防止中文在 Excel 中乱码)

用法:
  python tools/export_csv.py                          # 导出全部数据到 export_leads.csv
  python tools/export_csv.py --limit 1000             # 导出前 1000 条
  python tools/export_csv.py --output my_leads.csv    # 指定导出路径
  python tools/export_csv.py --only-wireless          # 仅导出有手机号的有效线索
"""

import argparse
import csv
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from person_visibility import person_has_phone_sql, person_has_wireless_sql, visible_phone_fields_sql

def load_env():
    for f in (ROOT / ".env", ROOT / "deploy" / ".env"):
        if f.exists():
            try:
                for line in f.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass

def get_db():
    load_env()
    import mysql.connector
    return mysql.connector.connect(
        host=os.environ.get("TPS_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("TPS_DB_PORT", 3306)),
        user=os.environ.get("TPS_DB_USER", "root"),
        password=os.environ.get("TPS_DB_PASSWORD", ""),
        database=os.environ.get("TPS_DB_NAME", "people_search"),
        connection_timeout=10,
    )

EXPORT_HEADERS = [
    "人物ID", "全名", "性别", "年龄", "当前电话", "当前电话类型",
    "当前地址", "当前地址时长", "姓", "名", "中间名",
    "电话列表", "移动号码1", "移动号码2", "移动号码3",
]

def export_leads(output_path: str, limit: int = None, only_wireless: bool = False):
    print("=" * 65)
    print("   📊 TruePeopleSearch 客户情报数据一键导出工具")
    print("=" * 65)

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    where_clauses = [
        "p.person_id NOT LIKE 'http%'", "p.person_id NOT LIKE '%resultphone%'",
        person_has_phone_sql(),
    ]
    if only_wireless:
        where_clauses.append(person_has_wireless_sql())

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    limit_sql = f"LIMIT {limit}" if limit else ""
    phones = visible_phone_fields_sql()

    query_sql = f"""
    SELECT
        person_id          AS `人物ID`,
        full_name          AS `全名`,
        IFNULL(gender, '未知') AS `性别`,
        age                AS `年龄`,
        {phones['primary_phone']} AS `当前电话`,
        {phones['primary_phone_type']} AS `当前电话类型`,
        current_address    AS `当前地址`,
        address_duration   AS `当前地址时长`,
        last_name          AS `姓`,
        first_name         AS `名`,
        middle_name        AS `中间名`,
        {phones['all_phones']} AS `电话列表`,
        {phones['wireless_phone_1']} AS `移动号码1`,
        {phones['wireless_phone_2']} AS `移动号码2`,
        {phones['wireless_phone_3']} AS `移动号码3`
    FROM (
        SELECT p.* FROM persons p {where_sql}
        ORDER BY p.scraped_at DESC {limit_sql}
    ) p
    ORDER BY p.scraped_at DESC;
    """

    print(f"  • 正在从数据库提取客户数据 (条件: {where_sql or '全量'})...")
    cursor.execute(query_sql)

    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with open(out_file, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(EXPORT_HEADERS)

        while True:
            rows = cursor.fetchmany(1000)
            if not rows:
                break
            for r in rows:
                writer.writerow([r.get(h) or "" for h in EXPORT_HEADERS])
                count += 1
            print(f"  • 已导出 {count:,} 条记录...", end="\r", flush=True)

    cursor.close()
    conn.close()

    print(f"\n\n  ✅ 导出圆满完成！")
    print(f"  • 文件保存路径: {out_file.resolve()}")
    print(f"  • 成功导出总数: {count:,} 条有效档案")
    print(f"  • 编码格式: UTF-8 with BOM (Excel 打开即用，汉字绝无乱码)")
    print("=" * 65)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export TPS Leads to CSV")
    parser.add_argument("--output", default="export_leads.csv", help="输出 CSV 文件路径")
    parser.add_argument("--limit", type=int, default=None, help="最大导出条数")
    parser.add_argument("--only-wireless", action="store_true", help="仅导出包含移动手机号的档案")
    args = parser.parse_args()

    export_leads(args.output, args.limit, args.only_wireless)
