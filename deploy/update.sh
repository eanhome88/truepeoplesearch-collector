#!/usr/bin/env bash
# ============================================================
# TruePeopleSearch 客户端一键升级脚本 (Linux / macOS)
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "============================================================"
echo "  TruePeopleSearch 正在检测与拉取最新系统更新..."
echo "============================================================"

VENV_PYTHON="${ROOT_DIR}/.venv/bin/python3"
if [ ! -f "${VENV_PYTHON}" ]; then
    VENV_PYTHON="python3"
fi

"${VENV_PYTHON}" -c "
import sys
sys.path.insert(0, '${ROOT_DIR}/scripts')
import tps_version
print('正在执行升级...')
res = tps_version.execute_system_update(force_stash=True)
for line in res.get('logs', []):
    print(line)
if not res.get('ok'):
    print('升级失败:', res.get('error'))
    sys.exit(1)
"

echo "============================================================"
echo "🎉 升级完成！请刷新浏览器控制台: http://127.0.0.1:5001"
echo "============================================================"
