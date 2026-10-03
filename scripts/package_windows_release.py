#!/usr/bin/env python3
"""Create a deterministic, offline Windows release archive.

This tool is deliberately a *packager*, not an installer, builder, updater, or
deployment command.  It reads only the local checkout and the three supplied
Windows launchers.  In particular it never starts a process from the release,
does not invoke package managers, and does not access a remote Git endpoint.

The release contents are an allowlist rather than a recursive directory copy.
That keeps per-installation data, credentials, proxy values, logs, caches, and
old executable aliases out of customer archives by construction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Optional, Sequence


# Only these three freshly supplied launchers are release artifacts.  Legacy
# aliases such as TruePeopleSearch_GUI.exe are intentionally not included.
REQUIRED_WINDOWS_EXECUTABLES = (
    "TruePeopleSearch.exe",
    "TruePeopleSearch_后台无窗启动.exe",
    "TruePeopleSearch_停止.exe",
)
BINARY_MANIFEST_NAME = "windows-launchers.manifest.json"
BINARY_MANIFEST_SCHEMA_VERSION = 1
IMAGE_FILE_MACHINE_AMD64 = 0x8664
IMAGE_NT_OPTIONAL_HDR64_MAGIC = 0x20B
_DOS_HEADER_SIZE = 0x40
_DOS_E_LFANEW_OFFSET = 0x3C
_PE_SIGNATURE = b"PE\0\0"
_PE_COFF_HEADER_SIZE = 20
_PE_MINIMUM_HEADER_SIZE = len(_PE_SIGNATURE) + _PE_COFF_HEADER_SIZE + 2
_PE32_PLUS_MINIMUM_OPTIONAL_HEADER_SIZE = 0x70
_PE_SECTION_HEADER_SIZE = 40

# This list is intentionally explicit.  Do not replace it with a directory
# walk, glob, or caller-supplied include pattern: a release must be reviewable
# by reading this source file alone.  The files are the supported application
# source, setup templates, and customer-facing instructions; no runtime state
# or customer configuration is part of this list.  In particular, proxy and
# schema *source* can be reviewed here when a supported module imports it, but
# proxy values, database contents, queues, logs, and caches remain excluded.
RUNTIME_SOURCE_ALLOWLIST = (
    "README_Windows.txt",
    "start_client.bat",
    "stop_client.bat",
    "version.json",
    "deploy/.env.example",
    "deploy/docker-compose.yml",
    "deploy/windows/Expand-Release.ps1",
    "deploy/windows/README-RELEASE.md",
    "deploy/windows/requirements-dashboard.txt",
    "deploy/windows/Test-HostReadiness.ps1",
    "deploy/windows/Verify-Release.ps1",
    "scripts/local_logs.py",
    "scripts/person_visibility.py",
    "scripts/proxy_pool.py",
    "scripts/tps_alert.py",
    "scripts/tps_control.py",
    "scripts/tps_coverage.py",
    "scripts/tps_env.py",
    "scripts/tps_metrics.py",
    "scripts/tps_plan.py",
    "scripts/tps_queue.py",
    "scripts/tps_scale.py",
    "scripts/tps_supervisor.py",
    "scripts/tps_version.py",
    "tools/dashboard-app.js",
    "tools/dashboard-runtime.js",
    "tools/dashboard.css",
    "tools/dashboard.html",
    "tools/dashboard_api.py",
)

# Keep the old dashboard-only packager from repeating an earlier archive leak.
# The legacy database value is represented only by a digest, never plaintext.
LEGACY_DB_CREDENTIAL_SHA256 = "f2c650c373692d5cd0e9a95551be2a8beb4b9367ad9bad84e05006b39c280b75"
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?m)^[ \t]*(?:PROXY_TUNNEL|TPS_DB_PASSWORD|TPS_MYSQL_ROOT_PASSWORD|TPS_REDIS_PASSWORD)[ \t]*=[ \t]*['\"]?([^'\"\s#]*)"
)
CREDENTIAL_URL_RE = re.compile(r"(?i)(?:https?|socks5?)://([^\s:@/]+):([^\s@/]+)@([^\s/'\"<>]+)")
PLACEHOLDER_CREDENTIALS = {("user", "pass"), ("username", "password"), ("account", "password"), ("账号", "密码")}

# A path must not be allowed to cross into a stateful or secret-bearing tree,
# even if a future allowlist edit accidentally names it.  `.env.example` is a
# reviewed template, not an environment file, and is allowed above.
FORBIDDEN_PATH_COMPONENTS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        "data",
        "logs",
        "log",
        "cache",
        "caches",
        "proxy",
        "proxies",
        "db",
        "database",
    }
)
FORBIDDEN_FILE_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        "dump.rdb",
        "my_us_proxies.txt",
        "proxy_config.json",
    }
)
FORBIDDEN_SUFFIXES = (
    ".db",
    ".sqlite",
    ".sqlite3",
    ".rdb",
    ".log",
    ".pid",
)
MANIFEST_NAME = "release-manifest.json"
MANIFEST_SCHEMA_VERSION = 1


class ReleaseError(RuntimeError):
    """Raised when a release cannot be safely assembled."""


@dataclass(frozen=True)
class ReleaseFile:
    archive_path: str
    source_path: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class ReleaseResult:
    output: Path
    checksum_path: Path
    extractor_path: Path
    extractor_checksum_path: Path
    archive_sha256: str
    file_count: int
    version: str
    commit: str
    source_dirty: bool


def _to_posix_path(value: str) -> PurePosixPath:
    """Validate a checked-in release path before it is resolved on disk."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise ReleaseError(f"release path must use a non-empty POSIX path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ReleaseError(f"unsafe release path: {value!r}")
    return path


def _is_forbidden_release_path(path: PurePosixPath) -> bool:
    parts = tuple(part.casefold() for part in path.parts)
    if any(part in FORBIDDEN_PATH_COMPONENTS for part in parts):
        return True
    basename = path.name.casefold()
    if basename in FORBIDDEN_FILE_NAMES:
        return True
    if basename.startswith(".env.") and basename != ".env.example":
        return True
    return basename.endswith(FORBIDDEN_SUFFIXES)


def validate_release_path(value: str) -> PurePosixPath:
    """Return a safe allowlist path or raise a clear release error."""
    path = _to_posix_path(value)
    if _is_forbidden_release_path(path):
        raise ReleaseError(f"release path is in an excluded state/configuration area: {value}")
    return path


def _resolve_existing_file(root: Path, archive_path: str, *, label: str) -> Path:
    relative = validate_release_path(archive_path)
    candidate = root.joinpath(*relative.parts)
    if candidate.is_symlink():
        raise ReleaseError(f"{label} must not be a symbolic link: {archive_path}")
    if not candidate.is_file():
        raise ReleaseError(f"required {label} is missing or not a regular file: {archive_path}")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ReleaseError(f"{label} escapes the repository root: {archive_path}") from exc
    return resolved


def scan_release_text(path: str, contents: bytes) -> None:
    """Reject credential-bearing allowlisted source before hashing or packaging."""
    if b"\0" in contents:
        raise ReleaseError(f"allowlisted source contains binary data: {path}")
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseError(f"allowlisted source is not UTF-8: {path}") from exc
    if "gate.decodo.com" in text.casefold():
        raise ReleaseError(f"provider-specific proxy material is forbidden: {path}")
    if any(
        hashlib.sha256(token.encode("ascii")).hexdigest() == LEGACY_DB_CREDENTIAL_SHA256
        for token in re.findall(r"[A-Za-z0-9]{8,}", text.casefold())
    ):
        raise ReleaseError(f"known literal database credential is forbidden: {path}")
    for match in SECRET_ASSIGNMENT_RE.finditer(text):
        if match.group(1).strip().casefold() not in {"generated_locally", ""}:
            raise ReleaseError(f"literal runtime secret assignment is forbidden: {path}")
    for match in CREDENTIAL_URL_RE.finditer(text):
        user, password, host = (part.casefold() for part in match.groups())
        hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        placeholder_host = (
            hostname in {"example.com", "example.net", "example.org"}
            or hostname.endswith((".example", ".invalid", ".example.com", ".example.net", ".example.org"))
        )
        if (user, password) not in PLACEHOLDER_CREDENTIALS and not placeholder_host:
            raise ReleaseError(f"embedded authenticated URL is forbidden: {path}")


def _validate_windows_amd64_pe(path: Path, filename: str) -> None:
    """Reject DOS stubs, non-PE files, and PE targets other than AMD64.

    A leading ``MZ`` marker only proves that a file begins like a DOS
    executable.  The release launcher manifest is an architecture claim, so
    verify the PE signature, COFF machine field, and PE32+ optional-header
    magic before accepting an artifact as a Windows x64 launcher.
    """
    try:
        file_size = path.stat().st_size
        with path.open("rb") as handle:
            dos_header = handle.read(_DOS_HEADER_SIZE)
            if len(dos_header) != _DOS_HEADER_SIZE or dos_header[:2] != b"MZ":
                raise ReleaseError(f"Windows executable is not a 64-bit AMD64 PE file: {filename}")
            pe_offset = struct.unpack_from("<I", dos_header, _DOS_E_LFANEW_OFFSET)[0]
            if pe_offset < _DOS_HEADER_SIZE or pe_offset + _PE_MINIMUM_HEADER_SIZE > file_size:
                raise ReleaseError(f"Windows executable is not a 64-bit AMD64 PE file: {filename}")
            handle.seek(pe_offset)
            pe_header = handle.read(_PE_MINIMUM_HEADER_SIZE)
    except OSError as exc:
        raise ReleaseError(f"could not read Windows executable: {filename}") from exc

    if len(pe_header) != _PE_MINIMUM_HEADER_SIZE or pe_header[:4] != _PE_SIGNATURE:
        raise ReleaseError(f"Windows executable is not a 64-bit AMD64 PE file: {filename}")
    machine = struct.unpack_from("<H", pe_header, 4)[0]
    section_count = struct.unpack_from("<H", pe_header, 4 + 2)[0]
    optional_header_size = struct.unpack_from("<H", pe_header, 4 + 16)[0]
    optional_header_magic = struct.unpack_from("<H", pe_header, 4 + _PE_COFF_HEADER_SIZE)[0]
    section_table_end = (
        pe_offset + len(_PE_SIGNATURE) + _PE_COFF_HEADER_SIZE + optional_header_size + section_count * _PE_SECTION_HEADER_SIZE
    )
    if (
        machine != IMAGE_FILE_MACHINE_AMD64
        or optional_header_magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC
        or section_count < 1
        or optional_header_size < _PE32_PLUS_MINIMUM_OPTIONAL_HEADER_SIZE
        or section_table_end > file_size
    ):
        raise ReleaseError(f"Windows executable is not a 64-bit AMD64 PE file: {filename}")


def _resolve_executable(exe_dir: Path, filename: str) -> Path:
    candidate = exe_dir / filename
    if candidate.is_symlink():
        raise ReleaseError(f"Windows executable must not be a symbolic link: {filename}")
    if not candidate.is_file():
        raise ReleaseError(f"required Windows executable is missing: {filename}")
    resolved = candidate.resolve()
    _validate_windows_amd64_pe(resolved, filename)
    return resolved


def _load_binary_manifest(exe_dir: Path, commit: str, executable_files: Sequence[ReleaseFile]) -> ReleaseFile:
    """Validate the separately built Windows launchers against this source commit."""
    candidate = exe_dir / BINARY_MANIFEST_NAME
    if candidate.is_symlink() or not candidate.is_file():
        raise ReleaseError(
            f"required Windows launcher manifest is missing: {BINARY_MANIFEST_NAME}"
        )
    manifest_path = candidate.resolve()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("Windows launcher manifest is not valid UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise ReleaseError("Windows launcher manifest must contain a JSON object")
    if data.get("schema_version") != BINARY_MANIFEST_SCHEMA_VERSION:
        raise ReleaseError("Windows launcher manifest schema is unsupported")
    if data.get("source_commit") != commit:
        raise ReleaseError("Windows launcher manifest does not match the release source commit")
    if data.get("target") != "windows-amd64":
        raise ReleaseError("Windows launcher manifest target must be windows-amd64")
    records = data.get("files")
    if not isinstance(records, list) or len(records) != len(REQUIRED_WINDOWS_EXECUTABLES):
        raise ReleaseError("Windows launcher manifest must list every required executable exactly once")

    expected = {item.archive_path: item for item in executable_files}
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise ReleaseError("Windows launcher manifest contains an invalid file record")
        filename = record.get("name")
        expected_file = expected.get(filename)
        if not isinstance(filename, str) or expected_file is None or filename in seen:
            raise ReleaseError("Windows launcher manifest names are incomplete or duplicated")
        if record.get("sha256") != expected_file.sha256 or record.get("size") != expected_file.size:
            raise ReleaseError(f"Windows launcher manifest hash mismatch: {filename}")
        seen.add(filename)
    if seen != set(expected):
        raise ReleaseError("Windows launcher manifest does not cover all required executables")

    return ReleaseFile(
        archive_path=BINARY_MANIFEST_NAME,
        source_path=manifest_path,
        sha256=_sha256_file(manifest_path),
        size=manifest_path.stat().st_size,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(root: Path, args: Sequence[str]) -> str:
    """Run a strictly local Git query with all network interaction disabled."""
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    try:
        completed = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        raise ReleaseError("git is required to record the release commit") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReleaseError("local git metadata query timed out") from exc
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "unknown git error"
        raise ReleaseError(f"local git metadata query failed: {message}")
    return completed.stdout.strip()


def inspect_source(root: Path, *, allow_dirty: bool) -> tuple[str, int, bool]:
    """Read commit metadata and reject an uncommitted source tree by default."""
    root = root.resolve()
    if not root.is_dir():
        raise ReleaseError(f"repository root does not exist: {root}")
    if _git(root, ["rev-parse", "--is-inside-work-tree"]) != "true":
        raise ReleaseError("release source must be a Git working tree")
    commit = _git(root, ["rev-parse", "HEAD"])
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit.casefold()):
        raise ReleaseError("could not determine a full Git commit hash")
    timestamp_text = _git(root, ["show", "-s", "--format=%ct", "HEAD"])
    try:
        commit_epoch = int(timestamp_text)
    except ValueError as exc:
        raise ReleaseError("could not determine the Git commit timestamp") from exc
    if commit_epoch < 0:
        raise ReleaseError("Git commit timestamp must not be negative")
    dirty = bool(_git(root, ["status", "--porcelain=v1", "--untracked-files=all"]))
    if dirty and not allow_dirty:
        raise ReleaseError(
            "refusing to package a dirty source tree; commit/stash changes or pass --allow-dirty explicitly"
        )
    return commit, commit_epoch, dirty


def _zip_datetime(source_epoch: int) -> tuple[int, int, int, int, int, int]:
    """ZIP timestamps are UTC and clamped to ZIP's 1980 lower bound."""
    minimum_epoch = int(datetime(1980, 1, 1, tzinfo=timezone.utc).timestamp())
    maximum_epoch = int(datetime(2107, 12, 31, 23, 59, 58, tzinfo=timezone.utc).timestamp())
    value = min(max(source_epoch, minimum_epoch), maximum_epoch)
    instant = datetime.fromtimestamp(value, tz=timezone.utc)
    # ZIP stores seconds with two-second precision.
    return (instant.year, instant.month, instant.day, instant.hour, instant.minute, instant.second - instant.second % 2)


def _zip_write_file(
    archive: zipfile.ZipFile,
    release_file: ReleaseFile,
    timestamp: tuple[int, int, int, int, int, int],
) -> None:
    # Hash the exact bytes that enter the archive.  This closes the gap between
    # manifest creation and ZIP writing if a source file changes mid-package.
    contents = release_file.source_path.read_bytes()
    if len(contents) != release_file.size or hashlib.sha256(contents).hexdigest() != release_file.sha256:
        raise ReleaseError(f"source file changed while packaging: {release_file.archive_path}")
    info = zipfile.ZipInfo(release_file.archive_path, date_time=timestamp)
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, contents, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def _zip_write_bytes(
    archive: zipfile.ZipFile,
    archive_path: str,
    contents: bytes,
    timestamp: tuple[int, int, int, int, int, int],
) -> None:
    info = zipfile.ZipInfo(archive_path, date_time=timestamp)
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, contents, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def _build_release_files(root: Path, exe_dir: Path, commit: str) -> tuple[ReleaseFile, ...]:
    paths = [validate_release_path(path) for path in RUNTIME_SOURCE_ALLOWLIST]
    if len({path.as_posix() for path in paths}) != len(paths):
        raise ReleaseError("runtime source allowlist contains duplicate entries")

    files = []
    for relative in paths:
        source = _resolve_existing_file(root, relative.as_posix(), label="allowlisted source file")
        scan_release_text(relative.as_posix(), source.read_bytes())
        files.append(
            ReleaseFile(
                archive_path=relative.as_posix(),
                source_path=source,
                sha256=_sha256_file(source),
                size=source.stat().st_size,
            )
        )

    executable_files = []
    for filename in REQUIRED_WINDOWS_EXECUTABLES:
        source = _resolve_executable(exe_dir, filename)
        executable_files.append(
            ReleaseFile(
                archive_path=filename,
                source_path=source,
                sha256=_sha256_file(source),
                size=source.stat().st_size,
            )
        )
    files.extend(executable_files)
    files.append(_load_binary_manifest(exe_dir, commit, executable_files))

    files.sort(key=lambda item: item.archive_path)
    archive_paths = [item.archive_path for item in files]
    if MANIFEST_NAME in archive_paths or len(set(archive_paths)) != len(archive_paths):
        raise ReleaseError("release file names collide with each other or the manifest")
    return tuple(files)


def _load_version(root: Path) -> Mapping[str, object]:
    version_file = _resolve_existing_file(root, "version.json", label="version metadata")
    try:
        value = json.loads(version_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"version.json is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseError("version.json must contain a JSON object")
    version = value.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ReleaseError("version.json must contain a non-empty string version")
    return value


def _manifest_bytes(
    version_info: Mapping[str, object],
    *,
    commit: str,
    commit_epoch: int,
    dirty: bool,
    files: Iterable[ReleaseFile],
) -> bytes:
    # The manifest deliberately has no wall-clock build timestamp or host data.
    # Its sole time value comes from the immutable Git commit.
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "version": str(version_info["version"]),
        "build": version_info.get("build"),
        "release_date": version_info.get("release_date"),
        "commit": commit,
        "commit_timestamp": commit_epoch,
        "source_dirty": dirty,
        "release_files": [
            {
                "path": item.archive_path,
                "sha256": item.sha256,
                "size": item.size,
            }
            for item in files
        ],
    }
    return (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _ensure_output_is_external(root: Path, output: Path) -> None:
    try:
        output.relative_to(root)
    except ValueError:
        return
    raise ReleaseError("output ZIP must be outside the source tree to preserve a clean release input")


def _ensure_artifact_directory_is_external(root: Path, artifact_directory: Path) -> None:
    """Keep release binaries separate from source and runtime files."""
    try:
        artifact_directory.relative_to(root)
    except ValueError:
        return
    raise ReleaseError("Windows launcher directory must be outside the source tree")


def _extractor_paths(output: Path) -> tuple[Path, Path]:
    extractor = output.with_name(f"{output.stem}-extract.ps1")
    return extractor, Path(str(extractor) + ".sha256")


def create_release(
    *,
    repo_root: Path,
    output: Path,
    exe_dir: Path,
    allow_dirty: bool = False,
    overwrite: bool = False,
) -> ReleaseResult:
    """Write a verified deterministic ZIP from the fixed release allowlist."""
    root = repo_root.resolve()
    output = output.expanduser().resolve()
    checksum_path = Path(str(output) + ".sha256")
    extractor_path, extractor_checksum_path = _extractor_paths(output)
    executable_root = exe_dir.expanduser().resolve()
    if not executable_root.is_dir():
        raise ReleaseError(f"Windows launcher directory does not exist: {executable_root}")
    _ensure_output_is_external(root, output)
    _ensure_artifact_directory_is_external(root, executable_root)
    if output.suffix.casefold() != ".zip":
        raise ReleaseError("output path must end in .zip")
    if (output.exists() or checksum_path.exists() or extractor_path.exists() or extractor_checksum_path.exists()) and not overwrite:
        raise ReleaseError(
            f"release output artifacts already exist (use --overwrite to replace them): {output}"
        )

    commit, commit_epoch, dirty = inspect_source(root, allow_dirty=allow_dirty)
    version_info = _load_version(root)
    release_files = _build_release_files(root, executable_root, commit)
    extractor_release = next(
        (item for item in release_files if item.archive_path == "deploy/windows/Expand-Release.ps1"),
        None,
    )
    if extractor_release is None:
        raise ReleaseError("release allowlist must include the external extraction helper")
    manifest = _manifest_bytes(
        version_info,
        commit=commit,
        commit_epoch=commit_epoch,
        dirty=dirty,
        files=release_files,
    )
    timestamp = _zip_datetime(commit_epoch)

    output.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Optional[Path] = None
    checksum_temp_path: Optional[Path] = None
    extractor_temp_path: Optional[Path] = None
    extractor_checksum_temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".windows-release-", suffix=".zip", dir=output.parent, delete=False
        ) as handle:
            temp_path = Path(handle.name)
        with zipfile.ZipFile(
            temp_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as archive:
            for release_file in release_files:
                _zip_write_file(archive, release_file, timestamp)
            _zip_write_bytes(archive, MANIFEST_NAME, manifest, timestamp)
        archive_sha256 = _sha256_file(temp_path)
        checksum_contents = f"{archive_sha256} *{output.name}\n".encode("utf-8")
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".windows-release-", suffix=".sha256", dir=output.parent, delete=False
        ) as checksum_handle:
            checksum_handle.write(checksum_contents)
            checksum_temp_path = Path(checksum_handle.name)
        extractor_contents = extractor_release.source_path.read_bytes()
        if (
            len(extractor_contents) != extractor_release.size
            or hashlib.sha256(extractor_contents).hexdigest() != extractor_release.sha256
        ):
            raise ReleaseError("extraction helper changed while packaging")
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".windows-release-", suffix=".ps1", dir=output.parent, delete=False
        ) as extractor_handle:
            extractor_handle.write(extractor_contents)
            extractor_temp_path = Path(extractor_handle.name)
        extractor_sha256 = _sha256_file(extractor_temp_path)
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".windows-release-", suffix=".ps1.sha256", dir=output.parent, delete=False
        ) as extractor_checksum_handle:
            extractor_checksum_handle.write(
                f"{extractor_sha256} *{extractor_path.name}\n".encode("utf-8")
            )
            extractor_checksum_temp_path = Path(extractor_checksum_handle.name)
        if (output.exists() or checksum_path.exists() or extractor_path.exists() or extractor_checksum_path.exists()) and not overwrite:
            raise ReleaseError(
                f"release output artifacts already exist (use --overwrite to replace them): {output}"
            )
        os.replace(temp_path, output)
        temp_path = None
        os.replace(checksum_temp_path, checksum_path)
        checksum_temp_path = None
        os.replace(extractor_temp_path, extractor_path)
        extractor_temp_path = None
        os.replace(extractor_checksum_temp_path, extractor_checksum_path)
        extractor_checksum_temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        if checksum_temp_path is not None:
            checksum_temp_path.unlink(missing_ok=True)
        if extractor_temp_path is not None:
            extractor_temp_path.unlink(missing_ok=True)
        if extractor_checksum_temp_path is not None:
            extractor_checksum_temp_path.unlink(missing_ok=True)

    return ReleaseResult(
        output=output,
        checksum_path=checksum_path,
        extractor_path=extractor_path,
        extractor_checksum_path=extractor_checksum_path,
        archive_sha256=archive_sha256,
        file_count=len(release_files),
        version=str(version_info["version"]),
        commit=commit,
        source_dirty=dirty,
    )


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a deterministic offline Windows release ZIP from a fixed allowlist."
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Explicit ZIP path outside the source repository (required).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Local Git working tree to package (default: this repository).",
    )
    parser.add_argument(
        "--exe-dir",
        type=Path,
        required=True,
        help="External directory containing launchers freshly built for this exact Git commit.",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Explicitly permit an uncommitted source tree; the manifest will record it as dirty.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Explicitly replace an existing ZIP at --output.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        result = create_release(
            repo_root=args.repo_root,
            output=args.output,
            exe_dir=args.exe_dir,
            allow_dirty=args.allow_dirty,
            overwrite=args.overwrite,
        )
    except ReleaseError as exc:
        print(f"release packaging failed: {exc}", file=sys.stderr)
        return 2
    print(f"release ZIP: {result.output}")
    print(f"checksum: {result.checksum_path}")
    print(f"extractor: {result.extractor_path}")
    print(f"extractor checksum: {result.extractor_checksum_path}")
    print(f"version: {result.version}")
    print(f"commit: {result.commit}")
    print(f"source_dirty: {str(result.source_dirty).lower()}")
    print(f"files: {result.file_count}")
    print(f"sha256: {result.archive_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
