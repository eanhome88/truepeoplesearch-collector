#!/usr/bin/env bash
# ============================================================
# TruePeopleSearch 客户端环境一键体检与安装向导 (Linux / macOS)
# ============================================================
set -u

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE_DIR"

PYTHON="python3"
if [ -x "$BASE_DIR/.venv/bin/python3" ]; then
    PYTHON="$BASE_DIR/.venv/bin/python3"
fi

"$PYTHON" "$BASE_DIR/check_env.py"
