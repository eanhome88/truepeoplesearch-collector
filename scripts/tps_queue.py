#!/usr/bin/env python3
"""
Redis 可靠队列（BLMOVE + 租约 + 死信）。

供 distributed_worker.py 与测试直接 import。不连接 TiDB，不含抓取逻辑。
r 为 redis.Redis；decode_responses 建议 True，但对 bytes/str 都容错。

旧键 tps_urls 不在本模块消费路径里读取；仅 drain_legacy(r) 负责迁入新队列。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union
from urllib.parse import urlparse, urlunparse

LEASE_SEC = 90
MAX_ATTEMPTS = 3
CLAIM_TIMEOUT_SEC = 2
RECOVER_LOCK_SEC = 30
# ack 后保留 job JSON 的短 TTL（6 小时）
ACK_JOB_TTL_SEC = 6 * 3600
_PIPELINE_CHUNK = 256

PENDING_KEY = "tps:pending"
PROCESSING_KEY = "tps:processing"
LEASES_KEY = "tps:leases"
JOB_KEY_PREFIX = "tps:job:"
DLQ_KEY = "tps:dlq"
SEEN_KEY = "tps:seen"
QUEUED_KEY = "tps:queued"
SKIPPED_KEY = "tps:skipped"
FAILED_KEY = "tps:failed"
RECOVER_LOCK_KEY = "tps:recover_lock"
LEGACY_URLS_KEY = "tps_urls"

_PERSON_RE = re.compile(r"/person/(\w+)")

UrlLike = Union[str, bytes]


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value)


def _truthy(value: Any) -> bool:
    if value is True or value == 1:
        return True
    if value is False or value is None or value == 0:
        return False
    if isinstance(value, bytes):
        return value not in (b"", b"0", b"false", b"False", b"none", b"None")
    if isinstance(value, str):
        return value not in ("", "0", "false", "False", "none", "None")
    return bool(value)


def _as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(float(_as_str(value)))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(_as_str(value))
    except (TypeError, ValueError):
        return default


def _job_key(job_id: str) -> str:
    return f"{JOB_KEY_PREFIX}{job_id}"


def _job_id_of(job: Any) -> str:
    if isinstance(job, dict):
        return _as_str(job.get("id") or job.get(b"id"))
    return _as_str(job)


def _dumps(job: dict) -> str:
    return json.dumps(job, ensure_ascii=False, separators=(",", ":"))


def _parse_job(raw: Any) -> Optional[dict]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    text = _as_str(raw).strip()
    if not text:
        return None
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("job payload is not an object")
    if "id" in data:
        data["id"] = _as_str(data["id"])
    if "person_id" in data:
        data["person_id"] = _as_str(data["person_id"])
    if "url" in data:
        data["url"] = _as_str(data["url"])
    return data


def _iter_urls(urls: Any) -> Iterable[Any]:
    if urls is None:
        return ()
    if isinstance(urls, (str, bytes)):
        return (urls,)
    return urls


def extract_person_id(url: str) -> str:
    """从 URL 提取 TruePeopleSearch person_id：/person/(\\w+)。"""
    match = _PERSON_RE.search(_as_str(url))
    return match.group(1) if match else ""


def normalize_url(url: str) -> str:
    """去掉空白、fragment、query，并规范化 scheme/host 大小写。"""
    text = _as_str(url).strip()
    if not text:
        return ""
    parsed = urlparse(text)
    path = parsed.path.rstrip("/")
    if parsed.scheme and parsed.netloc:
        return urlunparse(
            (parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", "")
        )
    return path or text.split("#", 1)[0].split("?", 1)[0].rstrip("/")


def new_job(url: str) -> dict:
    raw = _as_str(url).strip()
    norm = normalize_url(raw)
    person_id = extract_person_id(norm) or extract_person_id(raw)
    return {
        "id": uuid.uuid4().hex,
        "person_id": person_id,
        "url": norm or raw,
        "attempts": 0,
        "enqueued_at": time.time(),
        "lease_until": 0,
        "last_error": None,
    }


def _sismember_many(r: Any, key: str, person_ids: Sequence[str]) -> List[bool]:
    flags: List[bool] = []
    for i in range(0, len(person_ids), _PIPELINE_CHUNK):
        chunk = person_ids[i : i + _PIPELINE_CHUNK]
        pipe = r.pipeline(transaction=False)
        for pid in chunk:
            pipe.sismember(key, pid)
        flags.extend(_truthy(x) for x in pipe.execute())
    return flags


def _sadd_many(r: Any, key: str, person_ids: Sequence[str]) -> List[bool]:
    """SADD 各 id，返回是否新加入。"""
    flags: List[bool] = []
    for i in range(0, len(person_ids), _PIPELINE_CHUNK):
        chunk = person_ids[i : i + _PIPELINE_CHUNK]
        pipe = r.pipeline(transaction=False)
        for pid in chunk:
            pipe.sadd(key, pid)
        flags.extend(int(x or 0) == 1 for x in pipe.execute())
    return flags


def _person_id_of(job: Any) -> str:
    if not isinstance(job, dict):
        return ""
    pid = _as_str(job.get("person_id") or job.get(b"person_id"))
    if pid:
        return pid
    return extract_person_id(_as_str(job.get("url") or job.get(b"url")))


def feed(r: Any, urls: Any, seen_check: bool = True) -> dict:
    """灌入 pending。去重看 seen（已入库）和 queued（在飞）。成功 ack 才写 seen。"""
    enqueued = 0
    deduped = 0
    invalid = 0
    candidates: List[tuple] = []

    for raw in _iter_urls(urls):
        text = _as_str(raw).strip()
        if not text:
            invalid += 1
            continue
        norm = normalize_url(text)
        person_id = extract_person_id(norm) or extract_person_id(text)
        if not person_id:
            invalid += 1
            continue
        candidates.append((norm or text, person_id))

    if not candidates:
        return {"enqueued": enqueued, "deduped": deduped, "invalid": invalid}

    pids = [pid for _, pid in candidates]
    seen_flags = (
        _sismember_many(r, SEEN_KEY, pids)
        if seen_check
        else [False] * len(candidates)
    )

    maybe: List[tuple] = []
    batch_seen = set()
    for (url, person_id), already in zip(candidates, seen_flags):
        if seen_check and (already or person_id in batch_seen):
            deduped += 1
            continue
        if seen_check:
            batch_seen.add(person_id)
        maybe.append((url, person_id))

    if seen_check and maybe:
        queued_new = _sadd_many(r, QUEUED_KEY, [pid for _, pid in maybe])
        to_enqueue = []
        for (url, person_id), added in zip(maybe, queued_new):
            if added:
                to_enqueue.append((url, person_id))
            else:
                deduped += 1
    else:
        to_enqueue = maybe

    now = time.time()
    for i in range(0, len(to_enqueue), _PIPELINE_CHUNK):
        chunk = to_enqueue[i : i + _PIPELINE_CHUNK]
        pipe = r.pipeline(transaction=True)
        for url, person_id in chunk:
            job = new_job(url)
            job["person_id"] = person_id
            job["url"] = url
            job["enqueued_at"] = now
            job_id = job["id"]
            pipe.set(_job_key(job_id), _dumps(job))
            pipe.lpush(PENDING_KEY, job_id)
            enqueued += 1
        try:
            pipe.execute()
        except Exception:
            for _, person_id in chunk:
                r.srem(QUEUED_KEY, person_id)
            raise

    return {"enqueued": enqueued, "deduped": deduped, "invalid": invalid}


def _move_pending_to_processing(r: Any, timeout: float = CLAIM_TIMEOUT_SEC):
    try:
        return r.blmove(
            PENDING_KEY, PROCESSING_KEY, timeout, src="RIGHT", dest="LEFT"
        )
    except TypeError:
        return r.blmove(PENDING_KEY, PROCESSING_KEY, timeout, "RIGHT", "LEFT")
    except Exception as exc:
        msg = _as_str(exc).lower()
        if "unknown command" in msg or "blmove" in msg:
            return r.brpoplpush(PENDING_KEY, PROCESSING_KEY, timeout)
        raise


def _orphan_claim(r: Any, job_id: str) -> None:
    pipe = r.pipeline(transaction=True)
    pipe.lrem(PROCESSING_KEY, 1, job_id)
    pipe.zrem(LEASES_KEY, job_id)
    pipe.execute()


def claim(r: Any, worker_id: str, lease_sec: int = LEASE_SEC) -> Optional[dict]:
    """BLMOVE pending→processing（RIGHTLEFT，timeout 2s），写入租约后返回 job。"""
    raw_id = _move_pending_to_processing(r, CLAIM_TIMEOUT_SEC)
    if not raw_id:
        return None
    job_id = _as_str(raw_id)
    if not job_id:
        return None

    raw = r.get(_job_key(job_id))
    try:
        job = _parse_job(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        _orphan_claim(r, job_id)
        return None
    if job is None:
        _orphan_claim(r, job_id)
        return None

    now = time.time()
    lease_until = now + float(lease_sec)
    job["id"] = job_id
    job["claimed_at"] = now
    job["lease_until"] = lease_until
    wid = _as_str(worker_id)
    if wid:
        job["worker_id"] = wid

    pipe = r.pipeline(transaction=True)
    pipe.zadd(LEASES_KEY, {job_id: lease_until})
    pipe.set(_job_key(job_id), _dumps(job))
    pipe.execute()
    return job


def heartbeat(r: Any, job: dict, lease_sec: int = LEASE_SEC) -> bool:
    """ZADD XX 续租；租约已不存在则返回 False。"""
    if not isinstance(job, dict):
        return False
    job_id = _job_id_of(job)
    if not job_id:
        return False

    lease_until = time.time() + float(lease_sec)
    try:
        changed = r.zadd(LEASES_KEY, {job_id: lease_until}, xx=True, ch=True)
    except TypeError:
        score = r.zscore(LEASES_KEY, job_id)
        if score is None:
            return False
        r.zadd(LEASES_KEY, {job_id: lease_until})
        changed = 1
    if not _truthy(changed):
        return False

    job["lease_until"] = lease_until
    r.set(_job_key(job_id), _dumps(job))
    return True


def ack(r: Any, job: dict) -> None:
    """从 processing/leases 摘掉；成功后才写入 tps:seen，并清掉 queued。"""
    job_id = _job_id_of(job)
    if not job_id:
        return
    person_id = _person_id_of(job)
    pipe = r.pipeline(transaction=True)
    pipe.lrem(PROCESSING_KEY, 1, job_id)
    pipe.zrem(LEASES_KEY, job_id)
    if person_id:
        pipe.sadd(SEEN_KEY, person_id)
        pipe.srem(QUEUED_KEY, person_id)
        pipe.srem(FAILED_KEY, person_id)
    pipe.expire(_job_key(job_id), ACK_JOB_TTL_SEC)
    pipe.execute()


def release(r: Any, job: dict, error: str) -> None:
    """放回 pending，不增加 attempts，不进死信。出口 429 时用，避免把人页打进 DLQ。"""
    if not isinstance(job, dict):
        return
    job_id = _job_id_of(job)
    if not job_id:
        return

    job["last_error"] = _as_str(error)
    job["lease_until"] = 0

    pipe = r.pipeline(transaction=True)
    pipe.set(_job_key(job_id), _dumps(job))
    pipe.zrem(LEASES_KEY, job_id)
    pipe.lrem(PROCESSING_KEY, 1, job_id)
    pipe.lpush(PENDING_KEY, job_id)
    pipe.execute()


def nack(r: Any, job: dict, error: str, retry: bool = True) -> None:
    """attempts+1 并丢租约；可重试则回 pending，否则进 tps:dlq。"""
    if not isinstance(job, dict):
        return
    job_id = _job_id_of(job)
    if not job_id:
        return

    attempts = _as_int(job.get("attempts"), 0) + 1
    job["attempts"] = attempts
    job["last_error"] = _as_str(error)
    job["lease_until"] = 0

    person_id = _person_id_of(job)
    pipe = r.pipeline(transaction=True)
    pipe.set(_job_key(job_id), _dumps(job))
    pipe.zrem(LEASES_KEY, job_id)
    pipe.lrem(PROCESSING_KEY, 1, job_id)
    if retry and attempts < MAX_ATTEMPTS:
        pipe.lpush(PENDING_KEY, job_id)
    else:
        pipe.lpush(DLQ_KEY, job_id)
        if person_id:
            pipe.srem(QUEUED_KEY, person_id)
            pipe.sadd(FAILED_KEY, person_id)
    pipe.execute()


def _release_recover_lock(r: Any, token: str) -> None:
    current = r.get(RECOVER_LOCK_KEY)
    if _as_str(current) == token:
        r.delete(RECOVER_LOCK_KEY)


def _is_rate_limit_hold(error: Any) -> bool:
    """last_error 含 429 或 captcha（大小写不敏感）时，过期租约应 release 而非 nack。"""
    text = _as_str(error).lower()
    return "429" in text or "captcha" in text


def recover_expired(r: Any) -> int:
    """SET tps:recover_lock NX EX 30。429/captcha 过期租约 release（不增加 attempts）；其余 nack(retry=True)。"""
    token = uuid.uuid4().hex
    got = r.set(RECOVER_LOCK_KEY, token, nx=True, ex=RECOVER_LOCK_SEC)
    if not _truthy(got):
        return 0

    recovered = 0
    try:
        now = time.time()
        expired = r.zrangebyscore(LEASES_KEY, 0, now) or []
        for raw_id in expired:
            job_id = _as_str(raw_id)
            if not job_id:
                continue
            raw = r.get(_job_key(job_id))
            try:
                job = _parse_job(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                job = None
            if job is None:
                job = {
                    "id": job_id,
                    "person_id": "",
                    "url": "",
                    "attempts": 0,
                    "enqueued_at": now,
                    "lease_until": 0,
                    "last_error": None,
                }
            error = _as_str(job.get("last_error"))
            if _is_rate_limit_hold(error):
                release(r, job, error)
            else:
                nack(r, job, "lease_expired", retry=True)
            recovered += 1
        return recovered
    finally:
        _release_recover_lock(r, token)


def _peek_oldest_pending_id(r: Any) -> Any:
    """LPUSH + BLMOVE RIGHT：最老任务在列表右端。无 lindex 时退回 lrange。"""
    lindex = getattr(r, "lindex", None)
    if callable(lindex):
        try:
            return lindex(PENDING_KEY, -1)
        except Exception:
            pass
    lrange = getattr(r, "lrange", None)
    if callable(lrange):
        try:
            items = lrange(PENDING_KEY, -1, -1) or []
            if items:
                return items[0]
        except Exception:
            pass
    return None


def queue_stats(r: Any) -> dict:
    now = time.time()
    pipe = r.pipeline(transaction=False)
    pipe.llen(PENDING_KEY)
    pipe.llen(PROCESSING_KEY)
    pipe.llen(DLQ_KEY)
    pipe.scard(SEEN_KEY)
    pipe.scard(QUEUED_KEY)
    pipe.scard(SKIPPED_KEY)
    pipe.scard(FAILED_KEY)
    pipe.zcount(LEASES_KEY, 0, now)
    pending, processing, dlq, seen, queued, skipped, failed, expired = pipe.execute()

    oldest_age: Optional[float] = None
    oid = _as_str(_peek_oldest_pending_id(r))
    if oid:
        try:
            job = _parse_job(r.get(_job_key(oid)))
        except (json.JSONDecodeError, TypeError, ValueError):
            job = None
        if job is not None:
            enqueued_at = job.get("enqueued_at")
            if enqueued_at not in (None, ""):
                oldest_age = max(0.0, now - _as_float(enqueued_at))

    return {
        "pending": _as_int(pending),
        "processing": _as_int(processing),
        "dlq": _as_int(dlq),
        "seen": _as_int(seen),
        "queued": _as_int(queued),
        "skipped": _as_int(skipped),
        "failed": _as_int(failed),
        "expired_leases": _as_int(expired),
        "oldest_pending_age_sec": oldest_age,
    }


def peek_jobs(r: Any, key: str, limit: int = 20) -> List[dict]:
    """读取队列左侧若干 job JSON，缺 payload 时仍返回 id。"""
    cap = max(0, min(_as_int(limit, 20), 50))
    if cap == 0:
        return []
    lrange = getattr(r, "lrange", None)
    if not callable(lrange):
        return []
    raw_ids = lrange(key, 0, cap - 1) or []
    out: List[dict] = []
    for raw_id in raw_ids:
        job_id = _as_str(raw_id)
        if not job_id:
            continue
        item = {
            "id": job_id,
            "person_id": "",
            "url": "",
            "attempts": 0,
            "worker_id": "",
            "enqueued_at": None,
            "claimed_at": None,
            "lease_until": None,
            "last_error": None,
        }
        try:
            job = _parse_job(r.get(_job_key(job_id)))
        except (json.JSONDecodeError, TypeError, ValueError):
            job = None
        if job:
            item.update({
                "id": _as_str(job.get("id") or job_id),
                "person_id": _as_str(job.get("person_id")),
                "url": _as_str(job.get("url")),
                "attempts": _as_int(job.get("attempts")),
                "worker_id": _as_str(job.get("worker_id")),
                "enqueued_at": job.get("enqueued_at"),
                "claimed_at": job.get("claimed_at"),
                "lease_until": job.get("lease_until"),
                "last_error": job.get("last_error"),
            })
        out.append(item)
    return out


def peek_processing(r: Any, limit: int = 20) -> List[dict]:
    return peek_jobs(r, PROCESSING_KEY, limit)


def peek_dlq(r: Any, limit: int = 10) -> List[dict]:
    return peek_jobs(r, DLQ_KEY, limit)


def drain_legacy(r: Any) -> dict:
    """把旧列表 tps_urls 里的原始 URL 原子迁出并 feed 进新队列。"""
    empty = {"enqueued": 0, "deduped": 0, "invalid": 0, "drained": 0}
    if not _truthy(r.exists(LEGACY_URLS_KEY)):
        return empty

    tmp_key = f"{LEGACY_URLS_KEY}:drain:{uuid.uuid4().hex}"
    try:
        r.rename(LEGACY_URLS_KEY, tmp_key)
    except Exception:
        return empty

    raw_urls = r.lrange(tmp_key, 0, -1) or []
    r.delete(tmp_key)
    result = feed(r, raw_urls, seen_check=True)
    result["drained"] = len(raw_urls)
    return result


__all__ = [
    "LEASE_SEC",
    "MAX_ATTEMPTS",
    "CLAIM_TIMEOUT_SEC",
    "PENDING_KEY",
    "PROCESSING_KEY",
    "LEASES_KEY",
    "JOB_KEY_PREFIX",
    "DLQ_KEY",
    "SEEN_KEY",
    "RECOVER_LOCK_KEY",
    "LEGACY_URLS_KEY",
    "extract_person_id",
    "normalize_url",
    "new_job",
    "feed",
    "claim",
    "heartbeat",
    "ack",
    "release",
    "nack",
    "recover_expired",
    "queue_stats",
    "drain_legacy",
]
