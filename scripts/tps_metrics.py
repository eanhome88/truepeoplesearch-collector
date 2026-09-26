#!/usr/bin/env python3
"""
多 Worker 可汇总的 Redis 计数器 + Prometheus 文本暴露。

键前缀：
  tps:metrics:counter:{bucket}
  tps:metrics:lat:count:{metric}
  tps:metrics:lat:sum:{metric}

不依赖 prometheus_client，不连接 TiDB。
r 的 decode_responses 为 True/False 均可。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional

BUCKETS = (
    "attempt",
    "success",
    "empty",
    "http_4xx",
    "rate_limit",
    "cf_fail",
    "parse_fail",
    "write_fail",
    "dedup_hit",
    "retry",
    "dlq",
)

LATENCY_METRICS = ("scrape_ms", "write_ms", "queue_wait_ms", "cf_solve_ms")

COUNTER_KEY = "tps:metrics:counter:{bucket}"
LAT_COUNT_KEY = "tps:metrics:lat:count:{metric}"
LAT_SUM_KEY = "tps:metrics:lat:sum:{metric}"

_BUCKET_SET = frozenset(BUCKETS)
_LATENCY_SET = frozenset(LATENCY_METRICS)

# These buckets are mutually exclusive primary outcomes for one scrape/ingest
# attempt.  ``retry`` and ``dlq`` are lifecycle counters which can be emitted in
# addition to a primary failure bucket, while ``dedup_hit`` is not an attempt.
# Keeping them out of this set prevents the denominator from double counting a
# single job outcome.
_ATTEMPT_OUTCOME_BUCKETS = frozenset(
    {
        "success",
        "empty",
        "http_4xx",
        "rate_limit",
        "cf_fail",
        "parse_fail",
        "write_fail",
    }
)


def _as_str(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return int(float(_as_str(value)))


def _as_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return float(_as_str(value))


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _prom_num(value: float) -> str:
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return str(int(value))
    return repr(float(value))


class Metrics:
    def __init__(self, r):
        self.r = r

    def incr(self, bucket: str, n: int = 1) -> None:
        requested_bucket = _as_str(bucket)
        bucket = requested_bucket
        unknown_outcome = bucket not in _BUCKET_SET
        if unknown_outcome:
            bucket = "retry"
        n = int(n)
        if n == 0:
            return
        count_attempt = bucket in _ATTEMPT_OUTCOME_BUCKETS or unknown_outcome
        if count_attempt:
            pipe = self.r.pipeline(transaction=True)
            pipe.incrby(COUNTER_KEY.format(bucket=bucket), n)
            pipe.incrby(COUNTER_KEY.format(bucket="attempt"), n)
            pipe.execute()
            return
        self.r.incrby(COUNTER_KEY.format(bucket=bucket), n)

    def observe_ms(self, metric: str, value_ms: float) -> None:
        metric = _as_str(metric)
        if metric not in _LATENCY_SET:
            raise ValueError(f"unknown metric: {metric}")
        count_key = LAT_COUNT_KEY.format(metric=metric)
        sum_key = LAT_SUM_KEY.format(metric=metric)
        value_ms = float(value_ms)
        pipe = getattr(self.r, "pipeline", None)
        if callable(pipe):
            p = self.r.pipeline()
            p.incrby(count_key, 1)
            p.incrbyfloat(sum_key, value_ms)
            p.execute()
            return
        self.r.incrby(count_key, 1)
        self.r.incrbyfloat(sum_key, value_ms)

    def snapshot(self) -> dict:
        counters: Dict[str, int] = {}
        latency: Dict[str, Dict[str, Any]] = {}

        counter_keys = [COUNTER_KEY.format(bucket=b) for b in BUCKETS]
        lat_count_keys = [LAT_COUNT_KEY.format(metric=m) for m in LATENCY_METRICS]
        lat_sum_keys = [LAT_SUM_KEY.format(metric=m) for m in LATENCY_METRICS]
        values = self._mget(counter_keys + lat_count_keys + lat_sum_keys)

        n_buckets = len(BUCKETS)
        n_lat = len(LATENCY_METRICS)
        for i, bucket in enumerate(BUCKETS):
            counters[bucket] = _as_int(values[i])

        count_offset = n_buckets
        sum_offset = n_buckets + n_lat
        for i, metric in enumerate(LATENCY_METRICS):
            count = _as_int(values[count_offset + i])
            total = _as_float(values[sum_offset + i])
            avg = (total / count) if count else 0.0
            latency[metric] = {"count": count, "sum": total, "avg": avg}

        attempt = counters["attempt"]
        success = counters["success"]
        completed = success + counters["empty"]
        success_rate_pct = (success / attempt * 100.0) if attempt else 0.0
        completion_rate_pct = (completed / attempt * 100.0) if attempt else 0.0

        return {
            "counters": counters,
            "latency": latency,
            "success_rate_pct": success_rate_pct,
            "completion_rate_pct": completion_rate_pct,
            "ts": int(time.time()),
        }

    def render_prometheus(self) -> str:
        snap = self.snapshot()
        lines: List[str] = [
            "# HELP tps_jobs_total Total jobs by outcome bucket",
            "# TYPE tps_jobs_total counter",
        ]
        for bucket in BUCKETS:
            lines.append(
                f'tps_jobs_total{{bucket="{_escape_label(bucket)}"}} '
                f"{_prom_num(snap['counters'][bucket])}"
            )

        lines.extend(
            [
                "# HELP tps_latency_ms_sum Latency milliseconds sum",
                "# TYPE tps_latency_ms_sum counter",
            ]
        )
        for metric in LATENCY_METRICS:
            lines.append(
                f'tps_latency_ms_sum{{metric="{_escape_label(metric)}"}} '
                f"{_prom_num(snap['latency'][metric]['sum'])}"
            )

        lines.extend(
            [
                "# HELP tps_latency_ms_count Latency observation count",
                "# TYPE tps_latency_ms_count counter",
            ]
        )
        for metric in LATENCY_METRICS:
            lines.append(
                f'tps_latency_ms_count{{metric="{_escape_label(metric)}"}} '
                f"{_prom_num(snap['latency'][metric]['count'])}"
            )

        return "\n".join(lines) + "\n"

    def _mget(self, keys: Iterable[str]) -> List[Optional[Any]]:
        key_list = list(keys)
        if not key_list:
            return []
        mget = getattr(self.r, "mget", None)
        if callable(mget):
            try:
                result = mget(key_list)
            except TypeError:
                result = mget(*key_list)
            if result is None:
                return [None] * len(key_list)
            return list(result)
        getter = getattr(self.r, "get")
        return [getter(k) for k in key_list]


def get_metrics(r) -> Metrics:
    return Metrics(r)
