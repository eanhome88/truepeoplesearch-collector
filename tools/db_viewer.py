#!/usr/bin/env python3
"""
数据库查看工具 — 命令行交互式查询 TiDB 中的人物数据

依赖：pip install mysql-connector-python tabulate

使用：
  python3 db_viewer.py                          # 交互式菜单
  python3 db_viewer.py --search "Jamie Perez"   # 按姓名搜索
  python3 db_viewer.py --phone "(303) 210-9670" # 按电话反查
  python3 db_viewer.py --email "gmail.com"      # 按邮箱反查
  python3 db_viewer.py --city "Thornton"         # 按城市查
  python3 db_viewer.py --stats                   # 统计信息
  python3 db_viewer.py --person "px82l44nur68u2l2l8n60"  # 查看某人全部信息
"""

import argparse
import mysql.connector
from tabulate import tabulate

# ============================================================
# TiDB 连接配置 — 和 scrape_to_tidb.py 保持一致
# ============================================================
TIDB_CONFIG = {
    "host": "127.0.0.1",
    "port": 4000,
    "user": "root",
    "password": "",
    "database": "people_search",
}


def get_db():
    return mysql.connector.connect(**TIDB_CONFIG)


def print_table(cursor, title=""):
    """打印查询结果为表格"""
    rows = cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    if title:
        print(f"\n{'='*60}")
        print(f"  {title}")
        print(f"{'='*60}")
    if rows:
        print(tabulate(rows, headers=cols, tablefmt="grid", maxcolwidth=40))
    else:
        print("  (no data)")
    print(f"  {len(rows)} rows")


# ============================================================
# 查询函数
# ============================================================

def search_by_name(name: str):
    """按姓名模糊搜索"""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT person_id, full_name, age, current_city, current_state
        FROM persons
        WHERE full_name LIKE %s
        ORDER BY full_name
        LIMIT 50
    """, (f"%{name}%",))
    print_table(cur, f"Search: '{name}'")
    db.close()


def search_by_phone(phone: str):
    """按电话号码反查"""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT p.person_id, p.full_name, p.age, p.current_city,
               ph.phone_number, ph.line_type, ph.carrier, ph.last_reported
        FROM persons p
        JOIN phone_numbers ph ON p.person_id = ph.person_id
        WHERE ph.phone_number LIKE %s
    """, (f"%{phone}%",))
    print_table(cur, f"Phone: '{phone}'")
    db.close()


def search_by_email(email: str):
    """按邮箱反查"""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT p.person_id, p.full_name, p.age, p.current_city,
               e.email
        FROM persons p
        JOIN email_addresses e ON p.person_id = e.person_id
        WHERE e.email LIKE %s
        LIMIT 50
    """, (f"%{email}%",))
    print_table(cur, f"Email: '{email}'")
    db.close()


def search_by_city(city: str):
    """按城市查所有人"""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT person_id, full_name, age, current_zip
        FROM persons
        WHERE current_city = %s
        ORDER BY full_name
        LIMIT 100
    """, (city,))
    print_table(cur, f"City: '{city}'")
    db.close()


def show_stats():
    """统计信息"""
    db = get_db()
    cur = db.cursor()

    cur.execute("SELECT COUNT(*) FROM persons")
    total = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM phone_numbers")
    phones = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM email_addresses")
    emails = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM previous_addresses")
    prev_addr = cur.fetchone()[0]

    print(f"\n{'='*60}")
    print(f"  DATABASE STATISTICS")
    print(f"{'='*60}")
    print(f"  Total persons:      {total:>10,}")
    print(f"  Phone numbers:     {phones:>10,}")
    print(f"  Email addresses:   {emails:>10,}")
    print(f"  Previous addresses: {prev_addr:>9,}")

    # Top 10 城市
    cur.execute("""
        SELECT current_city, current_state, COUNT(*) as cnt
        FROM persons
        WHERE current_city IS NOT NULL
        GROUP BY current_city, current_state
        ORDER BY cnt DESC
        LIMIT 10
    """)
    print_table(cur, "Top 10 Cities")

    # 年龄分布
    cur.execute("""
        SELECT
            CASE
                WHEN age < 20 THEN '0-19'
                WHEN age < 30 THEN '20-29'
                WHEN age < 40 THEN '30-39'
                WHEN age < 50 THEN '40-49'
                WHEN age < 60 THEN '50-59'
                WHEN age < 70 THEN '60-69'
                ELSE '70+'
            END as age_group,
            COUNT(*) as cnt
        FROM persons
        WHERE age IS NOT NULL
        GROUP BY age_group
        ORDER BY age_group
    """)
    print_table(cur, "Age Distribution")

    db.close()


def show_person_detail(person_id: str):
    """查看某人全部信息"""
    db = get_db()
    cur = db.cursor()

    # 基本信息
    cur.execute("SELECT * FROM v_person_full_profile WHERE person_id = %s", (person_id,))
    print_table(cur, f"Person: {person_id}")

    # 别名
    cur.execute("SELECT alias_name FROM aliases WHERE person_id = %s", (person_id,))
    print_table(cur, "Aliases")

    # 电话
    cur.execute("""
        SELECT phone_number, line_type, carrier, is_primary, last_reported
        FROM phone_numbers WHERE person_id = %s
    """, (person_id,))
    print_table(cur, "Phone Numbers")

    # 邮箱
    cur.execute("SELECT email FROM email_addresses WHERE person_id = %s", (person_id,))
    print_table(cur, "Email Addresses")

    # 当前地址
    cur.execute("SELECT * FROM current_addresses WHERE person_id = %s", (person_id,))
    print_table(cur, "Current Address")

    # 过往地址
    cur.execute("""
        SELECT street, city, state, zip_code, county, lived_from, lived_to
        FROM previous_addresses WHERE person_id = %s
    """, (person_id,))
    print_table(cur, "Previous Addresses")

    db.close()


# ============================================================
# 交互式菜单
# ============================================================

def interactive_menu():
    """交互式菜单"""
    while True:
        print(f"\n{'='*60}")
        print("  TruePeopleSearch Database Viewer")
        print(f"{'='*60}")
        print("  1. Search by name")
        print("  2. Search by phone number")
        print("  3. Search by email")
        print("  4. Search by city")
        print("  5. Show person detail (by person_id)")
        print("  6. Show database statistics")
        print("  7. Show top 10 cities")
        print("  8. Show age distribution")
        print("  0. Exit")
        print(f"{'='*60}")

        choice = input("  Choice: ").strip()

        if choice == "1":
            name = input("  Name: ").strip()
            if name:
                search_by_name(name)
        elif choice == "2":
            phone = input("  Phone (e.g. 303-210-9670): ").strip()
            if phone:
                search_by_phone(phone)
        elif choice == "3":
            email = input("  Email: ").strip()
            if email:
                search_by_email(email)
        elif choice == "4":
            city = input("  City: ").strip()
            if city:
                search_by_city(city)
        elif choice == "5":
            pid = input("  Person ID: ").strip()
            if pid:
                show_person_detail(pid)
        elif choice == "6":
            show_stats()
        elif choice == "7":
            db = get_db()
            cur = db.cursor()
            cur.execute("""
                SELECT current_city, current_state, COUNT(*) as cnt
                FROM persons WHERE current_city IS NOT NULL
                GROUP BY current_city, current_state
                ORDER BY cnt DESC LIMIT 10
            """)
            print_table(cur, "Top 10 Cities")
            db.close()
        elif choice == "8":
            db = get_db()
            cur = db.cursor()
            cur.execute("""
                SELECT
                    CASE
                        WHEN age < 20 THEN '0-19'
                        WHEN age < 30 THEN '20-29'
                        WHEN age < 40 THEN '30-39'
                        WHEN age < 50 THEN '40-49'
                        WHEN age < 60 THEN '50-59'
                        WHEN age < 70 THEN '60-69'
                        ELSE '70+'
                    END as age_group,
                    COUNT(*) as cnt
                FROM persons WHERE age IS NOT NULL
                GROUP BY age_group ORDER BY age_group
            """)
            print_table(cur, "Age Distribution")
            db.close()
        elif choice == "0":
            print("  Bye!")
            break
        else:
            print("  Invalid choice")


# ============================================================
# CLI 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Database Viewer")
    parser.add_argument("--search", help="search by name")
    parser.add_argument("--phone", help="search by phone number")
    parser.add_argument("--email", help="search by email")
    parser.add_argument("--city", help="search by city")
    parser.add_argument("--stats", action="store_true", help="show statistics")
    parser.add_argument("--person", help="show person detail by person_id")
    args = parser.parse_args()

    if args.search:
        search_by_name(args.search)
    elif args.phone:
        search_by_phone(args.phone)
    elif args.email:
        search_by_email(args.email)
    elif args.city:
        search_by_city(args.city)
    elif args.stats:
        show_stats()
    elif args.person:
        show_person_detail(args.person)
    else:
        interactive_menu()


if __name__ == "__main__":
    main()
