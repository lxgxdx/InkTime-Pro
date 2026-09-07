# InkTime Pro 服务端（照片AI打分 + 多屏渲染 + HTTP下载 + WebUI）
# 基础：python:3.11-slim（稳定且依赖兼容）
FROM python:3.11-slim

# 中文时区 + cron + exiftool（读GPS/EXIF）+ libraw（RAW解码）+ 中文字体
# rawpy 依赖 libraw* 系统库解 RAW 相机格式；exiftool 读完整 GPS
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata cron libimage-exiftool-perl fonts-noto-cjk \
        libraw23 libraw-bin \
    && rm -rf /var/lib/apt/lists/*

# 时区（compose 可覆盖，这里给东八区默认）
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# 依赖清单先装（利用 Docker 层缓存）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 复制全部应用代码
# ⚠️ config.py 是用户本机/挂载的带密钥版本，不进镜像、不入库；这里用 .env / volume 注入
COPY analyze_photos.py render_daily_photo.py server.py \
     converter.py web_settings.py image_utils.py config-example.py ./

# config.py 不存在时用模板兜底一份（不含真实密钥，运行时挂载覆盖）
RUN cp config-example.py config.py || true

# 数据/输出/日志目录（挂载点）
RUN mkdir -p /app/data /app/photos /app/logs /app/output /app/data/converted

# 运行时环境变量（compose 可覆盖）
ENV INKTIME_IMAGE_DIR=/app/photos \
    INKTIME_DB_PATH=/app/data/photos.db \
    INKTIME_BIN_OUTPUT_DIR=/app/data/output \
    INKTIME_CONVERTED_DIR=/app/data/converted \
    INKTIME_SETTINGS_PATH=/app/data/web_settings.json \
    INKTIME_LOG_DIR=/app/logs \
    FLASK_HOST=0.0.0.0 \
    FLASK_PORT=8765

# 非 root 运行（更安全；数据目录挂载到 host 时注意权限）
RUN useradd -m -u 1000 inktime \
    && mkdir -p /app/data /app/photos /app/logs /app/output /app/data/converted \
    && chown -R inktime:inktime /app
USER inktime

# 入口脚本：启动 server（含后台调度）+ 可选容器 cron 兜底
COPY --chown=inktime:inktime docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8765

# 健康检查：用 Python 的 socket 测 8765 端口是否在听（无需额外依赖）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1',8765), timeout=4).close(); raise SystemExit(0)" \
        || python -c "import socket,sys; sys.exit(1)"

# 入口（容器是 cron+server 常驻，所以前台跑 entrypoint）
CMD ["docker-entrypoint.sh"]
