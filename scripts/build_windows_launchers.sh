#!/usr/bin/env bash
# Build the three Windows launchers into an external, empty artifact directory.
# The source tree must be clean. This script neither packages nor deploys the
# result, and it leaves the checkout's existing executable artifacts untouched.
set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "Usage: $0 <external-empty-output-directory>" >&2
    exit 2
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
OUT_DIR="$1"
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd -P)"

case "$OUT_DIR" in
    "$ROOT_DIR"|"$ROOT_DIR"/*)
        echo "Refusing to place release launchers inside the source tree." >&2
        exit 2
        ;;
esac

if [ -n "$(find "$OUT_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
    echo "Output directory must be empty; existing artifacts will not be replaced." >&2
    exit 2
fi

if [ -n "$(git -C "$ROOT_DIR" status --porcelain=v1 --untracked-files=all)" ]; then
    echo "Refusing to build release launchers from a dirty source tree." >&2
    exit 2
fi

command -v go >/dev/null 2>&1 || { echo "Go is required to build Windows launchers." >&2; exit 2; }

GOOS=windows GOARCH=amd64 go build -trimpath -buildvcs=false -ldflags="-s -w" \
    -o "$OUT_DIR/TruePeopleSearch.exe" \
    "$ROOT_DIR/launcher/main.go" "$ROOT_DIR/launcher/process_windows.go"
GOOS=windows GOARCH=amd64 go build -trimpath -buildvcs=false -ldflags="-s -w -H windowsgui" \
    -o "$OUT_DIR/TruePeopleSearch_后台无窗启动.exe" \
    "$ROOT_DIR/launcher/main.go" "$ROOT_DIR/launcher/process_windows.go"
GOOS=windows GOARCH=amd64 go build -trimpath -buildvcs=false -ldflags="-s -w" \
    -o "$OUT_DIR/TruePeopleSearch_停止.exe" \
    "$ROOT_DIR/launcher/stop.go"

"$ROOT_DIR/scripts/write_windows_launcher_manifest.py" --repo-root "$ROOT_DIR" --exe-dir "$OUT_DIR"
echo "Windows launchers built in: $OUT_DIR"
