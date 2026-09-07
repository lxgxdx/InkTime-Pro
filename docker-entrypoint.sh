#!/usr/bin/env bash
set -euo pipefail

# ------------------------------------------------------------
# InkTime 容器入口：
#   1) 若环境变量 RENDER_CRON 存在，注册为 cron 定时任务
#   2) 启动 server.py（常驻 HTTP 服务，前台）
# 手动分析照片： docker exec <容器> python analyze_photos.py
# 手动渲染一次：    docker exec <容器> python render_daily_photo.py
# ------------------------------------------------------------

LOG_DIR="${INKTIME_LOG_DIR:-/app/logs}"
mkdir -p "$LOG_DIR"
touch "$LOG_DIR/render.log" "$LOG_DIR/server.log"

# ---- 配置 cron 定时渲染 ----
# 默认：每天渲染一次（可用 RENDER_CRON 覆盖，如 "0 5 * * *"）
CRON_SCHEDULE="${RENDER_CRON:-5 4 * * *}"   # 默认每天 04:05

# 若 data 卷已有 web_settings.json（用户从设置页保存过），定时调度交给 server.py 的
# 后台 Scheduler（设置页可改、即时生效），此时不注册容器 cron，避免双重渲染。
if [ -f "$INKTIME_SETTINGS_PATH" ] || [ -f /app/data/web_settings.json ]; then
  echo "[entrypoint] 检测到 web_settings.json，定时调度交给 server 后台线程，不注册容器 cron" >> "$LOG_DIR/render.log"
else
  # 仅当未用设置页时，保留容器 cron 作为默认兜底（单任务渲染）
  if command -v crontab >/dev/null 2>&1; then
    (
      echo "SHELL=/bin/bash"
      echo "TZ=${TZ:-Asia/Shanghai}"
      echo "$CRON_SCHEDULE cd /app && python render_daily_photo.py >> $LOG_DIR/render.log 2>&1"
    ) | crontab -
    echo "[entrypoint] 已注册容器 cron: '$CRON_SCHEDULE' -> render_daily_photo.py" >> "$LOG_DIR/render.log"
    cron
    echo "[entrypoint] cron 已启动" >> "$LOG_DIR/render.log"
  else
    echo "[WARN] 容器内无 crontab，定时渲染需外部调度（如 宿主机 cron / Unraid 计划任务）" >> "$LOG_DIR/render.log"
  fi
fi

echo "[entrypoint] 启动 server.py @ ${FLASK_HOST:-0.0.0.0}:${FLASK_PORT:-8765}" >> "$LOG_DIR/server.log"

# ---- 前台跑 server.py ----
exec python server.py
