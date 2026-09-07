#!/usr/bin/env bash
set -euo pipefail

# ------------------------------------------------------------
# InkTime Pro 容器入口：
#   仅启动 server.py（常驻 HTTP 服务，前台）。
#   定时任务由 server 内置的 Scheduler（croniter 后台线程）承载，
#   设置页可改、即时生效，无需容器内 cron（非 root 也跑不了 cron）。
# 手动分析照片： docker exec <容器> python analyze_photos.py
# 手动渲染一次： docker exec <容器> python render_daily_photo.py
# ------------------------------------------------------------

LOG_DIR="${INKTIME_LOG_DIR:-/app/logs}"
mkdir -p "$LOG_DIR"
touch "$LOG_DIR/server.log"

echo "[entrypoint] 启动 server.py @ ${FLASK_HOST:-0.0.0.0}:${FLASK_PORT:-8765}" >> "$LOG_DIR/server.log"

# ---- 前台跑 server.py（常驻） ----
exec python server.py
