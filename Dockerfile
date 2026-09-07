# InkTime 服务器（分析 + 渲染 + NFC/HTTP 下载 + WebUI）
# 基础：python:3.11-slim（3.11 稳定且比 3.10 新，依赖均兼容）
FROM python:3.11-slim

# 中文时区 + cron + exiftool（读取照片 GPS/EXIF）+ libraw（RAW 解码）+ 中文字体
# rawpy 依赖 python3-rawpy 或 libraw* 系列系统库来解 RAW 相机格式
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata cron libimage-exiftool-perl fonts-noto-cjk \
        libraw23 libraw-bin \
    && rm -rf /var/lib/apt/lists/*

# 设置时区（可在 compose 里覆盖，这里给默认东八区）
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# 复制依赖清单并先装（利用 Docker 层缓存）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码（config.py 由用户挂载/覆写，不在这里固化）
COPY analyze_photos.py render_daily_photo.py server.py config-example.py ./

# config.py 可能由用户 volume 挂载覆盖；若不存在则用模板拷一份
RUN cp config-example.py config.py || true

# 数据目录（挂载点）
RUN mkdir -p /app/data /app/photos /app/logs /app/output /app/data/converted

# 运行时也允许覆盖（compose 传 MINIMAX_API_KEY / 其它环境变量）
ENV INKTIME_IMAGE_DIR=/app/photos \
    INKTIME_DB_PATH=/app/data/photos.db \
    INKTIME_BIN_OUTPUT_DIR=/app/data/output \
    INKTIME_CONVERTED_DIR=/app/data/converted \
    INKTIME_LOG_DIR=/app/logs

# 入口脚本：启动 cron + server.py
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8765

CMD ["docker-entrypoint.sh"]
