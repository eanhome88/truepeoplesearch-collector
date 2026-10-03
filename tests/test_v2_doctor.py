"""诊断只自动处理已知环境问题，不改程序。"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "v2"))

import doctor


class DoctorTest(unittest.TestCase):
    def test_repairs_missing_env_and_broken_pid_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "runtime.env.example").write_text("CLOUDBYPASS_APIKEY=\n", encoding="utf-8")
            (root / "data").mkdir()
            (root / "data" / "client_pids.json").write_text("not-json", encoding="utf-8")
            notes = "\n".join(doctor.run_doctor(root))
            self.assertTrue((root / ".env").is_file())
            self.assertIn("{}", (root / "data" / "client_pids.json").read_text(encoding="utf-8"))
            self.assertIn("CLOUDBYPASS_APIKEY", notes)
            self.assertIn("install.bat", notes)

    def test_explain_without_key_is_empty(self):
        self.assertEqual(doctor.explain_log("Traceback", {}), "")


if __name__ == "__main__":
    unittest.main()
