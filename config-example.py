# -*- coding: utf-8 -*-
# 照片库路径（你自己的相册目录）
# Docker 部署时建议用环境变量覆盖：INKTIME_IMAGE_DIR
import os
IMAGE_DIR = os.environ.get("INKTIME_IMAGE_DIR", "./test")

# 数据库路径（建议保持默认）
# Docker 部署时可覆盖：INKTIME_DB_PATH
DB_PATH = os.environ.get("INKTIME_DB_PATH", "./photos.db")

# VLM 多渠道（OpenAI 兼容视觉），按优先级自动降级（某家 429/失败自动切下一家）
# 注意：api.minimax.io 在本机旁路由下连不上；api.minimaxi.com 可达（2026-09-06 实测）。
# 重要：API_CHANNELS 条目数固定（3 条），运行时只改 api_key/enabled，勿删减（concatenate 数组长度定死）。
API_CHANNELS = [
    {
        "provider":   "minimax",
        "api_url":    "https://api.minimaxi.com/v1/chat/completions",
        "api_key":    os.environ.get("MINIMAX_API_KEY", ""),
        "model_name": "MiniMax-M3",
        "enabled":    True,
    },
    {
        "provider":   "zhipu",
        "api_url":    "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "api_key":    os.environ.get("ZHIPU_API_KEY", ""),
        "model_name": "glm-4.6v",
        "enabled":    True,
    },
    {
        "provider":   "deepseek",
        "api_url":    "https://api.deepseek.com/chat/completions",
        "api_key":    os.environ.get("DEEPSEEK_API_KEY", ""),
        "model_name": "deepseek-v4-flash-vision-exp",
        "enabled":    True,
    },
]

# 每次最多处理多少张的图片
BATCH_LIMIT = None

# 请求超时时间（秒）
TIMEOUT = 600

# 某个渠道失败后，临时降低其优先级的冷却时间（秒）
# 例如 A 失败、B 成功后，在冷却期内后续照片会优先从 B 开始请求
CHANNEL_FAILOVER_COOLDOWN_SEC = 300

# 为防止照片隐私泄露，建议为 ESP32 下载路径加一个随机前缀作为密钥
# 前缀修改后，请同步修改 ESP32 固件中的 DAILY_PHOTO_PATH_PREFIX 字段
DOWNLOAD_KEY = "inktime_local_test"

# 成品目录：存"各启用屏预裁切好的基图"（analyze 时写，render 复用）
CONVERTED_DIR = os.environ.get("INKTIME_CONVERTED_DIR", "./converted")

# Flask 静态服务
FLASK_HOST = "0.0.0.0"
FLASK_PORT = 8765
# 是否开启照片库 WebUI（前期检验提示词选片效果时使用，跑通后建议关闭）
ENABLE_REVIEW_WEBUI = True

# 离线中文城市名索引，使用 geonames 数据制作。
# Docker 里放 /app 根（镜像已 COPY），默认 ./world_cities_zh.csv 直接可读。
WORLD_CITIES_CSV = os.environ.get("INKTIME_WORLD_CITIES", "./world_cities_zh.csv")

# 网格大小（纬度/经度度数）；越大越快但精度略差。1.0 对大多数场景够用。
CITY_GRID_DEG = 1.0

# 你的“常驻常驻”坐标（用于判断是否为旅行期间照片，从而对评分进行小幅加成）
# 照片 GPS 距离常驻地超过 HOME_RADIUS_KM，则视为“异地”
# 默认值给了深圳市中心附近（不改也能保持原行为的大致效果）
HOME_LAT = 22.543096
HOME_LON = 114.057865
HOME_RADIUS_KM = 60.0

# 最大接受距离（公里），超出则认为“不在任何城市附近”
CITY_MAX_DISTANCE_KM = 100.0

# 墨水屏渲染 BIN 文件输出目录
# Docker 部署时可覆盖：INKTIME_BIN_OUTPUT_DIR
BIN_OUTPUT_DIR = os.environ.get("INKTIME_BIN_OUTPUT_DIR", "./output")

# 自定义字体路径（为空则退回默认字体）
FONT_PATH = ""

# 每日选片“精彩度”阈值
MEMORY_THRESHOLD = 70.0

# 每日挑选的照片数量
DAILY_PHOTO_QUANTITY = 5

# 发送给 VLM 之前，先把图片长边缩放到该值（像素）。MiniMax 建议 512，省 token/成本。
VLM_MAX_LONG_EDGE = 512

# 无意义照片判定：memory_score 低于该值则视为"无意义"，跳过旁白与成品生成
UNMEANINGFUL_THRESHOLD = float(os.environ.get("INKTIME_UNMEANINGFUL_THRESHOLD", 40.0))

# ========== 5小时窗口对齐的 Token 感知调度参数（可选）==========
# MiniMax 套餐额度是"5 小时一清、用不完浪费"。深夜空闲自动多扫照片把额度用掉。
IDLE_WINDOW_START = os.environ.get("INKTIME_IDLE_START", "23:00")   # 空闲时段起点（深夜）
IDLE_WINDOW_END = os.environ.get("INKTIME_IDLE_END", "07:00")       # 空闲时段终点
QUOTA_SCHEDULE_ENABLED = os.environ.get("INKTIME_QUOTA_SCHEDULE", "1") not in ("0", "false", "False")
QUOTA_EDGE_MIN = float(os.environ.get("INKTIME_QUOTA_EDGE_MIN", 30))      # 窗口剩 <N 分钟触发
QUOTA_PER_RUN_BATCH_LIMIT = int(os.environ.get("INKTIME_QUOTA_BATCH_LIMIT", 20))  # 每次限量
QUOTA_FIRE_COOLDOWN_SEC = int(os.environ.get("INKTIME_QUOTA_COOLDOWN", 1800))     # 冷却
QUOTA_IDLE_PERCENT = float(os.environ.get("INKTIME_QUOTA_IDLE_PERCENT", 5.0))     # 空闲余量>N%才烧

# ========== 屏幕配置（多分辨率并存）==========
# SCREENS 是列表：一次可为多块屏（不同尺寸/分辨率/颜色）同时出成品。
# 每项含 enabled 开关：enabled=True 的屏才会被渲染/生成成品；第一个启用屏为默认屏。
# 字段：
#   - width,height   : 画布逻辑分辨率（行优先 宽x高）
#   - text_area_height: 底部文字区高度(px)
#   - palette        : 调色板 RGB 列表，索引顺序即 BIN 里像素的索引值(0黑 1白 2红 3黄 4蓝 5绿...)
#   - bin_format     : "1byte_per_px"（每像素1字节，7.3"/7.09" 用）
#                      "2px_per_byte"（每字节2像素/4bit，12.43" 用）
SCREENS = [
    # 当前屏：GDEP073E01 竖用 480x800，六色
    {
        "name": "GDEP073E01",
        "width": 480,
        "height": 800,
        "text_area_height": 100,
        "palette": [
            (0, 0, 0),      # 0 黑
            (255, 255, 255),# 1 白
            (200, 0, 0),    # 2 红
            (220, 180, 0),  # 3 黄
            (0, 0, 255),    # 4 蓝
            (0, 150, 0),    # 5 绿
        ],
        "bin_format": "1byte_per_px",
        "enabled": True,
    },
    # 示例：7.09" 高分辨率 E6（1200x1600，六色，1byte/px）。取消 enabled 改 True 即启用
    # {
    #     "name": "7.09_E6",
    #     "width": 1200,
    #     "height": 1600,
    #     "text_area_height": 200,
    #     "palette": [(0,0,0),(255,255,255),(200,0,0),(220,180,0),(0,0,255),(0,150,0)],
    #     "bin_format": "1byte_per_px",
    #     "enabled": False,
    # },
    # 示例：12.43" 133C（1208x1600，六色，2px/byte）
    # {
    #     "name": "12.43_133C",
    #     "width": 1208,
    #     "height": 1600,
    #     "text_area_height": 200,
    #     "palette": [(0,0,0),(255,255,255),(200,0,0),(220,180,0),(0,0,255),(0,150,0)],
    #     "bin_format": "2px_per_byte",
    #     "enabled": False,
    # },
]

# 成品目录：存"各启用屏预裁切好的基图"（analyze 时写，render 复用）
CONVERTED_DIR = os.environ.get("INKTIME_CONVERTED_DIR", "./converted")

# 无意义照片判定阈值（低于则跳过旁白/成品）
UNMEANINGFUL_THRESHOLD = float(os.environ.get("INKTIME_UNMEANINGFUL_THRESHOLD", 40.0))

# 5小时窗口对齐的 Token 感知调度参数
IDLE_WINDOW_START = os.environ.get("INKTIME_IDLE_START", "23:00")
IDLE_WINDOW_END = os.environ.get("INKTIME_IDLE_END", "07:00")
QUOTA_EDGE_MIN = float(os.environ.get("INKTIME_QUOTA_EDGE_MIN", 30))
QUOTA_PER_RUN_BATCH_LIMIT = int(os.environ.get("INKTIME_QUOTA_BATCH_LIMIT", 20))
QUOTA_FIRE_COOLDOWN_SEC = int(os.environ.get("INKTIME_QUOTA_COOLDOWN", 1800))
QUOTA_IDLE_PERCENT = float(os.environ.get("INKTIME_QUOTA_IDLE_PERCENT", 5.0))
