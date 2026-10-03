import hashlib
import gzip
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "scripts" / "package_windows_full_bundle.py"
SPEC = importlib.util.spec_from_file_location("package_windows_full_bundle", MODULE_PATH)
bundle = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = bundle
SPEC.loader.exec_module(bundle)


class WindowsFullBundleTests(unittest.TestCase):
    @staticmethod
    def _write_docker_archive(path, tag):
        layer_buffer = io.BytesIO()
        with tarfile.open(fileobj=layer_buffer, mode="w") as layer_archive:
            payload = b"synthetic-layer"
            payload_info = tarfile.TarInfo("fixture.txt")
            payload_info.size = len(payload)
            layer_archive.addfile(payload_info, io.BytesIO(payload))
        layer_bytes = layer_buffer.getvalue()
        compressed_layer = gzip.compress(layer_bytes, mtime=0)
        diff_id = "sha256:" + hashlib.sha256(layer_bytes).hexdigest()
        config = json.dumps({
            "os": "linux",
            "architecture": "amd64",
            "rootfs": {"type": "layers", "diff_ids": [diff_id]},
        }).encode()
        config_name = hashlib.sha256(config).hexdigest() + ".json"
        layer_name = "layers/synthetic-layer.tar.gz"
        manifest = json.dumps(
            [{"Config": config_name, "RepoTags": [tag], "Layers": [layer_name]}]
        ).encode()
        with tarfile.open(path, "w") as archive:
            for name, contents in (
                (config_name, config),
                ("manifest.json", manifest),
                (layer_name, compressed_layer),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(contents)
                archive.addfile(info, io.BytesIO(contents))

    def test_application_allowlist_is_complete_and_secret_free(self):
        files = bundle.build_application_files(ROOT)
        archive_paths = {item.archive_path for item in files}
        self.assertIn("app/tools/dashboard_api.py", archive_paths)
        self.assertIn("app/scripts/distributed_worker.py", archive_paths)
        self.assertIn("app/scripts/person_visibility.py", archive_paths)
        self.assertIn("app/deploy/windows-full/Start-Stack.ps1", archive_paths)
        self.assertNotIn("app/scripts/start_all.py", archive_paths)
        self.assertNotIn("app/tools/test_live_scrape.py", archive_paths)
        self.assertNotIn("app/tools/test_decodo_now.py", archive_paths)
        self.assertFalse(any("/data/" in path or path.endswith(".env") for path in archive_paths))

    def test_secret_scanner_allows_placeholders_and_rejects_credentials(self):
        bundle.scan_application_text(
            "placeholder.txt", b"http://username:password@gateway.example:9000"
        )
        bundle.scan_application_text(
            "empty.env", b"TPS_DB_PASSWORD=\nTPS_DB_NAME=people_search\n"
        )
        with self.assertRaises(bundle.BundleError):
            bundle.scan_application_text(
                "secret.txt", b"http://actual-user:actual-secret@proxy.provider.test:9000"
            )
        with self.assertRaises(bundle.BundleError):
            bundle.scan_application_text(
                "secret.py", b"PROXY_TUNNEL='http://credential.invalid'\n"
            )
        with self.assertRaises(bundle.BundleError):
            bundle.scan_application_text("binary.py", b"safe\0ignored")
        with self.assertRaises(bundle.BundleError):
            bundle.scan_application_text(
                "second.env",
                b"TPS_DB_PASSWORD=GENERATED_LOCALLY\nTPS_REDIS_PASSWORD=actualsecret\n",
            )
        with self.assertRaises(bundle.BundleError):
            bundle.scan_application_text(
                "host.txt", b"http://actual:secret@notexample.com:9000"
            )
        # Reconstruct the previously leaked value only in memory: neither the
        # packager nor this test should carry its plaintext bytes in source.
        legacy = bytes.fromhex("747073313233343536")
        with self.assertRaises(bundle.BundleError):
            bundle.scan_application_text("legacy.txt", b"legacy=" + legacy + b"\n")

    def test_clean_release_bytes_come_from_the_pinned_git_object(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            stage = Path(temporary) / "stage"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Bundle Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "bundle@example.invalid"], check=True)
            source = repo / "demo.txt"
            source.write_text("committed\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "demo.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
            commit = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
            ).strip()
            source.write_text("hidden worktree edit\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(repo), "update-index", "--assume-unchanged", "demo.txt"],
                check=True,
            )
            with mock.patch.object(bundle, "APP_ALLOWLIST", ("demo.txt",)):
                records = bundle.build_committed_application_files(repo, commit, stage)
            self.assertEqual(records[0].source_path.read_text(encoding="utf-8"), "committed\n")

    def test_vendor_manifest_requires_all_roles_and_exact_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vendor_root = root / "vendor"
            vendor_root.mkdir()
            stages = root / "stages"
            records = []
            definitions = (
                ("python/python-3.12.10-amd64.exe", "python-installer", True),
                ("docker/DockerDesktopInstaller.exe", "docker-desktop-installer", True),
                ("browser/chromium-1243.zip", "chromium-archive", False),
                ("images/mysql-8.4.11-linux-amd64.tar", "mysql-image", False),
                ("images/redis-7.4.8-alpine-linux-amd64.tar", "redis-image", False),
                ("wheelhouse/runtime-1.0-py3-none-any.whl", "python-wheel", False),
            )
            for index, (relative, role, signed) in enumerate(definitions):
                path = vendor_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if role == "chromium-archive":
                    with zipfile.ZipFile(path, "w") as archive:
                        archive.writestr("chromium-1243/INSTALLATION_COMPLETE", b"")
                        archive.writestr("chromium-1243/chrome-win64/chrome.exe", b"synthetic")
                        archive.writestr("chromium_headless_shell-1243/INSTALLATION_COMPLETE", b"")
                        archive.writestr(
                            "chromium_headless_shell-1243/chrome-headless-shell-win64/chrome-headless-shell.exe",
                            b"synthetic",
                        )
                elif role == "mysql-image":
                    self._write_docker_archive(
                        path, "tps-offline/mysql:8.4.11-amd64"
                    )
                elif role == "redis-image":
                    self._write_docker_archive(
                        path, "tps-offline/redis:7.4.8-alpine-amd64"
                    )
                else:
                    if role == "python-wheel":
                        with zipfile.ZipFile(path, "w") as archive:
                            archive.writestr(
                                "runtime-1.0.dist-info/WHEEL",
                                "Wheel-Version: 1.0\nTag: py3-none-any\n",
                            )
                            archive.writestr(
                                "runtime-1.0.dist-info/METADATA",
                                "Metadata-Version: 2.1\nName: runtime\nVersion: 1.0\n",
                            )
                    else:
                        path.write_bytes(f"asset-{index}".encode())
                contents = path.read_bytes()
                record = {
                    "path": relative,
                    "role": role,
                    "sha256": hashlib.sha256(contents).hexdigest(),
                    "size": len(contents),
                }
                if signed:
                    record["authenticode_thumbprint"] = "A" * 40
                if role == "docker-desktop-installer":
                    record["component_versions"] = {
                        "desktop_product_version": "4.92.0.240144",
                        "docker_cli_version": "29.8.0",
                        "compose_version": "5.5.1",
                        "engine_version": "29.8.0",
                    }
                records.append(record)
            manifest = vendor_root / "vendor-manifest.json"
            manifest.write_text(
                json.dumps(
                    {"schema_version": 1, "target": "windows-amd64", "files": records}
                ),
                encoding="utf-8",
            )
            with mock.patch.object(bundle, "EXPECTED_WHEEL_FILENAMES", {"runtime-1.0-py3-none-any.whl"}):
                files = bundle.load_vendor_files(vendor_root, manifest, stages / "valid")
            self.assertEqual(len(files), len(records) + 1)
            self.assertEqual(
                {item.role for item in files if item.role}, bundle.REQUIRED_VENDOR_ROLES
            )
            self.assertTrue(all(
                item.image_id and item.image_id.startswith("sha256:")
                for item in files if item.role in {"mysql-image", "redis-image"}
            ))
            docker_item = next(
                item for item in files if item.role == "docker-desktop-installer"
            )
            self.assertEqual(docker_item.component_versions["compose_version"], "5.5.1")
            original_docker = vendor_root / "docker" / "DockerDesktopInstaller.exe"
            original_docker_bytes = original_docker.read_bytes()
            original_docker.write_bytes(b"replaced after validation")
            self.assertEqual(docker_item.source_path.read_bytes(), original_docker_bytes)
            original_docker.write_bytes(original_docker_bytes)
            canonical_manifest = next(
                item for item in files if item.category == "vendor-manifest"
            )
            self.assertTrue(canonical_manifest.source_path.is_relative_to(stages / "valid"))
            self.assertEqual(
                set(json.loads(canonical_manifest.source_path.read_text(encoding="utf-8"))),
                {"schema_version", "target", "files"},
            )

            manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
            manifest_payload["comment"] = "unsupported release metadata"
            manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
            with self.assertRaises(bundle.BundleError):
                with mock.patch.object(bundle, "EXPECTED_WHEEL_FILENAMES", {"runtime-1.0-py3-none-any.whl"}):
                    bundle.load_vendor_files(vendor_root, manifest, stages / "bad-top-level")
            manifest_payload.pop("comment")
            manifest_payload["files"][0]["source_url"] = "https://token@example.invalid/asset"
            manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
            with self.assertRaises(bundle.BundleError):
                with mock.patch.object(bundle, "EXPECTED_WHEEL_FILENAMES", {"runtime-1.0-py3-none-any.whl"}):
                    bundle.load_vendor_files(vendor_root, manifest, stages / "bad-record")
            manifest_payload["files"][0].pop("source_url")
            manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")

            (vendor_root / definitions[-1][0]).write_bytes(b"tampered")
            with self.assertRaises(bundle.BundleError):
                with mock.patch.object(bundle, "EXPECTED_WHEEL_FILENAMES", {"runtime-1.0-py3-none-any.whl"}):
                    bundle.load_vendor_files(vendor_root, manifest, stages / "tampered")

    def test_vendor_manifest_rejects_duplicate_keys_and_noncanonical_location(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vendor_root = root / "vendor"
            vendor_root.mkdir()
            manifest = vendor_root / "vendor-manifest.json"
            manifest.write_text(
                '{"schema_version":1,"target":"windows-amd64",'
                '"target":"windows-amd64","files":[]}',
                encoding="utf-8",
            )
            with self.assertRaises(bundle.BundleError):
                bundle.load_vendor_files(vendor_root, manifest, root / "stage-duplicate")

            outside = root / "reviewed.json"
            outside.write_text('{}', encoding="utf-8")
            with self.assertRaises(bundle.BundleError):
                bundle.load_vendor_files(vendor_root, outside, root / "stage-outside")

    def test_docker_archive_requires_nonempty_verified_rootfs_layers(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mysql.tar"
            config = json.dumps({
                "os": "linux", "architecture": "amd64",
                "rootfs": {"type": "layers", "diff_ids": []},
            }).encode()
            config_name = hashlib.sha256(config).hexdigest() + ".json"
            manifest = json.dumps([{
                "Config": config_name,
                "RepoTags": ["tps-offline/mysql:8.4.11-amd64"],
                "Layers": [],
            }]).encode()
            with tarfile.open(path, "w") as archive:
                for name, contents in ((config_name, config), ("manifest.json", manifest)):
                    info = tarfile.TarInfo(name)
                    info.size = len(contents)
                    archive.addfile(info, io.BytesIO(contents))
            with self.assertRaises(bundle.BundleError):
                bundle._validate_docker_image_archive(path, "mysql-image")

    def test_chromium_archive_rejects_windows_alias_and_case_collisions(self):
        invalid_names = ("C:/extra", "safe/file:stream", "safe/CON.txt")
        for invalid in invalid_names:
            with self.subTest(path=invalid), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "chromium.zip"
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr(invalid, b"x")
                with self.assertRaises(bundle.BundleError):
                    bundle._validate_chromium_archive(path)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "chromium.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("safe/chrome.exe", b"one")
                archive.writestr("SAFE/CHROME.EXE", b"two")
            with self.assertRaises(bundle.BundleError):
                bundle._validate_chromium_archive(path)

    def test_chromium_required_executables_are_regular_nonempty_and_unencrypted(self):
        required = (
            "chromium-1243/INSTALLATION_COMPLETE",
            "chromium-1243/chrome-win64/chrome.exe",
            "chromium_headless_shell-1243/INSTALLATION_COMPLETE",
            "chromium_headless_shell-1243/chrome-headless-shell-win64/chrome-headless-shell.exe",
        )
        for mode in ("empty", "directory"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "chromium.zip"
                with zipfile.ZipFile(path, "w") as archive:
                    for name in required:
                        if name.endswith("chrome.exe") and mode == "empty":
                            archive.writestr(name, b"")
                        elif name.endswith("chrome.exe") and mode == "directory":
                            archive.writestr(name + "/", b"")
                        else:
                            archive.writestr(name, b"x" if name.endswith(".exe") else b"")
                with self.assertRaises(bundle.BundleError):
                    bundle._validate_chromium_archive(path)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "chromium.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for name in required:
                    archive.writestr(name, b"x" if name.endswith(".exe") else b"")
            payload = bytearray(path.read_bytes())
            local = payload.index(b"PK\x03\x04")
            central = payload.index(b"PK\x01\x02")
            payload[local + 6:local + 8] = (
                int.from_bytes(payload[local + 6:local + 8], "little") | 1
            ).to_bytes(2, "little")
            payload[central + 8:central + 10] = (
                int.from_bytes(payload[central + 8:central + 10], "little") | 1
            ).to_bytes(2, "little")
            path.write_bytes(payload)
            with self.assertRaises(bundle.BundleError):
                bundle._validate_chromium_archive(path)

    def test_zip_writer_handles_precompressed_offline_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "mysql.tar"
            source.write_bytes(b"synthetic-image-tar")
            item = bundle.BundleFile(
                archive_path="vendor/images/mysql.tar",
                source_path=source,
                sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                size=source.stat().st_size,
                category="vendor",
                role="mysql-image",
            )
            output = root / "bundle.zip"
            with zipfile.ZipFile(output, "w") as archive:
                bundle._write_record(archive, item, (2026, 1, 1, 0, 0, 0))
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.read(item.archive_path), source.read_bytes())
                self.assertEqual(
                    archive.getinfo(item.archive_path).compress_type, zipfile.ZIP_STORED
                )

    def test_compose_pins_mysql_redis_and_d_drive_bind_mounts(self):
        text = (ROOT / "deploy/windows-full/docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("image: tps-offline/mysql:8.4.11-amd64", text)
        self.assertIn("image: tps-offline/redis:7.4.8-alpine-amd64", text)
        self.assertEqual(text.count("platform: linux/amd64"), 2)
        self.assertEqual(text.count("pull_policy: never"), 2)
        self.assertIn("127.0.0.1:${TPS_DB_PORT:-3306}:3306", text)
        self.assertIn("127.0.0.1:${TPS_REDIS_PORT:-6379}:6379", text)
        self.assertIn("--requirepass", text)
        self.assertIn("TPS_REDIS_PASSWORD", text)
        self.assertIn("--protected-mode yes", text)
        self.assertIn("/data/mysql:/var/lib/mysql", text)
        self.assertIn("/data/redis:/data", text)
        self.assertNotIn("pingcap/tidb", text)
        self.assertNotIn("distributed_worker", text)

    def test_startup_is_explicitly_non_collecting_and_has_no_c_drive_fallback(self):
        start = (ROOT / "deploy/windows-full/Start-Stack.ps1").read_text(encoding="utf-8")
        common = (ROOT / "deploy/windows-full/Common.ps1").read_text(encoding="utf-8")
        initialize = (ROOT / "deploy/windows-full/Initialize-Runtime.ps1").read_text(
            encoding="utf-8"
        )
        install = (ROOT / "deploy/windows-full/Install-OfflineRuntime.ps1").read_text(
            encoding="utf-8"
        )
        docker_install = (ROOT / "deploy/windows-full/Install-DockerDesktop.ps1").read_text(
            encoding="utf-8"
        )
        stack_test = (ROOT / "deploy/windows-full/Test-Stack.ps1").read_text(encoding="utf-8")
        self.assertIn("D:\\TruePeopleSearch", start)
        self.assertIn("up -d mysql redis", start)
        self.assertNotIn("distributed_worker.py", start)
        self.assertNotIn("multi_worker_runner.py", start)
        self.assertNotIn("phone_discover.py", start)
        self.assertNotIn("EnableOperatorControls", start)
        self.assertIn("TPS_LOCAL_AUTH_REQUIRED = '1'", start)
        self.assertNotIn("PROXY_TUNNEL = 'http", start)
        self.assertIn("No C: fallback is permitted", initialize)
        self.assertIn("--no-index", install)
        self.assertIn("--only-binary=:all:", install)
        self.assertIn("Get-TpsVerifiedDockerImageIds", install)
        self.assertIn("offline Playwright/Patchright launch smoke", install)
        self.assertIn("if ($name -eq 'TPS_MYSQL_ROOT_PASSWORD')", common)
        self.assertIn("[switch]$IncludeServiceCredentials", common)
        self.assertIn("Clear-TpsServiceCredentialEnvironment", common)
        self.assertIn("SetEnvironmentVariable('TPS_MYSQL_ROOT_PASSWORD', $null, 'Process')", common)
        self.assertIn("'PLAYWRIGHT_NODEJS_PATH'", common)
        self.assertIn("'NODE_OPTIONS', 'NODE_PATH'", common)
        self.assertIn("schema_version = 2", common)
        self.assertIn("bundle_manifest_sha256", common)
        self.assertIn("@('DOCKER_HOST', 'DOCKER_CONTEXT')", common)
        self.assertIn("npipe:////./pipe/dockerDesktopLinuxEngine", common)
        self.assertIn("Docker\\Docker\\resources", common)
        self.assertIn("Docker\\cli-plugins\\docker-compose.exe", common)
        self.assertIn("desktop_product_version", common)
        self.assertIn("docker_cli_version", common)
        self.assertIn("compose_version", common)
        self.assertIn("engine_version", common)
        self.assertIn("Server.Version", common)
        self.assertIn("Get-TpsDockerTools $root", docker_install)
        self.assertNotRegex(common, r"&\s+docker\b")
        self.assertIn("Get-TpsNativeSystemToolPath", common)
        self.assertNotRegex(common, r"&\s+(?:icacls|wsl)(?:\.exe)?\b")
        self.assertIn("Assert-TpsBusinessStorageOnD", start)
        self.assertIn("Assert-TpsStorageDirectoryTree $dockerData", common)
        self.assertIn("-I -B -c", install)
        self.assertIn("pip --isolated install", install)
        self.assertIn("-IncludeServiceCredentials", start)
        self.assertIn("Clear-TpsServiceCredentialEnvironment", start)
        self.assertIn("-IncludeServiceCredentials", stack_test)
        self.assertIn("Protect-TpsSecretFile $recordPath", start)
        self.assertIn("$existing.process_start_utc", start)
        self.assertIn("GetFullPath($existingProcess.Path)", start)
        self.assertIn("Stop-Process -InputObject $candidate", start)
        self.assertIn("$launchFailure = $_", start)
        self.assertIn("dashboard release identity", stack_test)
        self.assertIn("$record.application_root", stack_test)


if __name__ == "__main__":
    unittest.main()
