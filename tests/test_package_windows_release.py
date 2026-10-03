"""Focused tests for the deterministic offline Windows release packager."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from typing import Optional


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "package_windows_release.py"
SPEC = importlib.util.spec_from_file_location("package_windows_release", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
packager = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = packager
SPEC.loader.exec_module(packager)


def _amd64_pe_fixture(*, machine: int = 0x8664, optional_header_magic: int = 0x20B) -> bytes:
    """Small structurally valid PE fixture; it is never executed."""
    contents = bytearray(0x200)
    contents[:2] = b"MZ"
    pe_offset = 0x80
    struct.pack_into("<I", contents, 0x3C, pe_offset)
    contents[pe_offset : pe_offset + 4] = b"PE\0\0"
    struct.pack_into("<H", contents, pe_offset + 4, machine)
    struct.pack_into("<H", contents, pe_offset + 4 + 2, 1)
    struct.pack_into("<H", contents, pe_offset + 4 + 16, 0xF0)
    struct.pack_into("<H", contents, pe_offset + 4 + 20, optional_header_magic)
    return bytes(contents)


@unittest.skipUnless(shutil.which("git"), "requires local git")
class WindowsReleasePackagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.repo = self.temp_path / "source"
        self.exe_dir = self.temp_path / "windows-launchers"
        self.output_dir = self.temp_path / "artifacts"
        self._make_clean_repo()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run_git(self, *args: str, env: Optional[dict] = None) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        return completed.stdout.strip()

    def _make_clean_repo(self) -> None:
        for relative in packager.RUNTIME_SOURCE_ALLOWLIST:
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if relative == "version.json":
                path.write_text(
                    json.dumps(
                        {
                            "version": "9.8.7",
                            "build": "fixture-build",
                            "release_date": "2026-09-28",
                        },
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            else:
                path.write_text(f"fixture source: {relative}\n", encoding="utf-8")
        # These are deliberately present in the source tree, but a release must
        # never include them because it copies only the reviewed allowlist.
        (self.repo / ".env").write_text("TOKEN=not-for-release\n", encoding="utf-8")
        (self.repo / "data" / "logs").mkdir(parents=True)
        (self.repo / "data" / "logs" / "worker.log").write_text("runtime data\n", encoding="utf-8")
        (self.repo / "data" / "people.sqlite3").write_bytes(b"database")
        (self.repo / "my_us_proxies.txt").write_text("credentialed-proxy\n", encoding="utf-8")
        (self.repo / "TruePeopleSearch_GUI.exe").write_bytes(b"old alias")

        self._run_git("init", "--quiet")
        self._run_git("add", "--all")
        commit_env = os.environ.copy()
        commit_env.update(
            {
                "GIT_AUTHOR_NAME": "Release Test",
                "GIT_AUTHOR_EMAIL": "release-test@example.invalid",
                "GIT_COMMITTER_NAME": "Release Test",
                "GIT_COMMITTER_EMAIL": "release-test@example.invalid",
                "GIT_AUTHOR_DATE": "2026-09-28T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2026-09-28T00:00:00+00:00",
            }
        )
        self._run_git("commit", "--quiet", "-m", "fixture", env=commit_env)
        self.exe_dir.mkdir()
        manifest_files = []
        for filename in packager.REQUIRED_WINDOWS_EXECUTABLES:
            executable = self.exe_dir / filename
            executable.write_bytes(_amd64_pe_fixture())
            manifest_files.append(
                {
                    "name": filename,
                    "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                    "size": executable.stat().st_size,
                }
            )
        (self.exe_dir / packager.BINARY_MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "schema_version": packager.BINARY_MANIFEST_SCHEMA_VERSION,
                    "source_commit": self._run_git("rev-parse", "HEAD"),
                    "target": "windows-amd64",
                    "files": manifest_files,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _manifest(self, archive_path: Path) -> tuple[zipfile.ZipFile, dict]:
        archive = zipfile.ZipFile(archive_path)
        return archive, json.loads(archive.read(packager.MANIFEST_NAME).decode("utf-8"))

    def test_clean_release_is_allowlisted_hashed_and_deterministic(self) -> None:
        output_one = self.output_dir / "client-one.zip"
        output_two = self.output_dir / "client-two.zip"
        first = packager.create_release(repo_root=self.repo, output=output_one, exe_dir=self.exe_dir)
        second = packager.create_release(repo_root=self.repo, output=output_two, exe_dir=self.exe_dir)

        self.assertEqual(output_one.read_bytes(), output_two.read_bytes())
        self.assertEqual(first.archive_sha256, second.archive_sha256)
        self.assertEqual(first.checksum_path.read_text(encoding="utf-8"), f"{first.archive_sha256} *client-one.zip\n")
        self.assertEqual(hashlib.sha256(output_one.read_bytes()).hexdigest(), first.archive_sha256)
        self.assertEqual(
            first.extractor_checksum_path.read_text(encoding="utf-8"),
            f"{hashlib.sha256(first.extractor_path.read_bytes()).hexdigest()} *client-one-extract.ps1\n",
        )
        self.assertEqual(
            first.extractor_path.read_bytes(),
            (self.repo / "deploy/windows/Expand-Release.ps1").read_bytes(),
        )

        archive, manifest = self._manifest(output_one)
        self.addCleanup(archive.close)
        names = archive.namelist()
        expected = (
            set(packager.RUNTIME_SOURCE_ALLOWLIST)
            | set(packager.REQUIRED_WINDOWS_EXECUTABLES)
            | {packager.BINARY_MANIFEST_NAME}
        )
        self.assertEqual(set(names), expected | {packager.MANIFEST_NAME})
        self.assertEqual(manifest["version"], "9.8.7")
        self.assertEqual(manifest["commit"], self._run_git("rev-parse", "HEAD"))
        self.assertFalse(manifest["source_dirty"])
        self.assertEqual([entry["path"] for entry in manifest["release_files"]], sorted(expected))
        for entry in manifest["release_files"]:
            self.assertEqual(entry["size"], len(archive.read(entry["path"])))
            self.assertEqual(entry["sha256"], hashlib.sha256(archive.read(entry["path"])).hexdigest())

        forbidden_prefixes = (".git/", ".venv/", "data/", "logs/", "proxy/", "db/")
        self.assertFalse(any(name.startswith(forbidden_prefixes) for name in names))
        self.assertNotIn(".env", names)
        self.assertNotIn("my_us_proxies.txt", names)
        self.assertNotIn("TruePeopleSearch_GUI.exe", names)
        self.assertIn("start_client.bat", names)
        self.assertIn("deploy/windows/Verify-Release.ps1", names)
        self.assertNotIn("check_env.py", names)
        self.assertNotIn("Windows客户端使用说明.txt", names)

    def test_dirty_tree_is_rejected_unless_explicitly_allowed(self) -> None:
        (self.repo / "uncommitted-note.txt").write_text("dirty\n", encoding="utf-8")
        output = self.output_dir / "dirty.zip"
        with self.assertRaisesRegex(packager.ReleaseError, "dirty source tree"):
            packager.create_release(repo_root=self.repo, output=output, exe_dir=self.exe_dir)
        self.assertFalse(output.exists())

        result = packager.create_release(
            repo_root=self.repo, output=output, exe_dir=self.exe_dir, allow_dirty=True
        )
        archive, manifest = self._manifest(result.output)
        self.addCleanup(archive.close)
        self.assertTrue(manifest["source_dirty"])

    def test_allowlisted_source_with_credentials_is_rejected_before_archive(self) -> None:
        packager.scan_release_text(
            "deploy/.env.example", b"TPS_DB_PASSWORD=\nTPS_DB_NAME=people_search\n"
        )
        dashboard_source = self.repo / "tools" / "dashboard-app.js"
        legacy = bytes.fromhex("747073313233343536")
        leaked_sources = (
            b"const proxy = 'http://real-user:real-secret@proxy.provider.test:9000';\n",
            b"const oldDbPassword = '" + legacy + b"';\n",
        )
        for index, contents in enumerate(leaked_sources):
            with self.subTest(index=index):
                dashboard_source.write_bytes(contents)
                output = self.output_dir / f"secret-{index}.zip"
                with self.assertRaisesRegex(packager.ReleaseError, "forbidden"):
                    packager.create_release(
                        repo_root=self.repo, output=output, exe_dir=self.exe_dir,
                        allow_dirty=True,
                    )
                self.assertFalse(output.exists())

    def test_missing_reviewed_executable_fails_before_creating_archive(self) -> None:
        (self.exe_dir / packager.REQUIRED_WINDOWS_EXECUTABLES[0]).unlink()
        output = self.output_dir / "missing-exe.zip"
        with self.assertRaisesRegex(packager.ReleaseError, "required Windows executable is missing"):
            packager.create_release(repo_root=self.repo, output=output, exe_dir=self.exe_dir, allow_dirty=True)
        self.assertFalse(output.exists())

    def test_rejects_source_tree_output_and_stateful_allowlist_paths(self) -> None:
        with self.assertRaisesRegex(packager.ReleaseError, "outside the source tree"):
            packager.create_release(repo_root=self.repo, output=self.repo / "release.zip", exe_dir=self.exe_dir)
        for path in (".env", "data/people.db", "logs/dashboard.log", "proxy/config.json", "db/cache.sqlite"):
            with self.subTest(path=path):
                with self.assertRaises(packager.ReleaseError):
                    packager.validate_release_path(path)
        self.assertEqual(packager.validate_release_path("deploy/.env.example").as_posix(), "deploy/.env.example")

    def test_rejects_source_tree_launcher_artifacts(self) -> None:
        with self.assertRaisesRegex(packager.ReleaseError, "launcher directory must be outside the source tree"):
            packager.create_release(
                repo_root=self.repo,
                output=self.output_dir / "internal-launchers.zip",
                exe_dir=self.repo,
            )

    def test_rejects_mz_stub_and_non_amd64_pe_launchers(self) -> None:
        executable = self.exe_dir / packager.REQUIRED_WINDOWS_EXECUTABLES[0]
        for contents in (
            b"MZ\x90\x00not a PE image",
            _amd64_pe_fixture(machine=0x014C, optional_header_magic=0x10B),
        ):
            with self.subTest(contents=contents[:4].hex()):
                executable.write_bytes(contents)
                with self.assertRaisesRegex(packager.ReleaseError, "not a 64-bit AMD64 PE file"):
                    packager.create_release(
                        repo_root=self.repo,
                        output=self.output_dir / "invalid-launcher.zip",
                        exe_dir=self.exe_dir,
                    )

    def test_rejects_stale_or_unrelated_windows_launcher_manifest(self) -> None:
        manifest_path = self.exe_dir / packager.BINARY_MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source_commit"] = "0" * 40
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(packager.ReleaseError, "does not match the release source commit"):
            packager.create_release(
                repo_root=self.repo,
                output=self.output_dir / "stale-launchers.zip",
                exe_dir=self.exe_dir,
            )


if __name__ == "__main__":
    unittest.main()
