#!/usr/bin/env bash
# ============================================================
# TruePeopleSearch Windows 原生 EXE 客户端编译脚本
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

GO_CMD="go"
if [ -x "/Users/wangqi/go-sdk/usr/local/go/bin/go" ]; then
    GO_CMD="/Users/wangqi/go-sdk/usr/local/go/bin/go"
fi

echo "============================================================"
echo "  正在交叉编译 Windows 原生 PE 64位 EXE 客户端..."
echo "============================================================"

# 1. 编译带控制台主启动器 (支持实时输出和安全退出)
GOOS=windows GOARCH=amd64 "${GO_CMD}" build -ldflags="-s -w" -o TruePeopleSearch.exe launcher/main.go launcher/process_windows.go
echo "  ✅ TruePeopleSearch.exe 编译完成"

# 2. 编译纯后台无黑窗版本 (GUI子系统，双击静默启动并打开浏览器)
GOOS=windows GOARCH=amd64 "${GO_CMD}" build -ldflags="-s -w -H windowsgui" -o TruePeopleSearch_后台无窗启动.exe launcher/main.go launcher/process_windows.go
echo "  ✅ TruePeopleSearch_后台无窗启动.exe 编译完成"

# 3. 编译一键停止器
GOOS=windows GOARCH=amd64 "${GO_CMD}" build -ldflags="-s -w" -o TruePeopleSearch_停止.exe launcher/stop.go
echo "  ✅ TruePeopleSearch_停止.exe 编译完成"

echo "============================================================"
echo "🎉 全部 EXE 客户端编译完毕，已放置于根目录下！"
echo "============================================================"
