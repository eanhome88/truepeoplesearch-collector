#!/usr/bin/env python3
"""Record the exact Windows launchers built from a clean source commit.

This tool only writes a provenance manifest beside already-built PE files. It
does not build, sign, package, upload, deploy, or start anything. Run it after
any approved Windows code-signing step so the recorded hashes describe the
actual deliverables.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Sequence


EXECUTABLES = (
    "TruePeopleSearch.exe",
    "TruePeopleSearch_后台无窗启动.exe",
    "TruePeopleSearch_停止.exe",
)
MANIFEST_NAME = "windows-launchers.manifest.json"
SCHEMA_VERSION = 1
IMAGE_FILE_MACHINE_AMD64 = 0x8664
IMAGE_NT_OPTIONAL_HDR64_MAGIC = 0x20B
_DOS_HEADER_SIZE = 0x40
_DOS_E_LFANEW_OFFSET = 0x3C
_PE_SIGNATURE = b"PE\0\0"
_PE_COFF_HEADER_SIZE = 20
_PE_MINIMUM_HEADER_SIZE = len(_PE_SIGNATURE) + _PE_COFF_HEADER_SIZE + 2
_PE32_PLUS_MINIMUM_OPTIONAL_HEADER_SIZE = 0x70
_PE_SECTION_HEADER_SIZE = 40


class ManifestError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(root: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"})
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
    if completed.returncode != 0:
        raise ManifestError("could not read local Git release metadata")
    return completed.stdout.strip()


def _ensure_artifact_directory_is_external(root: Path, directory: Path) -> None:
    try:
        directory.relative_to(root)
    except ValueError:
        return
    raise ManifestError("Windows launcher directory must be outside the source tree")


def _validate_windows_amd64_pe(path: Path, filename: str) -> None:
    """Confirm that an artifact is a PE32+ AMD64 executable, not an MZ stub."""
    try:
        file_size = path.stat().st_size
        with path.open("rb") as handle:
            dos_header = handle.read(_DOS_HEADER_SIZE)
            if len(dos_header) != _DOS_HEADER_SIZE or dos_header[:2] != b"MZ":
                raise ManifestError(f"Windows executable is not a 64-bit AMD64 PE file: {filename}")
            pe_offset = struct.unpack_from("<I", dos_header, _DOS_E_LFANEW_OFFSET)[0]
            if pe_offset < _DOS_HEADER_SIZE or pe_offset + _PE_MINIMUM_HEADER_SIZE > file_size:
                raise ManifestError(f"Windows executable is not a 64-bit AMD64 PE file: {filename}")
            handle.seek(pe_offset)
            pe_header = handle.read(_PE_MINIMUM_HEADER_SIZE)
    except OSError as exc:
        raise ManifestError(f"could not read Windows executable: {filename}") from exc

    if len(pe_header) != _PE_MINIMUM_HEADER_SIZE or pe_header[:4] != _PE_SIGNATURE:
        raise ManifestError(f"Windows executable is not a 64-bit AMD64 PE file: {filename}")
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
        raise ManifestError(f"Windows executable is not a 64-bit AMD64 PE file: {filename}")


def create_manifest(repo_root: Path, exe_dir: Path, *, overwrite: bool = False) -> Path:
    root = repo_root.resolve()
    directory = exe_dir.resolve()
    if not directory.is_dir():
        raise ManifestError("Windows launcher directory does not exist")
    _ensure_artifact_directory_is_external(root, directory)
    if _git(root, "rev-parse", "--is-inside-work-tree") != "true":
        raise ManifestError("release source must be a Git working tree")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ManifestError("refusing to record launchers from a dirty source tree")
    commit = _git(root, "rev-parse", "HEAD")
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit.casefold()):
        raise ManifestError("could not determine the release source commit")

    files = []
    for name in EXECUTABLES:
        candidate = directory / name
        if candidate.is_symlink() or not candidate.is_file():
            raise ManifestError(f"required Windows executable is missing: {name}")
        _validate_windows_amd64_pe(candidate.resolve(), name)
        files.append({"name": name, "sha256": _sha256(candidate), "size": candidate.stat().st_size})

    manifest_path = directory / MANIFEST_NAME
    if manifest_path.exists() and not overwrite:
        raise ManifestError("launcher manifest already exists; use --overwrite only after reviewing the binaries")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_commit": commit,
        "target": "windows-amd64",
        "files": files,
    }
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, prefix=".windows-launchers-", suffix=".json", delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, manifest_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return manifest_path


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Write a provenance manifest for reviewed Windows launchers.")
    parser.add_argument(
        "--exe-dir",
        type=Path,
        required=True,
        help="External artifact directory containing the three reviewed Windows launchers.",
    )
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        output = create_manifest(args.repo_root, args.exe_dir, overwrite=args.overwrite)
    except (ManifestError, OSError) as exc:
        print(f"launcher manifest failed: {exc}", file=sys.stderr)
        return 2
    print(f"launcher manifest: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
