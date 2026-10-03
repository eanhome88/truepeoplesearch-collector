#!/bin/bash
# 第二个网关副本 (8089) 一键启动脚本
# 用法: bash deploy/run_replica_8089.sh
# 说明: 主网关跑在 8088 (勿动), 本脚本只起 8089 副本, 1 worker + UNBLOCKER_MAX_BROWSERS=1 (本机内存有限)
set -e
cd /Users/wangqi/Desktop/22
set -a
# shellcheck disable=SC1091
source .env
set +a
export UNBLOCKER_MAX_BROWSERS=1
exec ./.venv/bin/gunicorn -w 1 --threads 4 --timeout 600 --bind 127.0.0.1:8089 --chdir tools local_unblocker_api:app
