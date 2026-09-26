#!/usr/bin/env python3
"""Requeue dropped person pages from the latest worker run.

Includes [empty] person=<id> and [rate_limit] person=<id> lines that do
not say "returned to pending". Only lines after the last "--- start "
marker count. Skip ids already in persons. If tps:seen blocks feed,
delete only those ids from the seen set and feed again.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import mysql.connector
import redis

from tps_queue import SEEN_KEY, feed

LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "logs" / "worker.log"
START_MARKER = "--- start "
EMPTY_RE = re.compile(r"\[empty\] person=(\w+)")
RATE_RE = re.compile(r"\[rate_limit\] person=(\w+)")
PERSON_URL = "https://www.truepeoplesearch.com/find/person/{}"
CHUNK = 256

TIDB = {
    "host": "127.0.0.1",
    "port": 4000,
    "user": "root",
    "password": "",
    "database": "people_search",
}


def current_run_text(text: str) -> str:
    """Text after the last start marker. Empty when the log has none."""
    lines = text.splitlines()
    start_at = None
    for i, line in enumerate(lines):
        if START_MARKER in line:
            start_at = i
    if start_at is None:
        return ""
    return "\n".join(lines[start_at + 1 :])


def parse_dropped_ids(text: str) -> list[str]:
    """Unique dropped person ids in the current run, in log order."""
    found: list[str] = []
    seen: set[str] = set()
    for line in current_run_text(text).splitlines():
        if "[empty]" in line:
            match = EMPTY_RE.search(line)
        elif "[rate_limit]" in line and "returned to pending" not in line:
            match = RATE_RE.search(line)
        else:
            continue
        if not match:
            continue
        person_id = match.group(1)
        if person_id in seen:
            continue
        seen.add(person_id)
        found.append(person_id)
    return found


def stored_person_ids(conn, person_ids: list[str]) -> set[str]:
    stored: set[str] = set()
    if not person_ids:
        return stored
    cur = conn.cursor()
    try:
        for i in range(0, len(person_ids), CHUNK):
            chunk = person_ids[i : i + CHUNK]
            placeholders = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"SELECT person_id FROM persons WHERE person_id IN ({placeholders})",
                tuple(chunk),
            )
            for row in cur.fetchall():
                if row and row[0]:
                    stored.add(str(row[0]))
    finally:
        cur.close()
    return stored


def _chunks(person_ids: list[str]):
    for i in range(0, len(person_ids), CHUNK):
        yield person_ids[i : i + CHUNK]


def clear_seen(r, person_ids: list[str]) -> None:
    for chunk in _chunks(person_ids):
        r.srem(SEEN_KEY, *chunk)


def members_of_seen(r, person_ids: list[str]) -> list[str]:
    blocked: list[str] = []
    for chunk in _chunks(person_ids):
        pipe = r.pipeline(transaction=False)
        for person_id in chunk:
            pipe.sismember(SEEN_KEY, person_id)
        flags = pipe.execute()
        for person_id, flag in zip(chunk, flags):
            if flag:
                blocked.append(person_id)
    return blocked


def enqueue(r, person_ids: list[str]) -> int:
    if not person_ids:
        return 0
    urls = [PERSON_URL.format(person_id) for person_id in person_ids]
    stats = feed(r, urls, seen_check=True)
    enqueued = int(stats.get("enqueued") or 0)
    # feed skips tps:seen. Drop only the ids that were still marked seen.
    if int(stats.get("deduped") or 0):
        blocked = members_of_seen(r, person_ids)
        if blocked:
            clear_seen(r, blocked)
            again = feed(
                r,
                [PERSON_URL.format(person_id) for person_id in blocked],
                seen_check=True,
            )
            enqueued += int(again.get("enqueued") or 0)
    return enqueued


def main() -> int:
    text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
    ids = parse_dropped_ids(text)
    conn = mysql.connector.connect(**TIDB)
    try:
        stored = stored_person_ids(conn, ids)
    finally:
        conn.close()
    pending = [person_id for person_id in ids if person_id not in stored]
    r = redis.Redis(
        host="127.0.0.1",
        port=6379,
        decode_responses=True,
        socket_timeout=10,
        socket_connect_timeout=5,
    )
    try:
        enqueued = enqueue(r, pending)
    finally:
        r.close()
    print(
        f"found={len(ids)} skipped={len(stored)} enqueued={enqueued}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
