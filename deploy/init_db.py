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
from pathlib import Path

_ROOT_DIR = Path(__file__).resolve().parent.parent
_SCHEMA_SQL_FILE = _ROOT_DIR / "sql" / "tidb_schema.sql"

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


def run_init():
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
        # 尝试端口 3306 提示
        if port == 4000 and wait_for_port(host, 3306, timeout_sec=1):
            print(f"  ⚠️ 发现端口 3306 (标准MySQL) 处于开启状态，尝试切换至 3306...")
            port = 3306
        else:
            print(f"  ℹ️ 提示: 本地数据库服务 ({host}:{port}) 暂未启动。")
            print(f"     若使用 Docker，请确保 Docker Desktop 处于运行状态。")
            print(f"     待数据库就绪后，可随时运行: python deploy/init_db.py 完成表结构初始化。")
            return

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
    # 分割 SQL 语句 (处理分号与注释)
    raw_statements = sql_content.split(";")
    statements = []
    for stmt in raw_statements:
        clean = stmt.strip()
        if clean and not clean.startswith("--"):
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
                # TiFlash 语法在单机 TiDB 可能会报提示，忽略非致命性
                if "tiflash" in stmt.lower():
                    continue
                print(f"  ⚠️ 执行语句警告: {err}")

        conn.commit()
        print(f"  ✅ 成功执行 {executed_count} 条 DDL 建表与索引语句！")

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

        print(f"\n🎉 恭喜！本地数据库已成功初始化完毕，随时可投入高并发采集运行！\n")

    except Exception as e:
        print(f"  ❌ 数据库表结构初始化失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    run_init()
