# -*- coding: utf-8 -*-
"""Offline dashboard demo fixture. Never writes to a database or Redis.

The old seed command mixed demonstration records and metrics into the live
namespace. This compatibility entry point now prints explicitly labelled,
synthetic aggregate JSON only. Pipe it to a local mock server when needed;
the production dashboard does not load this fixture automatically.
"""
import json


def build_demo_fixture():
    return {
        "demo": True,
        "notice": "仅供离线界面测试：不是实际运行或入库数据",
        "stats": {
            "persons": 0,
            "phones": 0,
            "emails": 0,
            "prev_addr": 0,
            "aliases": 0,
            "total_tasks_executed": 10,
            "success_tasks": 8,
            "success_rate_pct": 80.0,
            "dedup_saved_count": 0,
            "current_qps": 0.0,
            "avg_latency_ms": 100.0,
            "traffic_saved_mb": None,
            "traffic_saved_gb": None,
            "traffic_saved_ratio_pct": None,
            "metrics_available": True,
            "throughput_available": True,
            "database_available": False,
            "metrics_quality": {"status": "demo", "issues": ["offline_demo_fixture"]},
        },
    }


def seed_data():
    """Keep the old command callable, without any live-service side effects."""
    print(json.dumps(build_demo_fixture(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    seed_data()
