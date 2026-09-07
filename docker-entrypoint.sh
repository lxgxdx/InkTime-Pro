#!/usr/bin/env bash
set -euo pipefail

# ------------------------------------------------------------
# InkTime Pro 容器入口：
#   1) 按 PUID/PGID 创建应用用户（Unraid 标准做法，保证能写挂载的宿主机目录）
#   2) chown 数据/输出/日志目录给该用户
#   3) 降权到该用户，前台跑 server.py（含后台调度 Scheduler）
# 手动分析照片： docker exec <容器> python analyze_photos.py
# 手动渲染一次： docker exec <容器> python render_daily_photo.py
# ------------------------------------------------------------

# PUID / PGID：宿主机用户/组 ID（默认 99:100 = Unraid 的 nobody:users）。可在容器变量里覆盖。
PUID="${PUID:-99}"
PGID="${PGID:-100}"

LOG_DIR="${INKTIME_LOG_DIR:-/app/logs}"
DATA_DIR="${INKTIME_DATA_DIR:-/app/data}"
OUT_DIR="${INKTIME_BIN_OUTPUT_DIR:-/app/data/output}"
CONVERTED_DIR="${INKTIME_CONVERTED_DIR:-/app/data/converted}"
PHOTOS_DIR="${INKTIME_IMAGE_DIR:-/app/photos}"

# 确保目录存在（挂载时可能只映射了部分）
mkdir -p "$LOG_DIR" "$OUT_DIR" "$CONVERTED_DIR" "$PHOTOS_DIR" "$DATA_DIR"

# 创建/复用应用用户（名 inktime，UID/GID 用宿主机值）
if ! getent group inktime >/dev/null 2>&1; then
  groupadd -g "$PGID" inktime 2>/dev/null || groupadd inktime
fi
if ! id -u inktime >/dev/null 2>&1; then
  useradd -m -s /bin/bash -u "$PUID" -g "$PGID" inktime 2>/dev/null || {
    useradd -m -s /bin/bash -g "$PGID" inktime 2>/dev/null || useradd -m -s /bin/bash inktime
  }
else
  # 已存在则把 UID/GID 对齐到 PUID/PGID
  usermod -u "$PUID" -g "$PGID" inktime 2>/dev/null || true
fi

# 把数据目录所属权交给应用用户，保证不出 Permission denied
chown -R "$PUID:$PGID" "$LOG_DIR" "$OUT_DIR" "$CONVERTED_DIR" "$DATA_DIR" 2>/dev/null || true

echo "[entrypoint] 应用用户 inktime (UID=$PUID GID=$PGID)，数据目录已就绪" >> "$LOG_DIR/server.log" 2>/dev/null || true
touch "$LOG_DIR/server.log" 2>/dev/null || true

echo "[entrypoint] 启动 server.py @ ${FLASK_HOST:-0.0.0.0}:${FLASK_PORT:-8765}" >> "$LOG_DIR/server.log" 2>/dev/null || true

# 降权到应用用户，前台跑 server.py（常驻）
exec gosu inktime python server.py
