#!/usr/bin/env python3
"""Compatibility entry point for the verified Windows full-stack launcher.

The full-stack launcher starts local dependencies and a protected dashboard.
Collection requires a separate, reviewed operation.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


STACK_SCRIPT = Path(r"D:\TruePeopleSearch\app\deploy\windows-full\Start-Stack.ps1")


def main() -> int:
    if os.name != "nt":
        print("The Windows full-stack launcher can only run on Windows.")
        return 2
    if not STACK_SCRIPT.is_file():
        print("Verified Windows bundle is missing at D:\\TruePeopleSearch.")
        return 2
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(STACK_SCRIPT)],
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
