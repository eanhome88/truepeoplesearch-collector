#!/usr/bin/env python3
"""Legacy compatibility entry for a safe, read-only database preflight.

Database configuration and schema repairs require an explicit, reviewed
migration.  This command never rewrites source, environment files, or processes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from tps_env import load_project_env


def main() -> int:
    load_project_env(ROOT, customer_safe=False)
    try:
        import mysql.connector

        host = os.environ.get("TPS_DB_HOST") or os.environ.get("TIDB_HOST") or "127.0.0.1"
        port = int(os.environ.get("TPS_DB_PORT") or os.environ.get("TIDB_PORT") or "3306")
        user = os.environ.get("TPS_DB_USER") or os.environ.get("TIDB_USER") or "root"
        password = os.environ.get("TPS_DB_PASSWORD") if "TPS_DB_PASSWORD" in os.environ else (os.environ.get("TIDB_PASSWORD") if "TIDB_PASSWORD" in os.environ else "")
        database = os.environ.get("TPS_DB_NAME") or os.environ.get("TIDB_DATABASE") or "people_search"

        print(f"[*] 正在尝试连接数据库 {user}@{host}:{port}/{database} ...")
        connection = mysql.connector.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            connection_timeout=4,
        )
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT 1")
            if cursor.fetchone() != (1,):
                raise RuntimeError("Unexpected database response")
        finally:
            connection.close()
    except Exception as exc:
        print(f"[FAIL] 数据库连接失败: {exc}")
        print(f"       请确认 MariaDB 是否已启动监听 {port} 端口。")
        return 1

    print("[OK] 数据库连接成功！MariaDB (3306) 运行正常！")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
