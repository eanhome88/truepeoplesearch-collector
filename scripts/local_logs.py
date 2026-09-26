"""Bounded local-log utilities with no service or database dependencies."""

from __future__ import annotations

import codecs
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from typing import BinaryIO, List, Tuple

TAIL_READ_BYTES = 48 * 1024
LOG_COMPACT_THRESHOLD = 10 * 1024 * 1024
LOG_RETAIN_BYTES = 2 * 1024 * 1024


def _read_tail(stream: BinaryIO, limit: int) -> Tuple[bytes, bool]:
    """Read a bounded snapshot, retrying once if a concurrent truncate is seen."""
    for attempt in range(2):
        size = stream.seek(0, os.SEEK_END)
        offset = max(0, size - limit)
        stream.seek(offset)
        data = stream.read(min(size, limit))
        if attempt == 0 and len(data) < min(size, limit):
            if stream.seek(0, os.SEEK_END) < size:
                continue
        return data, offset > 0
    return b"", False


def _without_partial_utf8_prefix(data: bytes) -> bytes:
    # A valid UTF-8 codepoint has at most three continuation bytes.
    offset = 0
    while offset < min(3, len(data)) and 0x80 <= data[offset] <= 0xBF:
        offset += 1
    return data[offset:]


def tail_lines(path: Path, n: int = 24) -> List[str]:
    """Return recent nonblank lines using at most 48 KiB per read.

    A concurrent truncation may require one more bounded read. The first line
    may be partial when a line crosses the byte window. UTF-8 characters split
    by that window or by an unfinished final write are omitted, not replaced
    with artificial decoding errors; invalid bytes inside the log are replaced.
    """
    if n <= 0:
        return []
    try:
        with Path(path).open("rb") as stream:
            data, starts_mid_file = _read_tail(stream, TAIL_READ_BYTES)
    except OSError:
        return []
    if starts_mid_file:
        data = _without_partial_utf8_prefix(data)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = decoder.decode(data, final=False)
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return lines[-n:]


def _no_open_handles(path: Path) -> bool:
    """Fail closed if the OS cannot establish that the file is unused."""
    executable = shutil.which("lsof")
    if not executable:
        return False
    try:
        result = subprocess.run(
            [executable, "-F", "p", "--", str(path.absolute())],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # lsof returns 1 for no matching open files; warnings make this inconclusive.
    return result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip()


def _file_version(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode)


def compact_inactive_log(
    path: Path,
    maximum_bytes: int = LOG_COMPACT_THRESHOLD,
    retained_bytes: int = LOG_RETAIN_BYTES,
) -> bool:
    """Best-effort startup maintenance, never a running-process size limit.

    Above the threshold, retain the latest bytes and discard older log history
    with an atomic replacement in the same directory, preserving permissions.
    Skip symlinks, open files, files changing during the operation, and systems
    without a conclusive lsof result. There is still an unavoidable race with
    an uncooperative writer opening the file immediately before replacement;
    callers must use this only before launching their log writer.
    """
    if not 0 < retained_bytes < maximum_bytes:
        raise ValueError("retained_bytes must be positive and below maximum_bytes")
    path = Path(path)
    temporary_path = None
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size <= maximum_bytes:
            return False
        if not _no_open_handles(path):
            return False
        with path.open("rb") as source:
            if _file_version(os.fstat(source.fileno())) != _file_version(before):
                return False
            recent, _ = _read_tail(source, retained_bytes)
            if _file_version(os.fstat(source.fileno())) != _file_version(before):
                return False
        recent = _without_partial_utf8_prefix(recent)
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.chmod(temporary_path, stat.S_IMODE(before.st_mode))
            temporary.write(recent)
            temporary.flush()
            os.fsync(temporary.fileno())
        if not _no_open_handles(path) or _file_version(path.lstat()) != _file_version(before):
            return False
        os.replace(temporary_path, path)
        temporary_path = None
        return True
    except OSError:
        # Optional maintenance must not prevent a normal process launch.
        return False
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
