"""Tests for launcher provenance recording; no launchers are executed."""

from __future__ import annotations

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


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "write_windows_launcher_manifest.py"
SPEC = importlib.util.spec_from_file_location("write_windows_launcher_manifest", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
writer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = writer
SPEC.loader.exec_module(writer)


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
class WindowsLauncherManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.repo = root / "source"
        self.exe_dir = root / "launchers"
        self.repo.mkdir()
        self.exe_dir.mkdir()
        (self.repo / "release.txt").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "init", "--quiet"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "release.txt"], check=True)
        env = os.environ.copy()
        env.update({
            "GIT_AUTHOR_NAME": "Release Test",
            "GIT_AUTHOR_EMAIL": "release-test@example.invalid",
            "GIT_COMMITTER_NAME": "Release Test",
            "GIT_COMMITTER_EMAIL": "release-test@example.invalid",
        })
        subprocess.run(["git", "-C", str(self.repo), "commit", "--quiet", "-m", "fixture"], check=True, env=env)
        for name in writer.EXECUTABLES:
            (self.exe_dir / name).write_bytes(_amd64_pe_fixture())

    def test_clean_commit_records_exact_windows_launcher_hashes(self):
        path = writer.create_manifest(self.repo, self.exe_dir)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], writer.SCHEMA_VERSION)
        self.assertEqual(manifest["target"], "windows-amd64")
        self.assertEqual(
            manifest["source_commit"],
            subprocess.run(
                ["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, text=True, stdout=subprocess.PIPE
            ).stdout.strip(),
        )
        self.assertEqual([entry["name"] for entry in manifest["files"]], list(writer.EXECUTABLES))

    def test_dirty_source_is_rejected_before_writing_a_manifest(self):
        (self.repo / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(writer.ManifestError, "dirty source tree"):
            writer.create_manifest(self.repo, self.exe_dir)
        self.assertFalse((self.exe_dir / writer.MANIFEST_NAME).exists())

    def test_rejects_source_tree_as_launcher_artifact_directory(self):
        with self.assertRaisesRegex(writer.ManifestError, "launcher directory must be outside the source tree"):
            writer.create_manifest(self.repo, self.repo)

    def test_rejects_mz_stub_and_non_amd64_pe_launchers(self):
        executable = self.exe_dir / writer.EXECUTABLES[0]
        for contents in (
            b"MZ\x90\x00not a PE image",
            _amd64_pe_fixture(machine=0x014C, optional_header_magic=0x10B),
        ):
            with self.subTest(contents=contents[:4].hex()):
                executable.write_bytes(contents)
                with self.assertRaisesRegex(writer.ManifestError, "not a 64-bit AMD64 PE file"):
                    writer.create_manifest(self.repo, self.exe_dir)
                self.assertFalse((self.exe_dir / writer.MANIFEST_NAME).exists())


if __name__ == "__main__":
    unittest.main()
