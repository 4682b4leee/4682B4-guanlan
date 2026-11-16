#!/usr/bin/env bash
# ============================================================
# 观澜 · 一键启动（macOS / Linux）
# 做四件事：建虚拟环境 → 装依赖 → 下载浏览器内核 → 启动服务
# 重复执行也安全：已装好的步骤会自动跳过
# ============================================================
set -e

# 切到脚本所在目录，保证从任何位置执行都能找到 server/server.py
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

# ---- 1. 检查 Python 版本（需要 3.9+）----
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "✗ 没找到 python3，请先安装 Python 3.9 或更高版本"
  exit 1
fi

VER=$("$PY" -c 'import sys; print("%d%d" % sys.version_info[:2])')
if [ "$VER" -lt 39 ]; then
  echo "✗ 当前 Python 版本过低（需要 3.9+），请升级后再试"
  "$PY" --version
  exit 1
fi

# ---- 2. 虚拟环境 ----
if [ ! -d ".venv" ]; then
  echo "→ 创建虚拟环境 .venv"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# ---- 3. 依赖 ----
echo "→ 安装 Python 依赖"
pip install -q --upgrade pip
pip install -q -r requirements.txt

# 浏览器内核体积大，用标记文件避免每次都检查
if [ ! -f ".venv/.chromium-installed" ]; then
  echo "→ 下载 Chromium（约 150MB，只需一次）"
  playwright install chromium
  touch .venv/.chromium-installed
fi

# ---- 4. 启动 ----
echo "→ 启动观澜：http://127.0.0.1:8765/"
echo "   按 Ctrl+C 停止"

python server/server.py &
SERVER_PID=$!
# 退出时顺手把服务一起关掉，避免留下后台进程
trap 'kill $SERVER_PID 2>/dev/null' EXIT INT TERM

# 等服务真正起来再开浏览器，否则会打开一个打不开的页面
for _ in $(seq 1 40); do
  if curl -s -o /dev/null "http://127.0.0.1:8765/index.html"; then
    # 服务器场景下不想自动弹浏览器，可以 NO_OPEN=1 ./start.sh
    if [ "${NO_OPEN:-0}" != "1" ]; then
      if command -v open >/dev/null 2>&1; then
        open "http://127.0.0.1:8765/index.html"        # macOS
      elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "http://127.0.0.1:8765/index.html"    # Linux
      fi
    fi
    break
  fi
  sleep 0.5
done

wait $SERVER_PID
