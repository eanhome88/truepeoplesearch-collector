"""Static contract tests for Windows-only release verification behavior.

PowerShell is not available in every development environment.  These tests
ensure the optional publisher pin is retained and forwarded; execution of the
Authenticode APIs remains a required Windows acceptance step.
"""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXTRACTOR = ROOT / "deploy" / "windows" / "Expand-Release.ps1"
VERIFIER = ROOT / "deploy" / "windows" / "Verify-Release.ps1"
READINESS = ROOT / "deploy" / "windows" / "Test-HostReadiness.ps1"
RELEASE_DOC = ROOT / "deploy" / "windows" / "README-RELEASE.md"


class WindowsReleasePowerShellContractTests(unittest.TestCase):
    def test_extractor_forwards_optional_authenticode_thumbprint(self) -> None:
        source = EXTRACTOR.read_text(encoding="utf-8")
        self.assertIn("[string]$ExpectedPackageSha256", source)
        self.assertIn("[ValidatePattern('^[a-fA-F0-9]{64}$')]", source)
        self.assertIn("package SHA-256 does not match the trusted expected value", source)
        self.assertIn("[string]$ExpectedAuthenticodeThumbprint", source)
        self.assertIn("$PSBoundParameters.ContainsKey('ExpectedAuthenticodeThumbprint')", source)
        self.assertIn("$verificationArguments['ExpectedAuthenticodeThumbprint']", source)
        self.assertIn("& $verifier @verificationArguments", source)

    def test_verifier_pins_all_required_launcher_publishers(self) -> None:
        source = VERIFIER.read_text(encoding="utf-8")
        self.assertIn("Get-AuthenticodeSignature -LiteralPath $FilePath -ErrorAction Stop", source)
        self.assertIn("expected Authenticode thumbprint must be exactly 40 hexadecimal characters", source)
        for launcher in (
            "TruePeopleSearch.exe",
            "TruePeopleSearch_后台无窗启动.exe",
            "TruePeopleSearch_停止.exe",
        ):
            with self.subTest(launcher=launcher):
                self.assertIn(f"'{launcher}'", source)
        self.assertIn("SHA-256 integrity does not establish publisher identity", source)

    def test_runtime_reverification_accepts_only_expected_local_state(self) -> None:
        source = VERIFIER.read_text(encoding="utf-8")
        self.assertIn("$relativePath -eq '.env'", source)
        self.assertIn("$relativeParts -contains '__pycache__'", source)
        self.assertIn("release contains a reparse point", source)

    def test_release_document_distinguishes_integrity_and_identity(self) -> None:
        source = RELEASE_DOC.read_text(encoding="utf-8")
        self.assertIn("完整性与发布者身份是两项独立检查", source)
        self.assertIn("-ExpectedAuthenticodeThumbprint", source)
        self.assertIn("不代表发布者身份已认证", source)

    def test_readiness_reads_only_bounded_regular_release_env_port_overrides(self) -> None:
        source = READINESS.read_text(encoding="utf-8")
        self.assertIn("$envPath = Join-Path $releaseRoot '.env'", source)
        self.assertIn("$MaxEnvironmentFileBytes = 1024 * 1024", source)
        self.assertIn("[IO.FileAttributes]::ReparsePoint", source)
        self.assertIn("Get-Content -LiteralPath $envPath -Raw -Encoding UTF8", source)
        self.assertIn("^(TPS_DB_PORT|TPS_REDIS_PORT)", source)
        self.assertIn("Convert-ApprovedPort", source)
        self.assertIn("$parsed -lt 1 -or $parsed -gt 65535", source)
        self.assertIn("duplicate approved port settings", source)
        self.assertIn("$PSBoundParameters.ContainsKey('DatabasePort')", source)
        self.assertIn("$PSBoundParameters.ContainsKey('RedisPort')", source)
        self.assertIn("$databasePort = 4000", source)
        self.assertIn("$redisPort = 6379", source)
        self.assertNotIn("Invoke-Expression", source)
        self.assertNotIn("127.0.0.1:$($service.Port)", source)


if __name__ == "__main__":
    unittest.main()
