#!/usr/bin/env bash
# ============================================================
# TruePeopleSearch 客户端一键安装配置向导 (Linux / macOS)
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "============================================================"
echo "  TruePeopleSearch 客户端一键安装向导"
echo "============================================================"
echo "  工作目录: ${ROOT_DIR}"

# 1. 检查 Python 环境
if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ 未检测到 Python 3，请先安装 Python 3.9+ 后重试。"
    exit 1
fi
echo "✅ Python 3 环境就绪: $(python3 --version)"

# 2. 创建并激活虚拟环境
if [ ! -d "${ROOT_DIR}/.venv" ]; then
    echo "[1/4] 正在创建 Python 虚拟环境 (.venv)..."
    python3 -m venv "${ROOT_DIR}/.venv"
fi
VENV_PYTHON="${ROOT_DIR}/.venv/bin/python3"
VENV_PIP="${ROOT_DIR}/.venv/bin/pip"

# 3. 安装依赖包
echo "[2/4] 正在安装与同步依赖包..."
"${VENV_PIP}" install -r "${ROOT_DIR}/requirements.txt" --quiet

# 4. 检查 Docker 及启动 TiDB + Redis
echo "[3/4] 检查本地数据库 (TiDB) 与消息队列 (Redis)..."
if command -v docker >/dev/null 2>&1; then
    echo "  发现 Docker 环境，正在启动容器集群..."
    docker compose -f "${SCRIPT_DIR}/docker-compose.yml" up -d
    echo "  等待数据库与 Redis 容器初始化就绪..."
    sleep 4
else
    echo "  ⚠️ 未检测到 Docker，请确保您已在本机或局域网独立启动了 TiDB(4000端口)/MySQL 及 Redis(6379端口)。"
fi

# 5. 执行数据库建表与验证
echo "[4/4] 执行数据库表结构自动化初始化..."
"${VENV_PYTHON}" "${SCRIPT_DIR}/init_db.py"

echo "============================================================"
echo "🎉 客户端安装初始化圆满完成！"
echo "启动命令: bash supervisor.sh start"
echo "面板访问: http://127.0.0.1:5001"
echo "============================================================"
