"""Throughput plan for the person scraper.

One cold browser per URL costs about 30s and a few hundred MB, so a thread
per in-flight page cannot reach millions per day. A warm browser that keeps
its Cloudflare cookie and reuses one tab is planned at WARM_PAGE_SEC.
"""

from __future__ import annotations

import math
import os

DAILY_TARGET = 3_000_000
WARM_PAGE_SEC = 8.0
COLD_PAGE_SEC = 30.0
MAX_BROWSER_CONCURRENCY = 128
PER_BROWSER_GB = 0.6
RESERVE_GB = 4.0
BROWSERS_PER_CORE = 3
TABS_PER_CHROME = 4


def chrome_process_count(pages: int, tabs_per_chrome: int = TABS_PER_CHROME) -> int:
    """How many Chrome processes serve this many concurrent pages."""
    tabs = max(1, int(tabs_per_chrome))
    return max(1, math.ceil(max(1, int(pages)) / tabs))


def pages_per_sec(per_day: float) -> float:
    return float(per_day) / 86400.0


def browsers_for(per_day: float = DAILY_TARGET, page_sec: float = WARM_PAGE_SEC) -> int:
    if page_sec <= 0:
        raise ValueError("page_sec must be > 0")
    need = pages_per_sec(per_day) * float(page_sec)
    return max(1, math.ceil(need - 1e-9))


def daily_capacity(browsers: int, page_sec: float = WARM_PAGE_SEC) -> int:
    if browsers <= 0 or page_sec <= 0:
        return 0
    return int(int(browsers) / float(page_sec) * 86400)


def physical_mem_gb() -> float:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
        return float(pages) * float(size) / (1024 ** 3)
    except (AttributeError, OSError, TypeError, ValueError):
        return 8.0


def host_browser_budget(
    mem_gb: float | None = None,
    reserve_gb: float = RESERVE_GB,
    per_browser_gb: float = PER_BROWSER_GB,
    cpu_count: int | None = None,
    browsers_per_core: int = BROWSERS_PER_CORE,
) -> int:
    mem = physical_mem_gb() if mem_gb is None else float(mem_gb)
    usable = max(float(per_browser_gb), mem - float(reserve_gb))
    by_mem = int(usable // float(per_browser_gb))
    cores = os.cpu_count() if cpu_count is None else int(cpu_count)
    cores = max(1, int(cores or 1))
    by_cpu = max(1, cores * max(1, int(browsers_per_core)))
    return max(1, min(MAX_BROWSER_CONCURRENCY, by_mem, by_cpu))


def max_page_sec(browsers: int, per_day: float = DAILY_TARGET) -> float:
    """Longest warm page time that still hits per_day with this many browsers."""
    rate = pages_per_sec(per_day)
    if browsers <= 0 or rate <= 0:
        return 0.0
    return float(browsers) / rate


def resolve_browsers(
    concurrency: int | None,
    per_day: float = DAILY_TARGET,
    page_sec: float = WARM_PAGE_SEC,
    mem_gb: float | None = None,
    cpu_count: int | None = None,
) -> dict:
    """Pick how many long-lived browsers this process should run.

    Omitting concurrency fills the host memory and CPU budget, never the
    cold-start thread count. An explicit value is clamped to MAX_BROWSER_CONCURRENCY.
    """
    need = browsers_for(per_day, page_sec)
    budget = host_browser_budget(mem_gb, cpu_count=cpu_count)
    clamped = False
    if concurrency is None:
        chosen = min(need, budget)
    else:
        raw = int(concurrency)
        chosen = max(1, min(raw, MAX_BROWSER_CONCURRENCY))
        clamped = raw != chosen
    hosts = math.ceil(need / chosen) if chosen else need
    return {
        "browsers": chosen,
        "need": need,
        "budget": budget,
        "per_day_target": int(per_day),
        "page_sec": float(page_sec),
        "capacity": daily_capacity(chosen, page_sec),
        "hosts": int(hosts),
        "clamped": clamped,
        "cold_browsers": browsers_for(per_day, COLD_PAGE_SEC),
    }
