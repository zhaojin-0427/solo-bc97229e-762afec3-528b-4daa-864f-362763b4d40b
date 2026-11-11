#!/usr/bin/env bash
# 本地（非容器）运行入口：需已安装 app/requirements.txt 中的依赖。
# 同时启动唯一执行器进程（worker.py）与 Web 服务（server.py）。
set -euo pipefail
cd "$(dirname "$0")/app"

python worker.py &
WORKER_PID=$!
trap 'kill "$WORKER_PID" 2>/dev/null || true' EXIT

exec python server.py
