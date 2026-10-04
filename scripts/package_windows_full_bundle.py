#!/usr/bin/env python3
"""Build a deterministic, secret-free, offline Windows x64 full-stack bundle.

The bundle is a transport artifact, not an installer.  It contains only an
explicit application allowlist and vendor files already authenticated by a
release engineer in ``vendor-manifest.json``.  It never downloads a package,
loads an image, starts a service, or reads runtime data.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional, Sequence


SCHEMA_VERSION = 1
TARGET = "windows-amd64"
MANIFEST_NAME = "bundle-manifest.json"

APP_ALLOWLIST = (
    "requirements.txt",
    "version.json",
    "deploy/init_db.py",
    "deploy/windows-full/Common.ps1",
    "deploy/windows-full/Initialize-Runtime.ps1",
    "deploy/windows-full/Install-DockerDesktop.ps1",
    "deploy/windows-full/Install-OfflineRuntime.ps1",
    "deploy/windows-full/Optimize-WindowsHost.ps1",
    "deploy/windows-full/README.md",
    "deploy/windows-full/Start-Stack.ps1",
    "deploy/windows-full/Start-Collector.ps1",
    "deploy/windows-full/Start-Customer.ps1",
    "deploy/windows-full/Stop-Collector.ps1",
    "deploy/windows-full/Stop-Stack.ps1",
    "deploy/windows-full/Test-CustomerEnv.ps1",
    "deploy/windows-full/Test-FullBundle.ps1",
    "deploy/windows-full/Test-Stack.ps1",
    "deploy/windows-full/docker-compose.yml",
    "deploy/windows-full/requirements-full.txt",
    "deploy/windows-full/runtime.env.example",
    "deploy/windows-full/vendor-manifest.example.json",
    "scripts/batch_ingest.py",
    "scripts/bulk_ingester_daemon.py",
    "scripts/cf_challenge.py",
    "scripts/cf_solver.py",
    "scripts/discover.py",
    "scripts/distributed_worker.py",
    "scripts/local_logs.py",
    "scripts/multi_worker_runner.py",
    "scripts/person_visibility.py",
    "scripts/phone_discover.py",
    "scripts/protocol_fetcher.py",
    "scripts/protocol_worker.py",
    "scripts/proxy_pool.py",
    "scripts/requeue_captcha.py",
    "scripts/scrape_to_tidb.py",
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
    "sql/tidb_schema.sql",
    "tools/batch_collector_100.py",
    "tools/check_stats.py",
    "tools/dashboard-app.js",
    "tools/dashboard-runtime.js",
    "tools/dashboard.css",
    "tools/dashboard.html",
    "tools/dashboard_api.py",
    "tools/db_viewer.py",
    "tools/export_csv.py",
)

REQUIRED_VENDOR_ROLES = frozenset(
    {
        "python-installer",
        "docker-desktop-installer",
        "chromium-archive",
        "mysql-image",
        "redis-image",
        "python-wheel",
    }
)
SIGNED_VENDOR_ROLES = frozenset({"python-installer", "docker-desktop-installer"})
FIXED_VENDOR_PATHS = {
    "python-installer": "python/python-3.12.10-amd64.exe",
    "docker-desktop-installer": "docker/DockerDesktopInstaller.exe",
    "chromium-archive": "browser/chromium-1243.zip",
    "mysql-image": "images/mysql-8.4.11-linux-amd64.tar",
    "redis-image": "images/redis-7.4.8-alpine-linux-amd64.tar",
}
EXPECTED_IMAGE_TAGS = {
    "mysql-image": "tps-offline/mysql:8.4.11-amd64",
    "redis-image": "tps-offline/redis:7.4.8-alpine-amd64",
}
EXPECTED_WHEEL_FILENAMES = frozenset(
    {
        "anyio-4.15.1-py3-none-any.whl",
        "apify_fingerprint_datapoints-0.15.0-py3-none-any.whl",
        "blinker-1.9.0-py3-none-any.whl",
        "browserforge-1.2.4-py3-none-any.whl",
        "certifi-2026.7.22-py3-none-any.whl",
        "cffi-2.1.1-cp312-cp312-win_amd64.whl",
        "click-8.5.0-py3-none-any.whl",
        "cssselect-1.5.0-py3-none-any.whl",
        "curl_cffi-0.16.3-cp310-abi3-win_amd64.whl",
        "flask-3.1.3-py3-none-any.whl",
        "greenlet-3.5.6-cp312-cp312-win_amd64.whl",
        "h11-0.16.0-py3-none-any.whl",
        "httpcore-1.0.9-py3-none-any.whl",
        "httpx-0.28.1-py3-none-any.whl",
        "idna-3.20-py3-none-any.whl",
        "itsdangerous-2.2.0-py3-none-any.whl",
        "jinja2-3.1.6-py3-none-any.whl",
        "lxml-6.1.3-cp312-cp312-win_amd64.whl",
        "markupsafe-3.0.3-cp312-cp312-win_amd64.whl",
        "msgspec-0.22.0-cp312-cp312-win_amd64.whl",
        "mysql_connector_python-26.7.0-py2.py3-none-any.whl",
        "orjson-3.12.0-cp312-cp312-win_amd64.whl",
        "patchright-1.63.0-py3-none-win_amd64.whl",
        "playwright-1.63.0-py3-none-win_amd64.whl",
        "protego-0.7.0-py3-none-any.whl",
        "psutil-7.2.2-cp37-abi3-win_amd64.whl",
        "pycparser-3.0-py3-none-any.whl",
        "pyee-13.0.1-py3-none-any.whl",
        "redis-8.1.0-py3-none-any.whl",
        "scrapling-0.4.15-py3-none-any.whl",
        "tabulate-0.10.0-py3-none-any.whl",
        "tld-0.13.2-py2.py3-none-any.whl",
        "typing_extensions-4.16.0-py3-none-any.whl",
        "w3lib-2.4.1-py3-none-any.whl",
        "werkzeug-3.1.9-py3-none-any.whl",
    }
)
FORBIDDEN_PARTS = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        "data",
        "logs",
        "backups",
        "config",
        "runtime",
    }
)
FORBIDDEN_NAMES = frozenset(
    {
        ".env",
        "runtime.env",
        "proxy_config.json",
        "my_us_proxies.txt",
        "dump.rdb",
    }
)
FORBIDDEN_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".rdb", ".log", ".pid")
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?m)^[ \t]*(?:PROXY_TUNNEL|TPS_DB_PASSWORD|TPS_MYSQL_ROOT_PASSWORD|TPS_REDIS_PASSWORD)[ \t]*=[ \t]*['\"]?([^'\"\s#]*)"
)
# Compare digests of standalone text tokens so the blocked legacy credential
# does not itself become a new source or release artifact secret.
LEGACY_DB_CREDENTIAL_SHA256 = "f2c650c373692d5cd0e9a95551be2a8beb4b9367ad9bad84e05006b39c280b75"
CREDENTIAL_URL_RE = re.compile(r"(?i)(?:https?|socks5?)://([^\s:@/]+):([^\s@/]+)@([^\s/'\"<>]+)")
PLACEHOLDER_CREDENTIALS = {
    ("user", "pass"),
    ("username", "password"),
    ("account", "password"),
    ("账号", "密码"),
}
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


class BundleError(RuntimeError):
    pass


@dataclass(frozen=True)
class BundleFile:
    archive_path: str
    source_path: Path
    sha256: str
    size: int
    category: str
    role: Optional[str] = None
    authenticode_thumbprint: Optional[str] = None
    image_id: Optional[str] = None
    component_versions: Optional[dict[str, str]] = None


def _windows_relative_path(value: str) -> tuple[PurePosixPath, str]:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
    ):
        raise BundleError("bundle paths must be canonical relative Windows paths")
    parts = value.split("/")
    folded_parts = []
    for part in parts:
        if part in {"", ".", ".."} or part.endswith((" ", ".")):
            raise BundleError(f"unsafe Windows bundle path: {value}")
        if len(part) > 255 or any(ord(char) < 32 or char in '<>:"|?*' for char in part):
            raise BundleError(f"Windows-incompatible bundle path: {value}")
        normalized = unicodedata.normalize("NFC", part)
        reserved_stem = normalized.split(".", 1)[0].upper()
        if reserved_stem in WINDOWS_RESERVED_NAMES:
            raise BundleError(f"reserved Windows bundle path: {value}")
        folded_parts.append(normalized.casefold())
    return PurePosixPath(*parts), "/".join(folded_parts)


def safe_relative(value: str) -> PurePosixPath:
    path, _ = _windows_relative_path(value)
    folded = tuple(part.casefold() for part in path.parts)
    if any(part in FORBIDDEN_PARTS for part in folded):
        raise BundleError(f"runtime-state path is forbidden: {value}")
    if path.name.casefold() in FORBIDDEN_NAMES or path.name.casefold().endswith(FORBIDDEN_SUFFIXES):
        raise BundleError(f"runtime-state file is forbidden: {value}")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def scan_application_text(path: str, contents: bytes) -> None:
    if b"\0" in contents:
        raise BundleError(f"allowlisted application file contains NUL bytes: {path}")
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BundleError(f"allowlisted application file is not UTF-8: {path}") from exc
    if "gate.decodo.com" in text.casefold():
        raise BundleError(f"provider-specific proxy material is forbidden in a bundle: {path}")
    if any(
        hashlib.sha256(token.encode("ascii")).hexdigest() == LEGACY_DB_CREDENTIAL_SHA256
        for token in re.findall(r"[A-Za-z0-9]{8,}", text.casefold())
    ):
        raise BundleError(f"known literal database credential is forbidden in a bundle: {path}")
    for match in SECRET_ASSIGNMENT_RE.finditer(text):
        if match.group(1).strip().casefold() not in {"generated_locally", ""}:
            raise BundleError(f"literal runtime secret assignment is forbidden: {path}")
    for match in CREDENTIAL_URL_RE.finditer(text):
        user, password, host = (value.casefold() for value in match.groups())
        hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        reserved_host = (
            hostname in {"example.com", "example.net", "example.org"}
            or hostname.endswith((".example", ".invalid", ".example.com", ".example.net", ".example.org"))
        )
        if (user, password) in PLACEHOLDER_CREDENTIALS or reserved_host:
            continue
        raise BundleError(f"embedded authenticated URL is forbidden: {path}")


def _existing_regular(root: Path, relative: PurePosixPath, label: str) -> Path:
    candidate = root.joinpath(*relative.parts)
    if candidate.is_symlink() or not candidate.is_file():
        raise BundleError(f"required {label} is missing or not a regular file: {relative}")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise BundleError(f"{label} escapes its approved root: {relative}") from exc
    return resolved


def _open_regular_under_root(root: Path, relative: PurePosixPath, label: str) -> int:
    """Open a leaf below root without following any relative-path symlink."""
    file_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    supports_dir_fd = os.open in getattr(os, "supports_dir_fd", set())
    if supports_dir_fd and hasattr(os, "O_DIRECTORY"):
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        opened_directories: list[int] = []
        try:
            current = os.open(root, directory_flags)
            opened_directories.append(current)
            for part in relative.parts[:-1]:
                current = os.open(part, directory_flags, dir_fd=current)
                opened_directories.append(current)
            descriptor = os.open(relative.name, file_flags, dir_fd=current)
        except OSError as exc:
            raise BundleError(f"required {label} is missing or unsafe: {relative}") from exc
        finally:
            for directory in reversed(opened_directories):
                os.close(directory)
        return descriptor

    candidate = _existing_regular(root, relative, label)
    try:
        return os.open(candidate, file_flags)
    except OSError as exc:
        raise BundleError(f"required {label} is missing or unsafe: {relative}") from exc


def _safe_archive_member(name: str) -> bool:
    normalized = str(name or "").replace("\\", "/")
    path = PurePosixPath(normalized)
    return bool(normalized) and not normalized.startswith("/") and not path.is_absolute() and ".." not in path.parts


def _safe_windows_archive_member(name: str) -> tuple[str, str]:
    if not isinstance(name, str) or "\\" in name:
        raise BundleError("Chromium archive contains a non-canonical Windows path")
    normalized = name.rstrip("/")
    if not normalized:
        raise BundleError("Chromium archive contains an empty path")
    path, folded = _windows_relative_path(normalized)
    return path.as_posix(), folded


def _validate_chromium_archive(source: Path) -> None:
    required = {
        "chromium-1243/INSTALLATION_COMPLETE",
        "chromium-1243/chrome-win64/chrome.exe",
        "chromium_headless_shell-1243/INSTALLATION_COMPLETE",
        "chromium_headless_shell-1243/chrome-headless-shell-win64/chrome-headless-shell.exe",
    }
    try:
        with zipfile.ZipFile(source) as archive:
            names = set()
            folded_names = set()
            file_names = set()
            entries_by_name: dict[str, zipfile.ZipInfo] = {}
            for entry in archive.infolist():
                name, folded = _safe_windows_archive_member(entry.filename)
                if folded in folded_names:
                    raise BundleError("Chromium archive contains an unsafe or duplicate path")
                if entry.flag_bits & 0x1:
                    raise BundleError("Chromium archive contains an encrypted entry")
                # Reject Unix symlinks. Windows attributes alone leave this as zero.
                file_type = (entry.external_attr >> 16) & 0o170000
                if file_type == 0o120000:
                    raise BundleError("Chromium archive contains a symbolic link")
                if file_type not in {0, 0o040000, 0o100000}:
                    raise BundleError("Chromium archive contains a non-regular entry")
                ancestors = folded.split("/")[:-1]
                for index in range(1, len(ancestors) + 1):
                    if "/".join(ancestors[:index]) in file_names:
                        raise BundleError("Chromium archive nests content below a file path")
                if not entry.is_dir() and any(existing.startswith(folded + "/") for existing in folded_names):
                    raise BundleError("Chromium archive file path collides with an existing directory")
                names.add(name)
                folded_names.add(folded)
                entries_by_name[name] = entry
                if not entry.is_dir():
                    file_names.add(folded)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BundleError("Chromium vendor asset is not a valid ZIP archive") from exc
    if required - names:
        raise BundleError("Chromium archive is incomplete")
    executable_names = {
        "chromium-1243/chrome-win64/chrome.exe",
        "chromium_headless_shell-1243/chrome-headless-shell-win64/chrome-headless-shell.exe",
    }
    for name in required:
        entry = entries_by_name[name]
        if entry.is_dir():
            raise BundleError("Chromium archive required marker is not a regular file")
        file_type = (entry.external_attr >> 16) & 0o170000
        if file_type not in {0, 0o100000}:
            raise BundleError("Chromium archive required marker is not a regular file")
        if name in executable_names and entry.file_size <= 0:
            raise BundleError("Chromium archive contains an empty browser executable")


def _validate_docker_image_archive(source: Path, role: str) -> str:
    expected_tag = EXPECTED_IMAGE_TAGS[role]
    try:
        with tarfile.open(source, mode="r") as archive:
            members = archive.getmembers()
            names = set()
            for member in members:
                normalized = member.name.replace("\\", "/").rstrip("/")
                if not normalized:
                    continue
                if not _safe_archive_member(normalized) or normalized in names:
                    raise BundleError("Docker image archive contains an unsafe or duplicate path")
                if member.issym() or member.islnk():
                    raise BundleError("Docker image archive contains a link")
                names.add(normalized)
            manifest_member = archive.getmember("manifest.json")
            if not manifest_member.isfile() or manifest_member.size > 1024 * 1024:
                raise BundleError("Docker image archive manifest is invalid")
            manifest_stream = archive.extractfile(manifest_member)
            if manifest_stream is None:
                raise BundleError("Docker image archive manifest is unavailable")
            manifest = json.load(manifest_stream)
            if not isinstance(manifest, list) or len(manifest) != 1:
                raise BundleError("Docker image archive must contain exactly one image")
            record = manifest[0]
            if not isinstance(record, dict) or record.get("RepoTags") != [expected_tag]:
                raise BundleError("Docker image archive has the wrong offline tag")
            layers = record.get("Layers")
            if (
                not isinstance(layers, list)
                or not layers
                or not all(isinstance(layer, str) and _safe_archive_member(layer) for layer in layers)
                or len(set(layers)) != len(layers)
            ):
                raise BundleError("Docker image archive has an invalid layer closure")
            config_name = record.get("Config")
            if not isinstance(config_name, str) or not _safe_archive_member(config_name):
                raise BundleError("Docker image archive has an unsafe config path")
            config_member = archive.getmember(config_name)
            if not config_member.isfile() or config_member.size > 1024 * 1024:
                raise BundleError("Docker image archive config is invalid")
            config_stream = archive.extractfile(config_member)
            if config_stream is None:
                raise BundleError("Docker image archive config is unavailable")
            config_bytes = config_stream.read()
            config = json.loads(config_bytes)
            if config.get("os") != "linux" or config.get("architecture") != "amd64":
                raise BundleError("Docker image archive is not Linux/amd64")
            rootfs = config.get("rootfs")
            diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
            if (
                not isinstance(rootfs, dict)
                or rootfs.get("type") != "layers"
                or not isinstance(diff_ids, list)
                or len(diff_ids) != len(layers)
                or not all(
                    isinstance(diff_id, str)
                    and re.fullmatch(r"sha256:[a-f0-9]{64}", diff_id)
                    for diff_id in diff_ids
                )
            ):
                raise BundleError("Docker image archive config has an invalid rootfs closure")
            for layer_name, diff_id in zip(layers, diff_ids):
                layer_member = archive.getmember(layer_name)
                if not layer_member.isfile() or layer_member.size <= 0:
                    raise BundleError("Docker image archive contains an invalid layer member")
                layer_stream = archive.extractfile(layer_member)
                if layer_stream is None:
                    raise BundleError("Docker image archive layer is unavailable")
                prefix = layer_stream.read(2)
                layer_stream.seek(0)
                payload = gzip.GzipFile(fileobj=layer_stream) if prefix == b"\x1f\x8b" else layer_stream
                digest = hashlib.sha256()
                try:
                    for block in iter(lambda: payload.read(1024 * 1024), b""):
                        digest.update(block)
                finally:
                    if payload is not layer_stream:
                        payload.close()
                if "sha256:" + digest.hexdigest() != diff_id:
                    raise BundleError("Docker image archive layer does not match its rootfs diff ID")
            config_digest = hashlib.sha256(config_bytes).hexdigest()
            if PurePosixPath(config_name).name not in {config_digest, config_digest + ".json"}:
                raise BundleError("Docker image archive config is not content-addressed")
            return "sha256:" + config_digest
    except BundleError:
        raise
    except (OSError, EOFError, tarfile.TarError, KeyError, json.JSONDecodeError) as exc:
        raise BundleError("Docker image vendor asset is not a valid docker-save archive") from exc


def _validate_python_wheel(source: Path) -> None:
    filename_match = re.fullmatch(
        r"(?P<name>[A-Za-z0-9_.]+)-(?P<version>[A-Za-z0-9_.!+]+)-"
        r"(?P<python>[^-]+)-(?P<abi>[^-]+)-(?P<platform>[^-]+)\.whl",
        source.name,
    )
    if not filename_match:
        raise BundleError("Python wheel filename is not a supported normalized wheel name")
    try:
        with zipfile.ZipFile(source) as archive:
            names = [entry.filename.replace("\\", "/") for entry in archive.infolist()]
            wheel_files = [name for name in names if name.endswith(".dist-info/WHEEL")]
            metadata_files = [name for name in names if name.endswith(".dist-info/METADATA")]
            if len(wheel_files) != 1 or len(metadata_files) != 1:
                raise BundleError("Python wheel must contain exactly one WHEEL and METADATA record")
            if wheel_files[0].rsplit("/", 1)[0] != metadata_files[0].rsplit("/", 1)[0]:
                raise BundleError("Python wheel metadata directories do not match")
            wheel_bytes = archive.read(wheel_files[0])
            metadata_bytes = archive.read(metadata_files[0])
            if len(wheel_bytes) > 1024 * 1024 or len(metadata_bytes) > 4 * 1024 * 1024:
                raise BundleError("Python wheel metadata is unreasonably large")
            wheel_text = wheel_bytes.decode("utf-8")
            metadata_text = metadata_bytes.decode("utf-8")
    except BundleError:
        raise
    except (OSError, zipfile.BadZipFile, KeyError, UnicodeDecodeError) as exc:
        raise BundleError("Python wheel is not a valid metadata-bearing ZIP archive") from exc
    if not re.search(r"(?m)^Wheel-Version:\s*\d+\.\d+\s*$", wheel_text):
        raise BundleError("Python wheel lacks Wheel-Version metadata")
    tags = re.findall(r"(?m)^Tag:\s*([^\s]+)\s*$", wheel_text)
    filename_python_tags = set(filename_match.group("python").split("."))
    filename_abi_tags = set(filename_match.group("abi").split("."))
    filename_platform_tags = set(filename_match.group("platform").split("."))
    compatible = False
    for tag in tags:
        parts = tag.split("-")
        if len(parts) != 3:
            continue
        interpreter, abi, platform = parts
        if interpreter not in filename_python_tags or abi not in filename_abi_tags:
            raise BundleError("Python wheel WHEEL tags disagree with its filename")
        if platform not in filename_platform_tags and platform != "any":
            raise BundleError("Python wheel platform tag disagrees with its filename")
        if platform not in {"any", "win_amd64"}:
            continue
        if interpreter in {"py2.py3", "py3", "cp312"}:
            compatible = True
        elif abi == "abi3" and re.fullmatch(r"cp(?:3[7-9]|31[0-2])", interpreter):
            compatible = True
    if not compatible:
        raise BundleError("Python wheel is not compatible with CPython 3.12 on Windows amd64")
    metadata_name = re.search(r"(?m)^Name:\s*(\S+)\s*$", metadata_text)
    metadata_version = re.search(r"(?m)^Version:\s*(\S+)\s*$", metadata_text)
    if not metadata_name or not metadata_version:
        raise BundleError("Python wheel lacks package name/version metadata")
    normalize_name = lambda value: re.sub(r"[-_.]+", "-", value).casefold()
    if normalize_name(metadata_name.group(1)) != normalize_name(filename_match.group("name")):
        raise BundleError("Python wheel METADATA name disagrees with its filename")
    if metadata_version.group(1) != filename_match.group("version"):
        raise BundleError("Python wheel METADATA version disagrees with its filename")


def validate_vendor_payload(source: Path, relative: PurePosixPath, role: str) -> Optional[str]:
    path_text = relative.as_posix()
    fixed = FIXED_VENDOR_PATHS.get(role)
    if fixed is not None and path_text != fixed:
        raise BundleError(f"vendor role {role} must use path {fixed}")
    if role == "python-wheel":
        if len(relative.parts) != 2 or relative.parts[0] != "wheelhouse" or source.suffix.casefold() != ".whl":
            raise BundleError("Python wheels must be direct .whl files under wheelhouse/")
        _validate_python_wheel(source)
    elif role == "chromium-archive":
        _validate_chromium_archive(source)
    elif role in EXPECTED_IMAGE_TAGS:
        return _validate_docker_image_archive(source, role)
    return None


def build_application_files(repo_root: Path) -> list[BundleFile]:
    paths = [safe_relative(path) for path in APP_ALLOWLIST]
    if len(set(paths)) != len(paths):
        raise BundleError("application allowlist contains duplicate paths")
    result = []
    for relative in paths:
        source = _existing_regular(repo_root, relative, "application file")
        contents = source.read_bytes()
        scan_application_text(relative.as_posix(), contents)
        result.append(
            BundleFile(
                archive_path="app/" + relative.as_posix(),
                source_path=source,
                sha256=hashlib.sha256(contents).hexdigest(),
                size=len(contents),
                category="application",
            )
        )
    return result


def _git_blob(root: Path, commit: str, relative: PurePosixPath) -> bytes:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    completed = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(root),
            "cat-file",
            "blob",
            f"{commit}:{relative.as_posix()}",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        env=environment,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise BundleError(detail or f"committed application file is missing: {relative}")
    return completed.stdout


def build_committed_application_files(
    repo_root: Path, commit: str, stage_root: Path
) -> list[BundleFile]:
    """Materialize allowlisted application bytes from an immutable Git object."""
    paths = [safe_relative(path) for path in APP_ALLOWLIST]
    if len(set(paths)) != len(paths):
        raise BundleError("application allowlist contains duplicate paths")
    result = []
    for relative in paths:
        contents = _git_blob(repo_root, commit, relative)
        scan_application_text(relative.as_posix(), contents)
        staged = stage_root.joinpath(*relative.parts)
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(contents)
        result.append(
            BundleFile(
                archive_path="app/" + relative.as_posix(),
                source_path=staged,
                sha256=hashlib.sha256(contents).hexdigest(),
                size=len(contents),
                category="application",
            )
        )
    return result


def load_vendor_files(
    vendor_root: Path, manifest_path: Path, stage_root: Path
) -> list[BundleFile]:
    """Copy reviewed vendor inputs once, then validate and package staged bytes.

    Vendor inputs may be located on removable or otherwise mutable storage.  A
    successful review must therefore bind the exact bytes later written to the
    bundle, not a path that can be replaced between validation and archiving.
    """
    root = vendor_root.resolve()
    expected_manifest = root / "vendor-manifest.json"
    if manifest_path.is_symlink():
        raise BundleError("vendor-manifest.json must not be a symbolic link")
    requested_manifest = manifest_path.resolve()
    if requested_manifest != expected_manifest:
        raise BundleError("vendor manifest must be exactly vendor-root/vendor-manifest.json")
    stage = Path(stage_root)
    stage.mkdir(parents=True, exist_ok=False)

    try:
        descriptor = _open_regular_under_root(
            root, PurePosixPath("vendor-manifest.json"), "vendor manifest"
        )
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > 16 * 1024 * 1024:
                raise BundleError("vendor-manifest.json is not a bounded regular file")
            manifest_bytes = handle.read(16 * 1024 * 1024 + 1)
    except BundleError:
        raise
    except OSError as exc:
        raise BundleError("vendor-manifest.json is missing or unreadable") from exc

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise BundleError(f"vendor manifest contains a duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        scan_application_text("vendor/vendor-manifest.json", manifest_bytes)
        manifest = json.loads(
            manifest_bytes.decode("utf-8"), object_pairs_hook=reject_duplicate_keys
        )
    except BundleError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError("vendor manifest is not valid UTF-8 JSON") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "target", "files"}
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("target") != TARGET
    ):
        raise BundleError("vendor manifest schema or target is invalid")
    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        raise BundleError("vendor manifest has no file records")

    seen_paths: set[str] = set()
    seen_windows_paths: set[str] = set()
    seen_roles: set[str] = set()
    wheel_filenames: set[str] = set()
    canonical_records: list[dict[str, object]] = []
    result = []
    for record in records:
        if not isinstance(record, dict):
            raise BundleError("vendor manifest contains an invalid file record")
        relative = safe_relative(record.get("path"))
        path_text = relative.as_posix()
        _, windows_path = _windows_relative_path(path_text)
        role = record.get("role")
        if not isinstance(role, str) or role not in REQUIRED_VENDOR_ROLES:
            raise BundleError("vendor manifest contains an unsupported role")
        expected_fields = {"path", "role", "sha256", "size"}
        if role in SIGNED_VENDOR_ROLES:
            expected_fields.add("authenticode_thumbprint")
        if role == "docker-desktop-installer":
            expected_fields.add("component_versions")
        if set(record) != expected_fields:
            raise BundleError("vendor manifest record contains unsupported or missing fields")
        expected_hash = record.get("sha256")
        size = record.get("size")
        if path_text in seen_paths or windows_path in seen_windows_paths:
            raise BundleError("vendor manifest contains a duplicate path or unsupported role")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[A-Fa-f0-9]{64}", expected_hash):
            raise BundleError(f"vendor manifest has an invalid hash: {path_text}")
        if type(size) is not int or size <= 0:
            raise BundleError(f"vendor manifest has an invalid size: {path_text}")
        thumbprint = record.get("authenticode_thumbprint")
        if role in SIGNED_VENDOR_ROLES:
            if not isinstance(thumbprint, str) or not re.fullmatch(r"[A-Fa-f0-9]{40}", thumbprint):
                raise BundleError(f"signed vendor asset lacks a trusted signer thumbprint: {path_text}")
            thumbprint = thumbprint.upper()
        elif thumbprint is not None:
            raise BundleError(f"unexpected signer thumbprint on non-executable vendor asset: {path_text}")
        component_versions = record.get("component_versions")
        if role == "docker-desktop-installer":
            required_version_keys = {
                "desktop_product_version", "docker_cli_version",
                "compose_version", "engine_version",
            }
            if (
                not isinstance(component_versions, dict)
                or set(component_versions) != required_version_keys
                or not all(
                    isinstance(value, str)
                    and re.fullmatch(r"\d+\.\d+\.\d+(?:\.\d+)?", value)
                    for value in component_versions.values()
                )
            ):
                raise BundleError("Docker Desktop vendor record lacks exact component versions")
            component_versions = dict(sorted(component_versions.items()))
        elif component_versions is not None:
            raise BundleError(f"unexpected component versions on vendor asset: {path_text}")

        staged = stage.joinpath(*relative.parts)
        staged.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        actual_size = 0
        try:
            descriptor = _open_regular_under_root(root, relative, "vendor file")
            with os.fdopen(descriptor, "rb") as source_handle, staged.open("xb") as target_handle:
                source_metadata = os.fstat(source_handle.fileno())
                if not stat.S_ISREG(source_metadata.st_mode):
                    raise BundleError(f"vendor input is not a regular file: {path_text}")
                for block in iter(lambda: source_handle.read(1024 * 1024), b""):
                    actual_size += len(block)
                    if actual_size > size:
                        raise BundleError(f"vendor file does not match its reviewed manifest: {path_text}")
                    digest.update(block)
                    target_handle.write(block)
        except BundleError:
            raise
        except OSError as exc:
            raise BundleError(f"vendor input could not be staged safely: {path_text}") from exc
        actual_hash = digest.hexdigest()
        if actual_size != size or actual_hash.casefold() != expected_hash.casefold():
            raise BundleError(f"vendor file does not match its reviewed manifest: {path_text}")
        image_id = validate_vendor_payload(staged, relative, role)
        seen_paths.add(path_text)
        seen_windows_paths.add(windows_path)
        seen_roles.add(role)
        if role == "python-wheel":
            wheel_filenames.add(relative.name)
        result.append(
            BundleFile(
                archive_path="vendor/" + path_text,
                source_path=staged,
                sha256=actual_hash,
                size=actual_size,
                category="vendor",
                role=role,
                authenticode_thumbprint=thumbprint,
                image_id=image_id,
                component_versions=component_versions,
            )
        )
        canonical_record: dict[str, object] = {
            "path": path_text,
            "role": role,
            "sha256": actual_hash,
            "size": actual_size,
        }
        if thumbprint:
            canonical_record["authenticode_thumbprint"] = thumbprint
        if component_versions:
            canonical_record["component_versions"] = component_versions
        canonical_records.append(canonical_record)

    missing_roles = REQUIRED_VENDOR_ROLES - seen_roles
    if missing_roles:
        raise BundleError("vendor manifest is missing roles: " + ", ".join(sorted(missing_roles)))
    if wheel_filenames != EXPECTED_WHEEL_FILENAMES:
        missing = EXPECTED_WHEEL_FILENAMES - wheel_filenames
        extra = wheel_filenames - EXPECTED_WHEEL_FILENAMES
        detail = []
        if missing:
            detail.append("missing=" + ",".join(sorted(missing)))
        if extra:
            detail.append("extra=" + ",".join(sorted(extra)))
        raise BundleError("offline wheelhouse does not match the locked Windows closure: " + "; ".join(detail))

    canonical_manifest = {
        "schema_version": 1,
        "target": TARGET,
        "files": canonical_records,
    }
    canonical_bytes = (
        json.dumps(canonical_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    scan_application_text("vendor/vendor-manifest.json", canonical_bytes)
    staged_manifest = stage / "vendor-manifest.json"
    staged_manifest.write_bytes(canonical_bytes)
    manifest_hash = hashlib.sha256(canonical_bytes).hexdigest()
    result.append(
        BundleFile(
            archive_path="vendor/vendor-manifest.json",
            source_path=staged_manifest,
            sha256=manifest_hash,
            size=len(canonical_bytes),
            category="vendor-manifest",
        )
    )
    return result


def _git(root: Path, args: Sequence[str]) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    completed = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
        env=environment,
    )
    if completed.returncode != 0:
        raise BundleError(completed.stderr.strip() or "local Git query failed")
    return completed.stdout.strip()


def inspect_source(root: Path, allow_dirty: bool) -> tuple[str, int, bool]:
    if _git(root, ["rev-parse", "--is-inside-work-tree"]) != "true":
        raise BundleError("application source must be a Git working tree")
    commit = _git(root, ["rev-parse", "HEAD"])
    if not re.fullmatch(r"[A-Fa-f0-9]{40}", commit):
        raise BundleError("could not determine a full source commit")
    epoch = int(_git(root, ["show", "-s", "--format=%ct", "HEAD"]))
    dirty = bool(_git(root, ["status", "--porcelain=v1", "--untracked-files=all"]))
    if dirty and not allow_dirty:
        raise BundleError("refusing to package a dirty source tree")
    return commit.lower(), epoch, dirty


def _zip_time(epoch: int) -> tuple[int, int, int, int, int, int]:
    minimum = int(datetime(1980, 1, 1, tzinfo=timezone.utc).timestamp())
    maximum = int(datetime(2107, 12, 31, 23, 59, 58, tzinfo=timezone.utc).timestamp())
    instant = datetime.fromtimestamp(min(max(epoch, minimum), maximum), tz=timezone.utc)
    return (instant.year, instant.month, instant.day, instant.hour, instant.minute, instant.second // 2 * 2)


def manifest_bytes(files: Iterable[BundleFile], commit: str, epoch: int, dirty: bool) -> bytes:
    records = []
    for item in sorted(files, key=lambda value: value.archive_path):
        record = {
            "category": item.category,
            "path": item.archive_path,
            "sha256": item.sha256,
            "size": item.size,
        }
        if item.role:
            record["role"] = item.role
        if item.authenticode_thumbprint:
            record["authenticode_thumbprint"] = item.authenticode_thumbprint
        if item.image_id:
            record["image_id"] = item.image_id
        if item.component_versions:
            record["component_versions"] = item.component_versions
        records.append(record)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "target": TARGET,
        "commit": commit,
        "commit_timestamp": epoch,
        "source_dirty": dirty,
        "files": records,
    }
    return (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _write_record(archive: zipfile.ZipFile, item: BundleFile, timestamp: tuple[int, ...]) -> None:
    info = zipfile.ZipInfo(item.archive_path, timestamp)
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    already_compressed = item.source_path.suffix.casefold() in {".exe", ".whl", ".zip", ".tar"}
    info.compress_type = zipfile.ZIP_STORED if already_compressed else zipfile.ZIP_DEFLATED
    digest = hashlib.sha256()
    size = 0
    with item.source_path.open("rb") as source, archive.open(info, "w", force_zip64=True) as target:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
            target.write(block)
    if size != item.size or digest.hexdigest() != item.sha256:
        raise BundleError(f"file changed during packaging: {item.archive_path}")


def create_bundle(
    repo_root: Path,
    vendor_root: Path,
    vendor_manifest: Path,
    output: Path,
    *,
    allow_dirty: bool = False,
    overwrite: bool = False,
) -> tuple[str, int]:
    root = repo_root.resolve()
    output = output.expanduser().resolve()
    checksum = Path(str(output) + ".sha256")
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise BundleError("bundle output must be outside the source tree")
    if output.suffix.casefold() != ".zip":
        raise BundleError("bundle output must end in .zip")
    if (output.exists() or checksum.exists()) and not overwrite:
        raise BundleError("bundle output already exists")

    output.parent.mkdir(parents=True, exist_ok=True)
    commit, epoch, dirty = inspect_source(root, allow_dirty)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".tps-source-") as source_stage_name:
        source_stage = Path(source_stage_name)
        # A clean artifact is built only from the pinned commit.  Dirty bundles
        # remain diagnostic-only and are rejected by Test-FullBundle.ps1.
        files = (
            build_application_files(root)
            if dirty
            else build_committed_application_files(root, commit, source_stage)
        )
        files.extend(
            load_vendor_files(
                vendor_root.resolve(),
                vendor_manifest.resolve(),
                source_stage / "vendor-stage",
            )
        )
        archive_paths = [item.archive_path for item in files]
        if len(set(archive_paths)) != len(archive_paths) or MANIFEST_NAME in archive_paths:
            raise BundleError("bundle file paths collide")
        manifest = manifest_bytes(files, commit, epoch, dirty)
        timestamp = _zip_time(epoch)

        temporary: Optional[Path] = None
        checksum_temporary: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".tps-full-", suffix=".zip", delete=False) as handle:
                temporary = Path(handle.name)
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED,
                compresslevel=9, allowZip64=True,
            ) as archive:
                for item in sorted(files, key=lambda value: value.archive_path):
                    _write_record(archive, item, timestamp)
                info = zipfile.ZipInfo(MANIFEST_NAME, timestamp)
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, manifest, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            digest = sha256_file(temporary)
            with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".tps-full-", suffix=".sha256", delete=False, mode="w", encoding="ascii") as handle:
                handle.write(f"{digest} *{output.name}\n")
                checksum_temporary = Path(handle.name)
            if (output.exists() or checksum.exists()) and not overwrite:
                raise BundleError("bundle output appeared during packaging")
            os.replace(temporary, output)
            temporary = None
            os.replace(checksum_temporary, checksum)
            checksum_temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if checksum_temporary is not None:
                checksum_temporary.unlink(missing_ok=True)
        return digest, len(files)


def _arguments(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--vendor-root", type=Path, required=True)
    parser.add_argument("--vendor-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _arguments(argv)
    try:
        digest, count = create_bundle(
            args.repo_root,
            args.vendor_root,
            args.vendor_manifest,
            args.output,
            allow_dirty=args.allow_dirty,
            overwrite=args.overwrite,
        )
    except (BundleError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"full bundle packaging failed: {exc}", file=sys.stderr)
        return 2
    print(f"bundle: {args.output.expanduser().resolve()}")
    print(f"files: {count}")
    print(f"sha256: {digest}")
    print("No installer, service, database, queue, browser, or collector was started.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
