"""美国本地号码计划：只排出可分配的号码，并记住已经入库的关联号码。"""

from __future__ import annotations

import os
import re
from typing import Callable, Iterable, Optional, Sequence

KNOWN_KEY = "tps:phone:known"
PREFIX_KEY = "tps:phone:prefix:"
PREFIX_MISS_LIMIT = 8

# 非地理号、免费号、高价号、加拿大和加勒比海非美国属地。这些号段查不到美国居民。
_EXCLUDED_NPA = frozenset({
    211, 311, 411, 511, 611, 711, 811, 911,
    456, 500, 521, 522, 523, 524, 525, 526, 527, 528, 529,
    532, 533, 544, 555, 566, 577, 588, 600, 700, 710,
    800, 833, 844, 855, 866, 877, 888, 900, 950, 976,
    204, 226, 236, 249, 250, 257, 263, 289, 306, 343, 354, 365, 367, 368, 382,
    403, 416, 418, 428, 431, 437, 438, 450, 468, 474, 506, 514, 519, 548, 579,
    581, 584, 587, 604, 613, 639, 647, 672, 683, 705, 709, 742, 753, 778, 780,
    782, 807, 819, 825, 867, 873, 879, 902, 905,
    242, 246, 264, 268, 284, 345, 441, 473, 649, 658, 664, 721, 758, 767, 784,
    809, 829, 849, 868, 869, 876,
})

_PHONE_IN_URL = re.compile(r"(?:phoneno=|/find/phone/)(\d{10})(?:\D|$)")
_DIGITS = re.compile(r"\D+")


def valid_npa(npa: int) -> bool:
    if npa < 200 or npa > 999:
        return False
    if npa % 100 == 11:
        return False
    return npa not in _EXCLUDED_NPA


def valid_nxx(nxx: int) -> bool:
    if nxx < 200 or nxx > 999:
        return False
    if nxx % 100 == 11:
        return False
    return nxx != 555


def normalize_digits(raw) -> str:
    digits = _DIGITS.sub("", str(raw or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "23456789" and digits[3] in "23456789":
        return digits
    return ""


def associated_digits(phones: Optional[Iterable]) -> list:
    """人物页上的全部号码，去重后保留 10 位数字。"""
    found = []
    seen = set()
    for item in phones or []:
        raw = item.get("phone_number") if isinstance(item, dict) else item
        digits = normalize_digits(raw)
        if not digits or digits in seen:
            continue
        seen.add(digits)
        found.append(digits)
    return found


def phone_url(digits: str) -> str:
    return f"https://www.truepeoplesearch.com/find/phone/{digits}"


def phone_digits_from_url(url: str) -> str:
    match = _PHONE_IN_URL.search(str(url or ""))
    if not match:
        return ""
    return normalize_digits(match.group(1))


def _next_npa(npa: int) -> Optional[int]:
    current = npa + 1
    while current <= 999:
        if valid_npa(current):
            return current
        current += 1
    return None


def _first_nxx(start: int = 200) -> int:
    current = max(200, start)
    while current <= 999 and not valid_nxx(current):
        current += 1
    return current


def next_prefix(npa: int, nxx: int):
    """跳到下一个有效局号，用尽则返回 None。"""
    current_nxx = nxx + 1
    while current_nxx <= 999 and not valid_nxx(current_nxx):
        current_nxx += 1
    if current_nxx <= 999:
        return npa, current_nxx, 1
    nxt = _next_npa(npa)
    if nxt is None:
        return None
    return nxt, _first_nxx(), 1


def normalize_cursor(npa: int, nxx: int, line: int):
    if not valid_npa(npa):
        nxt = _next_npa(npa - 1) if npa > 0 else _next_npa(199)
        if nxt is None:
            return None
        npa = nxt
        nxx = 200
        line = 1
    if not valid_nxx(nxx):
        jumped = next_prefix(npa, nxx - 1 if nxx > 0 else 199)
        if jumped is None:
            return None
        npa, nxx, line = jumped
    if line < 1:
        line = 1
    if line > 9999:
        return next_prefix(npa, nxx)
    return npa, nxx, line


def step(npa: int, nxx: int, line: int):
    if line < 9999:
        return npa, nxx, line + 1
    return next_prefix(npa, nxx)


def select_batch(
    npa: int,
    nxx: int,
    line: int,
    count: int,
    blocked: Callable[[Sequence[str]], Sequence[bool]],
    cold: Callable[[int, int], bool],
    max_scan: int = 8000,
):
    """从光标往后挑出 count 个还没查过的号码。返回 (号码, 新光标或 None)。"""
    cursor = normalize_cursor(npa, nxx, line)
    if cursor is None or count <= 0:
        return [], cursor

    scan_npa, scan_nxx, scan_line = cursor
    committed = cursor
    picked = []
    pending = []
    scanned = 0

    def take(items) -> bool:
        nonlocal committed, picked
        if not items:
            return False
        flags = list(blocked([number for number, _state in items]))
        if len(flags) < len(items):
            flags.extend([False] * (len(items) - len(flags)))
        for (number, state), skip in zip(items, flags):
            if skip:
                committed = state
                continue
            picked.append(number)
            committed = state
            if len(picked) >= count:
                return True
        return False

    while len(picked) < count and scanned < max_scan:
        if cold(scan_npa, scan_nxx):
            if take(pending):
                break
            pending = []
            jumped = next_prefix(scan_npa, scan_nxx)
            if jumped is None:
                return picked, None
            scan_npa, scan_nxx, scan_line = jumped
            committed = jumped
            continue
        digits = f"{scan_npa:03d}{scan_nxx:03d}{scan_line:04d}"
        nxt = step(scan_npa, scan_nxx, scan_line)
        pending.append((digits, nxt))
        scanned += 1
        if nxt is None:
            take(pending)
            return picked, None
        scan_npa, scan_nxx, scan_line = nxt
        if len(pending) >= 256:
            if take(pending):
                break
            pending = []
            committed = (scan_npa, scan_nxx, scan_line)
    if pending and len(picked) < count:
        take(pending)
    return picked, committed


def prefix_is_cold(r, npa: int, nxx: int, miss_limit: int = PREFIX_MISS_LIMIT) -> bool:
    raw = r.hgetall(PREFIX_KEY + f"{npa:03d}{nxx:03d}") or {}
    hits = int(raw.get("hits") or raw.get(b"hits") or 0)
    misses = int(raw.get("misses") or raw.get(b"misses") or 0)
    return hits <= 0 and misses >= miss_limit


def blocked_flags(r, digits: Sequence[str], seen_key: str) -> list:
    if not digits:
        return []
    pipe = r.pipeline(transaction=False)
    for number in digits:
        pipe.sismember(KNOWN_KEY, number)
        pipe.sismember(seen_key, number)
    raw = pipe.execute()
    flags = []
    for index in range(0, len(raw), 2):
        flags.append(bool(raw[index]) or bool(raw[index + 1]))
    return flags


def connect_redis():
    import redis

    return redis.Redis(
        host=os.environ.get("TPS_REDIS_HOST") or os.environ.get("REDIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("TPS_REDIS_PORT") or os.environ.get("REDIS_PORT", "6379")),
        password=os.environ.get("TPS_REDIS_PASSWORD") or os.environ.get("REDIS_PASSWORD") or None,
        decode_responses=True,
    )


def remember_associated_phones(phones: Optional[Iterable], r=None) -> int:
    """把人物页上的号码记入已知集合，生成器不再重复查询。"""
    digits = associated_digits(phones)
    if not digits:
        return 0
    own = r is None
    try:
        if own:
            r = connect_redis()
        pipe = r.pipeline(transaction=False)
        pipe.sadd(KNOWN_KEY, *digits)
        seen_prefix = set()
        for number in digits:
            prefix = number[:6]
            if prefix in seen_prefix:
                continue
            seen_prefix.add(prefix)
            pipe.hincrby(PREFIX_KEY + prefix, "hits", 1)
        pipe.execute()
    except Exception:
        return 0
    return len(digits)


def note_phone_lookup(r, url: str, hit: bool) -> None:
    digits = phone_digits_from_url(url)
    if not digits or r is None:
        return
    try:
        r.hincrby(PREFIX_KEY + digits[:6], "hits" if hit else "misses", 1)
    except Exception:
        return
