#!/bin/bash
# Local dashboard launcher. Never creates containers, pulls images or runs SQL migrations.
set -u

BASE_DIR="$(cd "$(dirname "$0")" && pwd)" || exit 1
cd "$BASE_DIR" || exit 1
if [ -n "${TPS_PYTHON:-}" ]; then
    PYTHON="$TPS_PYTHON"
elif [ -x "$BASE_DIR/.venv/bin/python3" ]; then
    PYTHON="$BASE_DIR/.venv/bin/python3"
else
    PYTHON="python3"
fi
# Read-only checks must not create Python bytecode beside application files.
export PYTHONDONTWRITEBYTECODE=1
CHECK_ONLY=0
WAIT_SECONDS="${TPS_STARTUP_TIMEOUT:-30}"

usage() {
    echo "用法: $0 [--check] [--timeout 秒]"
    echo "  --check   只读检查依赖、数据库结构、Redis 和现有容器元数据，不启动任何服务"
    echo "  默认      仅在必要时恢复本机已有容器，然后前台启动本地面板"
    echo "  不创建/替换容器、不下载镜像、不安装依赖、不建库或迁移已有数据。"
}

fail() { echo "错误: $*" >&2; exit 1; }
while [ "$#" -gt 0 ]; do
    case "$1" in
        --check) CHECK_ONLY=1; shift ;;
        --timeout)
            [ "$#" -ge 2 ] || fail "--timeout 缺少秒数"
            WAIT_SECONDS="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; fail "未知参数: $1" ;;
    esac
done
case "$WAIT_SECONDS" in ''|*[!0-9]*) fail "等待时间必须为 1 到 300 秒" ;; esac
[ "${#WAIT_SECONDS}" -le 3 ] || fail "等待时间必须为 1 到 300 秒"
WAIT_SECONDS=$((10#$WAIT_SECONDS))
[ "$WAIT_SECONDS" -ge 1 ] && [ "$WAIT_SECONDS" -le 300 ] || fail "等待时间必须为 1 到 300 秒"
command -v "$PYTHON" >/dev/null 2>&1 || fail "找不到 Python；请设置 TPS_PYTHON 指向已安装的解释器。"
"$PYTHON" -c 'import flask, mysql.connector, redis, psutil' || fail "面板依赖缺失；请先安装 flask、mysql-connector-python、redis、psutil。"

docker_cli() {
    "$PYTHON" -c '# local-startup-docker-wrapper
import re
import subprocess
import sys

def safe_output(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@]+@", r"\1[redacted]@", value or "")

try:
    result = subprocess.run(["docker", *sys.argv[1:]], capture_output=True, timeout=10)
except subprocess.TimeoutExpired as exc:
    print(safe_output(exc.stderr), end="", file=sys.stderr)
    print("Docker 命令超时（10 秒）；状态尚未确认，请检查后再重试。", file=sys.stderr)
    sys.exit(124)
except OSError as exc:
    print(f"Docker 命令不可用: {type(exc).__name__}", file=sys.stderr)
    sys.exit(1)
print(safe_output(result.stdout), end="")
print(safe_output(result.stderr), end="", file=sys.stderr)
sys.exit(result.returncode)
' "$@"
}

probe_service() {
    "$PYTHON" - "$BASE_DIR/tools" "$1" <<'PY'
import os
import sys
import threading

def timed_out():
    print("服务探测超时（4 秒）。", file=sys.stderr, flush=True)
    os._exit(124)

timer = threading.Timer(4, timed_out)
timer.daemon = True
timer.start()
sys.path.insert(0, sys.argv[1])
service = sys.argv[2]
try:
    from dashboard_api import check_local_database_ready, _redis_ready
    if service == "tidb":
        check_local_database_ready()
    elif not _redis_ready():
        raise RuntimeError("Redis readiness failed")
except Exception as exc:
    # Do not print connection strings, credentials or database content.
    code = getattr(exc, "errno", None)
    if getattr(exc, "code", None) == "mysql_connector_9_2_required":
        print("数据库就绪检查需要 mysql-connector-python >= 9.2；请更新面板依赖。", file=sys.stderr)
    else:
        print(f"{service} 未就绪: {type(exc).__name__}" + (f" (错误码 {code})" if code else "")
              + "；请检查连接配置、权限及已有数据库结构。", file=sys.stderr)
    sys.exit(1)
finally:
    timer.cancel()
PY
}

inspect_container() {
    local name="$1" image mounts
    command -v docker >/dev/null 2>&1 || { echo "  $name: 未安装 Docker，容器持久化元数据不可核验。"; return; }
    if ! image=$(docker_cli container inspect --format '{{.Config.Image}}' "$name" 2>&1); then
        echo "  $name: 无法检查同名容器；当前服务也可能由其他方式运行。"
        echo "$image" >&2
        return
    fi
    echo "  同名容器 $name 的镜像: ${image}（不代表当前连接一定由此容器提供）"
    case "$image" in
        *@sha256:*) ;;
        *:latest) echo "  提醒: latest 未锁定版本；升级前须备份并明确目标版本。" ;;
        *:*) ;;
        *) echo "  提醒: 未显式锁定镜像标签；升级前须备份并明确目标版本。" ;;
    esac
    if ! mounts=$(docker_cli container inspect --format '{{range .Mounts}}{{printf "%s | %s | %s\n" .Type .Name .Destination}}{{end}}' "$name" 2>&1); then
        echo "$mounts" >&2
        echo "  提醒: 无法核验挂载，升级前先确认数据位置与备份。"
    elif [ -z "$mounts" ]; then
        echo "  提醒: 未发现挂载；尚未验证数据持久化，不表示已发生数据丢失。"
    else
        echo "  挂载类型 | 卷名 | 容器目标目录:"
        echo "$mounts"
        echo "  仅确认挂载存在；仍须核对数据库实际存储位置、命名卷归属及备份恢复。"
    fi
}

ensure_service() {
    local name="$1" host="$2" running deadline docker_endpoint
    echo "检查 $name..."
    if probe_service "$name"; then
        echo "  $name 检查通过。"
        return 0
    fi
    [ "$CHECK_ONLY" -eq 0 ] || return 1
    case "$host" in 127.0.0.1|localhost|::1) ;; *)
        echo "错误: $name 使用非本机地址，不操作本机 Docker。" >&2; return 1 ;;
    esac
    command -v docker >/dev/null 2>&1 || { echo "错误: $name 未就绪且未找到 Docker。" >&2; return 1; }
    # A local DB address does not imply that Docker's active context is local.
    if [ -n "${DOCKER_HOST:-}" ] && [ -z "${DOCKER_CONTEXT:-}" ]; then
        docker_endpoint="$DOCKER_HOST"
    elif ! docker_endpoint=$(docker_cli context inspect --format '{{.Endpoints.docker.Host}}'); then
        echo "错误: 无法核验 Docker 上下文；不自动恢复容器。" >&2
        return 1
    fi
    case "$docker_endpoint" in unix://*|npipe://*) ;; *)
        echo "错误: Docker 使用非本机套接字上下文；不自动恢复容器。" >&2; return 1 ;;
    esac
    if ! running=$(docker_cli container inspect --format '{{.State.Running}}' "$name"); then
        echo "错误: 无法确认现有 $name 容器；不会创建容器或下载镜像。请先配置已有服务。" >&2
        return 1
    fi
    case "$running" in
        false)
            echo "  恢复已有 $name 容器..."
            docker_cli start "$name" || { echo "错误: $name 容器恢复失败。" >&2; return 1; } ;;
        true) echo "  现有 $name 容器正在运行，等待就绪..." ;;
        *) echo "错误: 无法识别 $name 容器状态。" >&2; return 1 ;;
    esac
    deadline=$((SECONDS + WAIT_SECONDS))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if probe_service "$name"; then
            echo "  $name 检查通过。"
            return 0
        fi
        sleep 1
    done
    echo "错误: 等待 $name 就绪超时（${WAIT_SECONDS} 秒；单次探测最多额外 4 秒）。不会自动建库或迁移。" >&2
    return 1
}

echo "本地面板环境检查"
FAILED=0
ensure_service tidb "${TPS_DB_HOST:-127.0.0.1}" || FAILED=1
ensure_service redis "${TPS_REDIS_HOST:-127.0.0.1}" || FAILED=1
echo "现有容器版本与挂载信息（只读）:"
inspect_container tidb
inspect_container redis
[ "$FAILED" -eq 0 ] || fail "依赖服务尚未就绪；面板未启动。"

if [ "$CHECK_ONLY" -eq 1 ]; then
    echo "只读检查通过；数据库结构与 Redis 可用。未启动面板或其他服务。"
    exit 0
fi
echo "正在前台启动面板；实际地址以面板输出为准，服务是否就绪请检查 /api/ready。"
exec "$PYTHON" "$BASE_DIR/tools/dashboard_api.py" --host "${TPS_DASHBOARD_HOST:-127.0.0.1}" --port "${TPS_DASHBOARD_PORT:-5001}"
