# InkTime Pro 服务端（照片AI打分 + 多屏渲染 + HTTP下载 + WebUI）
# 基础：python:3.11-slim（稳定且依赖兼容）
FROM python:3.11-slim

# 中文时区 + exiftool（读GPS/EXIF）+ libraw（RAW解码）+ 中文字体 + gosu（PUID/PGID 切用户）
# rawpy 依赖 libraw* 系统库解 RAW 相机格式；exiftool 读完整 GPS
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata libimage-exiftool-perl fonts-noto-cjk \
        libraw23 libraw-bin gosu \
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

# 中文城市索引（GPS→城市名，给 VLM 喂"拍摄地点"）。放 /app 根，配 ./world_cities_zh.csv 能直接读到
COPY world_cities_zh.csv ./world_cities_zh.csv

# config.py 不存在时用模板兜底一份（不含真实密钥，运行时挂载覆盖）
RUN cp config-example.py config.py || true

# 数据/输出/日志目录（挂载点；实际所有者为 entrypoint 里按 PUID/PGID 定的用户）
RUN mkdir -p /app/data /app/photos /app/logs /app/output /app/data/converted

# PUID / PGID：Unraid 标准的宿主机用户ID映射（默认 99:100 = Unraid 的 root:users 常见值）
# 容器内按这两个值创建一个 user 运行，从而对挂载的 /mnt/user/... 目录有正确写权限。
ENV PUID=99 \
    PGID=100 \
    INKTIME_IMAGE_DIR=/app/photos \
    INKTIME_DB_PATH=/app/data/photos.db \
    INKTIME_BIN_OUTPUT_DIR=/app/data/output \
    INKTIME_CONVERTED_DIR=/app/data/converted \
    INKTIME_SETTINGS_PATH=/app/data/web_settings.json \
    INKTIME_LOG_DIR=/app/logs \
    FLASK_HOST=0.0.0.0 \
    FLASK_PORT=8765

# 入口脚本：启动 server（含后台调度）。entrypoint 负责按 PUID/PGID 建用户并降权运行。
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8765

# 健康检查：用 Python 的 socket 测 8765 端口是否在听（无需额外依赖）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1',8765), timeout=4).close(); raise SystemExit(0)" \
        || python -c "import socket,sys; sys.exit(1)"

# 入口：以 root 跑 entrypoint（内部切 PUID/PGID）。
# 用 exec /entrypoint 而不是直接 docker-entrypoint.sh，保证信号转发。
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
