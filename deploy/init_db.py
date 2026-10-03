#!/usr/bin/env python3
"""
TruePeopleSearch 本地数据库一键初始化与结构校验工具
支持 TiDB (默认 4000 端口) 及本地 MySQL 8.0/5.7 (3306 端口)

用法：
  python3 deploy/init_db.py
"""

import os
import sys
import time
import argparse
from pathlib import Path

_ROOT_DIR = Path(__file__).resolve().parent.parent
_SCHEMA_SQL_FILE = _ROOT_DIR / "sql" / "tidb_schema.sql"
if str(_ROOT_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR / "scripts"))

from person_visibility import person_export_view_sql

REQUIRED_TABLES = [
    "persons",
    "aliases",
    "current_addresses",
    "previous_addresses",
    "phone_numbers",
    "email_addresses",
    "relatives",
    "associates",
]


def wait_for_port(host: str, port: int, timeout_sec: int = 30) -> bool:
    import socket
    start = time.time()
    while time.time() - start < timeout_sec:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return True
        except (OSError, socket.error):
            time.sleep(1.0)
    return False


def load_env_file():
    for candidate in (_ROOT_DIR / ".env", _ROOT_DIR / "deploy" / ".env"):
        if candidate.exists():
            try:
                for line in candidate.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass


def run_init(cleanup_invalid_records: bool = False):
    load_env_file()
    host = os.environ.get("TPS_DB_HOST", "127.0.0.1")
    port = int(os.environ.get("TPS_DB_PORT", 4000))
    user = os.environ.get("TPS_DB_USER", "root")
    password = os.environ.get("TPS_DB_PASSWORD", "")
    dbname = os.environ.get("TPS_DB_NAME", "people_search")

    print(f"\n============================================================")
    print(f"  TruePeopleSearch 本地数据库自动部署与结构初始化")
    print(f"============================================================")
    print(f"  • 目标主机: {host}:{port}")
    print(f"  • 用户名:   {user}")
    print(f"  • 目标库名: {dbname}")
    print(f"  • 结构脚本: {_SCHEMA_SQL_FILE.name}")
    print(f"============================================================")

    # 1. 检测端口联通
    print(f"[1/4] 正在检测数据库端口联通性 ({host}:{port})...")
    if not wait_for_port(host, port, timeout_sec=15):
        print(f"  ❌ 配置的数据库服务 ({host}:{port}) 不可用，已中止初始化。")
        print("     请检查 TPS_DB_HOST / TPS_DB_PORT；不会自动切换到其他数据库端口。")
        print("     待目标数据库就绪后，可重新运行: python deploy/init_db.py")
        sys.exit(1)

    print(f"  ✅ 数据库服务端口畅通！")

    try:
        import mysql.connector
    except ImportError:
        print("  ❌ 错误: 未安装 mysql-connector-python，请运行: pip install mysql-connector-python")
        sys.exit(1)

    # 2. 连接并创建数据库
    print(f"[2/4] 检查并创建数据库 [{dbname}]...")
    try:
        conn = mysql.connector.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            connection_timeout=5,
        )
        cursor = conn.cursor()
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{dbname}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
        cursor.close()
        conn.close()
        print(f"  ✅ 数据库 [{dbname}] 确认就绪！")
    except Exception as e:
        print(f"  ❌ 创建数据库失败: {e}")
        sys.exit(1)

    # 3. 执行建表脚本
    print(f"[3/4] 执行 SQL 表结构与索引初始化...")
    if not _SCHEMA_SQL_FILE.exists():
        print(f"  ❌ 错误: 未找到表结构文件 {_SCHEMA_SQL_FILE}")
        sys.exit(1)

    sql_content = _SCHEMA_SQL_FILE.read_text(encoding="utf-8")
    # MySQL 8 没有 TiDB 的 AUTO_RANDOM，建表前换成 AUTO_INCREMENT。
    sql_content = sql_content.replace("AUTO_RANDOM", "AUTO_INCREMENT")
    # 分割 SQL 语句。分号前的注释行不能把后面的 CREATE 整段丢掉。
    raw_statements = sql_content.split(";")
    statements = []
    for stmt in raw_statements:
        lines = [
            line for line in stmt.splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]
        clean = "\n".join(lines).strip()
        if clean:
            statements.append(clean)

    try:
        conn = mysql.connector.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=dbname,
            connection_timeout=10,
        )
        cursor = conn.cursor()
        executed_count = 0
        for stmt in statements:
            try:
                cursor.execute(stmt)
                executed_count += 1
            except mysql.connector.Error as err:
                # 忽略表已存在错误
                if err.errno in (1050,):  # Table already exists
                    continue
                raise

        conn.commit()
        print(f"  ✅ 成功执行 {executed_count} 条 DDL 建表与索引语句！")

        # 3.1 兼容旧表升级：检查并自动补齐缺失的新列与中文视图
        cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'persons'", (dbname,))
        current_cols = set(row[0].lower() for row in cursor.fetchall())
        col_defs = [
            ("first_name", "VARCHAR(100) NULL"),
            ("middle_name", "VARCHAR(100) NULL"),
            ("last_name", "VARCHAR(100) NULL"),
            ("gender", "VARCHAR(10) DEFAULT '未知'"),
            ("primary_phone", "VARCHAR(30) NULL"),
            ("primary_phone_type", "VARCHAR(30) NULL"),
            ("current_address", "VARCHAR(500) NULL"),
            ("address_duration", "VARCHAR(100) NULL"),
            ("all_phones", "TEXT NULL"),
            ("wireless_phone_1", "VARCHAR(30) NULL"),
            ("wireless_phone_2", "VARCHAR(30) NULL"),
            ("wireless_phone_3", "VARCHAR(30) NULL"),
        ]
        for col_name, col_type in col_defs:
            if col_name.lower() not in current_cols:
                try:
                    cursor.execute(f"ALTER TABLE persons ADD COLUMN {col_name} {col_type};")
                    print(f"  • 自动迁移补齐字段: `persons`.`{col_name}`")
                except mysql.connector.Error as alter_err:
                    # A concurrent/idempotent upgrade may have added the column.
                    if alter_err.errno != 1060:  # Duplicate column name
                        raise

        # 3.2 数据删除不属于默认的幂等建表流程。只有管理员显式确认
        # --cleanup-invalid-records 时才执行这项历史数据修复。
        if cleanup_invalid_records:
            try:
                cursor.execute("DELETE FROM persons WHERE person_id LIKE '%resultphone%' OR person_id LIKE 'http%' OR full_name LIKE '%TruePeopleSearch%';")
                deleted = cursor.rowcount
                if deleted > 0:
                    print(f"  🧹 已清理前期测试残留的 {deleted} 条无效记录！")
            except Exception as cleanup_err:
                raise RuntimeError(f"历史无效记录清理失败: {cleanup_err}") from cleanup_err

        # 3.3 确保创建客户专属中文视图 [人物主表]
        try:
            view_sql = person_export_view_sql()
            cursor.execute(view_sql)
            print("  ✅ 客户专属视图 `人物主表` (对标竞品底表) 校验就绪！")
        except Exception as v_err:
            raise RuntimeError(f"创建人物主表视图失败: {v_err}") from v_err

        conn.commit()

        # 4. 校验表完整性
        print(f"[4/4] 验证数据库表结构完整性...")
        cursor.execute("SHOW TABLES;")
        existing_tables = set(row[0].lower() for row in cursor.fetchall())
        missing = [t for t in REQUIRED_TABLES if t.lower() not in existing_tables]

        if missing:
            print(f"  ❌ 缺少以下关键数据表: {', '.join(missing)}")
            sys.exit(1)

        print(f"  ✅ 全部 {len(REQUIRED_TABLES)} 张核心数据表已全部建立并校验通过:")
        for t in REQUIRED_TABLES:
            print(f"     • `{t}` [OK]")

        cursor.close()
        conn.close()

        print(f"\n✅ 本地数据库结构已初始化并校验完成。")
        print("注意：数据库结构就绪不代表采集成功、入库正常或吞吐达标。\n")

    except Exception as e:
        print(f"  ❌ 数据库表结构初始化失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialize and verify the configured MySQL schema.")
    parser.add_argument(
        "--cleanup-invalid-records",
        action="store_true",
        help="explicitly delete known historical URL-pollution rows after schema setup",
    )
    arguments = parser.parse_args()
    run_init(cleanup_invalid_records=arguments.cleanup_invalid_records)
