#!/usr/bin/env python3
"""
2.5 亿全档的任务框架。目标是一天 300 万条人物全档。

人物页不按渲染计时。浏览器打开并过一次验证后，后续文档走这个
浏览器的协议请求。一路 = 一个粘性出口 + 一个常驻浏览器。
姓氏按稳定哈希分到路。目录卡片不能代替人物页。
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, List

from tps_scale import DAILY_TARGET

PLAN_KEY = "tps:plan"
SITE_UNIVERSE = 250_000_000
# fetch_document 实测往返，挑战页也会在这个时间返回。
PROTOCOL_SEC = 1.2
DEFAULT_INFLIGHT = 1
MAX_INFLIGHT = 16
MAX_LANES = 2000


def _as_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def assign_lane(key: str, lanes: int) -> int:
    """同一姓氏永远落在同一路。lanes < 1 时全部算第 0 路。"""
    n = int(lanes)
    if n <= 1:
        return 0
    text = (key or "").strip().lower()
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % n


def daily_pages(active_pages: int, page_sec: float) -> int:
    pages = max(0, int(active_pages))
    sec = float(page_sec)
    if pages <= 0 or sec <= 0:
        return 0
    return int(pages / sec * 86400)


def days_for(records: int, per_day: int) -> int | None:
    left = max(0, int(records))
    rate = int(per_day)
    if left <= 0:
        return 0
    if rate <= 0:
        return None
    return int(math.ceil(left / rate))


def inflight_for_target(page_sec: float, per_day: int = DAILY_TARGET) -> int:
    """同时在飞的协议请求数。一天 300 万、1.2 秒一次时是 42。"""
    sec = float(page_sec)
    rate = float(per_day) / 86400.0
    if sec <= 0 or rate <= 0:
        return 1
    return max(1, math.ceil(rate * sec - 1e-9))


def normalize_plan(
    lanes: Any = 0,
    inflight: Any = DEFAULT_INFLIGHT,
    page_sec: Any = PROTOCOL_SEC,
) -> Dict[str, Any]:
    lane_n = max(0, min(MAX_LANES, _as_int(lanes, 0)))
    inflight_n = max(1, min(MAX_INFLIGHT, _as_int(inflight, DEFAULT_INFLIGHT)))
    sec = _as_float(page_sec, PROTOCOL_SEC)
    if sec < 0.2:
        sec = 0.2
    if sec > 30:
        sec = 30.0
    return {
        "mode": "protocol",
        "lanes": lane_n,
        "inflight": inflight_n,
        "page_sec": round(sec, 2),
    }


def load_plan(r: Any) -> Dict[str, Any]:
    raw = r.get(PLAN_KEY) if r is not None else None
    if not raw:
        return normalize_plan()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return normalize_plan()
    if not isinstance(data, dict):
        return normalize_plan()
    page_sec = data.get("page_sec")
    if data.get("mode") != "protocol":
        page_sec = PROTOCOL_SEC
    return normalize_plan(data.get("lanes"), data.get("inflight"), page_sec)


def persist_plan(r: Any, plan: Dict[str, Any]) -> Dict[str, Any]:
    cfg = normalize_plan(plan.get("lanes"), plan.get("inflight"), plan.get("page_sec"))
    r.set(PLAN_KEY, json.dumps(cfg, ensure_ascii=False, separators=(",", ":")))
    return cfg


def build_plan(
    lanes: int,
    inflight: int,
    page_sec: float,
    scope: int,
    done: int,
) -> Dict[str, Any]:
    """按浏览器协议把一天 300 万条拆成同时请求数和粘性出口数。"""
    cfg = normalize_plan(lanes, inflight, page_sec)
    target = int(DAILY_TARGET)
    needed = inflight_for_target(cfg["page_sec"], target)
    lanes_needed = max(1, math.ceil(needed / cfg["inflight"]))
    scope_n = max(0, int(scope))
    done_n = max(0, int(done))
    scope_left = max(0, scope_n - done_n)
    universe_left = max(0, SITE_UNIVERSE - done_n)
    return {
        "mode": "protocol",
        "lanes": cfg["lanes"],
        "inflight": cfg["inflight"],
        "page_sec": cfg["page_sec"],
        "target_per_day": target,
        "inflight_needed": needed,
        "lanes_needed": lanes_needed,
        "shortfall": max(0, lanes_needed - cfg["lanes"]),
        "active_requests": cfg["lanes"] * cfg["inflight"],
        "per_lane_per_day": daily_pages(cfg["inflight"], cfg["page_sec"]),
        "per_day": target,
        "scope": scope_n,
        "done": done_n,
        "scope_left": scope_left,
        "universe": SITE_UNIVERSE,
        "universe_left": universe_left,
        "days_scope": days_for(scope_left, target),
        "days_universe": days_for(universe_left, target),
        "phases": _phases(lanes_needed, cfg["inflight"], cfg["page_sec"]),
    }


def _phases(lanes_needed: int, inflight: int, page_sec: float) -> List[Dict[str, str]]:
    return [
        {
            "id": "slice",
            "title": "定切片",
            "detail": "字母、州、年龄决定这一轮要哪一块。不选就是当前字母下的全部目录。",
        },
        {
            "id": "lane",
            "title": "分路",
            "detail": f"一天 300 万条要 {lanes_needed} 个粘性出口。一路一个浏览器，同时 {inflight} 个协议请求。姓氏固定分到路。",
        },
        {
            "id": "directory",
            "title": "扫目录",
            "detail": "每路只打开自己的姓氏页，把人物链接送进该路。目录上只有姓名和城市。",
        },
        {
            "id": "person",
            "title": "协议取全档",
            "detail": f"浏览器过一次验证后，人物文档走协议，按 {page_sec:g} 秒一次计。写入姓名、年龄、城市、街道、电话、邮箱、曾用地址。入库成功才算完成。",
        },
    ]
