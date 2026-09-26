#!/bin/bash
# TruePeopleSearch 生产级服务守护与自愈脚本
set -u

BASE_DIR="$(cd "$(dirname "$0")" && pwd)" || exit 1
cd "$BASE_DIR" || exit 1

if [ -x "$BASE_DIR/.venv/bin/python3" ]; then
    PYTHON="$BASE_DIR/.venv/bin/python3"
elif [ -n "${TPS_PYTHON:-}" ]; then
    PYTHON="$TPS_PYTHON"
else
    PYTHON="python3"
fi

export PYTHONPATH="$BASE_DIR/scripts:$BASE_DIR/tools:${PYTHONPATH:-}"

case "${1:-status}" in
    start)
        shift
        echo "正在后台启动 TPS Supervisor 守护进程..."
        nohup "$PYTHON" "$BASE_DIR/scripts/tps_supervisor.py" start "$@" > "$BASE_DIR/data/logs/supervisor.log" 2>&1 &
        sleep 1
        "$PYTHON" "$BASE_DIR/scripts/tps_supervisor.py" status
        ;;
    stop)
        "$PYTHON" "$BASE_DIR/scripts/tps_supervisor.py" stop
        ;;
    status)
        "$PYTHON" "$BASE_DIR/scripts/tps_supervisor.py" status
        ;;
    restart)
        shift
        "$PYTHON" "$BASE_DIR/scripts/tps_supervisor.py" stop
        sleep 1
        nohup "$PYTHON" "$BASE_DIR/scripts/tps_supervisor.py" start "$@" > "$BASE_DIR/data/logs/supervisor.log" 2>&1 &
        sleep 1
        "$PYTHON" "$BASE_DIR/scripts/tps_supervisor.py" status
        ;;
    foreground)
        shift
        exec "$PYTHON" "$BASE_DIR/scripts/tps_supervisor.py" start "$@"
        ;;
    *)
        echo "用法: $0 {start|stop|restart|status|foreground} [--concurrency N] [--no-dashboard]"
        exit 1
        ;;
esac
