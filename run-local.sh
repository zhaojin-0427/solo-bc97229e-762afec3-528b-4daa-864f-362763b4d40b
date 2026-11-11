#!/usr/bin/env bash
# 本地（非容器）运行入口：需已安装 app/requirements.txt 中的依赖。
set -euo pipefail
cd "$(dirname "$0")/app"
exec python server.py
