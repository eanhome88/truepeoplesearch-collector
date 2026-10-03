#!/usr/bin/env python3
"""Read-only local proxy configuration diagnostic.

It does not contact a website, change Redis, or write customer data. A live
collection test belongs in the separately authorized acceptance procedure.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from proxy_pool import load_proxy_config  # noqa: E402


def main() -> int:
    try:
        import redis

        connection = redis.Redis(
            host=os.environ.get("TPS_REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("TPS_REDIS_PORT", "6379")),
            password=os.environ.get("TPS_REDIS_PASSWORD") or os.environ.get("REDIS_PASSWORD"),
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        cfg = load_proxy_config(connection)
    except (ImportError, OSError, ValueError):
        cfg = load_proxy_config()
    mode = cfg.get("mode", "direct")
    tunnel = cfg.get("tunnel") or ""
    parsed = urlsplit(tunnel) if tunnel else None
    print(f"proxy_mode={mode}")
    print(f"proxy_configured={bool(tunnel)}")
    print(f"proxy_password_present={bool(parsed and parsed.password)}")
    print("No website request or database write was made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
