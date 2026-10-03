#!/usr/bin/env python3
"""Read-only local dependency preflight; never repairs configuration or scrapes."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from tps_env import load_project_env


def main() -> int:
    load_project_env(ROOT, customer_safe=False)
    failed = False
    try:
        import redis

        queue = redis.Redis(
            host=os.environ.get("TPS_REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("TPS_REDIS_PORT", "6379")),
            password=os.environ.get("TPS_REDIS_PASSWORD") or None,
            socket_connect_timeout=3,
            socket_timeout=3,
            decode_responses=True,
        )
        queue.ping()
        print(f"Redis: ready; pending={queue.llen('tps:pending')}")
    except Exception:
        print("Redis: unavailable or configuration invalid")
        failed = True

    try:
        import mysql.connector

        connection = mysql.connector.connect(
            host=os.environ.get("TPS_DB_HOST", "127.0.0.1"),
            port=int(os.environ.get("TPS_DB_PORT", "4000")),
            user=os.environ.get("TPS_DB_USER", "root"),
            password=os.environ.get("TPS_DB_PASSWORD", ""),
            database=os.environ.get("TPS_DB_NAME", "people_search"),
            connection_timeout=3,
        )
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT COUNT(*) FROM persons")
            print(f"Database: ready; persons={cursor.fetchone()[0]}")
        finally:
            connection.close()
    except Exception:
        print("Database: unavailable, missing table, or configuration invalid")
        failed = True

    proxy_configured = bool(
        os.environ.get("PROXY_TUNNEL")
        or os.environ.get("PROXY_FILE")
        or os.environ.get("PROXY_API")
    )
    print(f"Proxy: {'configured' if proxy_configured else 'not configured'}; no outbound request made")
    print("This preflight does not modify .env, start a collector, or claim ingestion success.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
