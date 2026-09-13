#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path
import base64
import datetime as dt
import hashlib
import json
import sqlite3
import os
import sys
import subprocess
import time
import threading
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import io
from PIL import Image, ExifTags, ImageOps
import pillow_heif
pillow_heif.register_heif_opener()
import config as cfg
import shutil
from image_utils import load_image_any, _RAW_EXTS
from converter import (
    normalize_crop,
    generate_converted_for_screens,
)


# =======================
# 扫描进度上报（供 Web 页面轮询显示）
# =======================
# 进度文件路径：server 拉起 analyze 时通过环境变量 INKTIME_PROGRESS_FILE 注入。
# analyze 每处理一张就把进度写成 JSON，server /api/scan/progress 读它返回给前端。
SCAN_PROGRESS_FILE = os.environ.get("INKTIME_PROGRESS_FILE", "")


def report_scan_progress(
    done: int,
    total: int,
    phase: str = "",
    current: str = "",
    detail: str = "",
) -> None:
    """把当前扫描进度写入进度文件（单次 json 覆盖写，读到的是最新快照）。

    失败静默——进度上报只是增强，绝不影响扫描主体。
    """
    if not SCAN_PROGRESS_FILE:
        return  # 未注入进度文件路径（比如命令行直接跑），跳过
    try:
        pct = (done / total) if total > 0 else 0.0
        payload = {
            "done": done,
            "total": total,
            "percent": round(100.0 * pct, 1),
            "phase": phase,
            "current": current,
            "detail": detail,
        }
        path = Path(SCAN_PROGRESS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def clear_scan_progress() -> None:
    """扫描结束后清除进度文件，让前端回到『未在扫描』状态。"""
    if not SCAN_PROGRESS_FILE:
        return
    try:
        Path(SCAN_PROGRESS_FILE).unlink(missing_ok=True)
    except Exception:
        pass


def _is_think_model(name: str) -> bool:
    """判断某模型是否默认带 <think> 思考块（需要显式关闭推理）。

    MiniMax-M3、智谱 GLM、DeepSeek 系列都可能在输出前带 thinking。
    """
    n = (name or "").lower()
    return any(k in n for k in ("minimax", "glm", "deepseek"))


# =======================
# NAS 掉盘守护（macOS /Volumes）
# =======================
NAS_MOUNT_URL = str(getattr(cfg, "NAS_MOUNT_URL", "") or "").strip()
NAS_MOUNT_POINT = Path(str(getattr(cfg, "NAS_MOUNT_POINT", "/Volumes/photo") or "/Volumes/photo")).expanduser()
NAS_RETRY_TIMES = int(getattr(cfg, "NAS_RETRY_TIMES", 3) or 3)
NAS_RETRY_SLEEP_SEC = float(getattr(cfg, "NAS_RETRY_SLEEP_SEC", 2.0) or 2.0)


def _is_mount_ok() -> bool:
    """判断 NAS 是否仍然挂载（尽量保守）。"""
    try:
        # /Volumes 下的网络卷一般是 mount point
        if NAS_MOUNT_POINT and NAS_MOUNT_POINT.exists():
            if os.path.ismount(str(NAS_MOUNT_POINT)):
                return True
        # 兜底：只要图片根目录可访问也算 OK
        return IMAGE_DIR.exists()
    except Exception:
        return False


def _try_remount_nas() -> bool:
    """尝试重挂载 NAS。

    只在配置了 NAS_MOUNT_URL 时执行；优先使用 macOS 的 AppleScript 挂载，
    这样可以复用钥匙串里保存的凭据。
    """
    if not NAS_MOUNT_URL:
        return False

    print(f"[WARN] 检测到 NAS 可能掉盘，尝试重挂载：{NAS_MOUNT_URL}")

    # 1) AppleScript（推荐）：mount volume "afp://..." / "smb://..."
    try:
        subprocess.run(
            ["osascript", "-e", f'mount volume "{NAS_MOUNT_URL}"'],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        pass

    # 2) 兜底：如果你更喜欢命令行挂载，可以自行改成 mount_afp / mount_smbfs。

    # 等待卷出现
    for _ in range(10):
        if _is_mount_ok():
            print("[INFO] NAS 重挂载成功，继续处理。")
            return True
        time.sleep(0.5)

    print("[WARN] NAS 重挂载失败（卷仍不可用）。")
    return False


def _read_bytes_with_nas_retry(path: Path) -> bytes:
    """读文件：遇到 NAS 掉盘类错误时，尝试重挂载并重试。"""
    last_err: Exception | None = None

    # 只对照片库路径内的文件启用重挂载逻辑，避免误伤本地文件。
    try:
        in_photo_dir = str(path).startswith(str(IMAGE_DIR))
    except Exception:
        in_photo_dir = False

    for attempt in range(1, max(1, NAS_RETRY_TIMES) + 1):
        try:
            return path.read_bytes()
        except OSError as e:
            last_err = e

            # 不在照片库目录内，或者没有配置 NAS URL，直接抛
            if not in_photo_dir or not NAS_MOUNT_URL:
                raise

            # 常见网络卷断连错误：57 Socket is not connected；也可能表现为 5/6 等 I/O 类错误。
            # 这里不做过度精确匹配，先判断挂载状态；如果掉盘就尝试重挂载。
            if not _is_mount_ok():
                print(f"[WARN] 读文件失败（第 {attempt}/{NAS_RETRY_TIMES} 次）：{e}")
                ok = _try_remount_nas()
                if ok:
                    # 重挂载后立刻重试
                    continue

            # 如果挂载看起来还 OK，但仍然读失败，按重试策略稍等再试
            if attempt < NAS_RETRY_TIMES:
                print(f"[WARN] 读文件失败（第 {attempt}/{NAS_RETRY_TIMES} 次），稍后重试：{e}")
                time.sleep(max(0.1, NAS_RETRY_SLEEP_SEC))
                continue

            raise

    # 理论上不会到这
    if last_err:
        raise last_err
    raise OSError("读取文件失败")


# ================== 配置区域（来自 config.py） ==================

ROOT_DIR = Path(__file__).resolve().parent

# 成品目录：写各启用屏预裁切基图（模块2+1）
CONVERTED_DIR = Path(str(getattr(cfg, "CONVERTED_DIR", "./converted") or "./converted")).expanduser()
if not CONVERTED_DIR.is_absolute():
    CONVERTED_DIR = (ROOT_DIR / CONVERTED_DIR).resolve()

# 无意义判定阈值（模块3）
UNMEANINGFUL_THRESHOLD = float(getattr(cfg, "UNMEANINGFUL_THRESHOLD", 40.0) or 40.0)

# 启用屏列表（支持多分辨率并存）：优先 SCREENS，兼容旧 SCREEN 单对象
_SCREENS_raw = getattr(cfg, "SCREENS", None) or getattr(cfg, "SCREEN", None) or {}
if isinstance(_SCREENS_raw, dict):
    SCREENS = [_SCREENS_raw]
elif isinstance(_SCREENS_raw, list) and _SCREENS_raw:
    SCREENS = _SCREENS_raw
else:
    SCREENS = [{}]
# 只保留启用且尺寸合法的屏当作成品生成目标
SCREENS_ENABLED = [
    s for s in SCREENS if s.get("enabled", True) and int(s.get("width", 0)) > 0 and int(s.get("height", 0)) > 0
]

# 要扫描的图片目录
IMAGE_DIR = Path(str(getattr(cfg, "IMAGE_DIR", "") or "")).expanduser()
if not IMAGE_DIR.is_absolute():
    IMAGE_DIR = (ROOT_DIR / IMAGE_DIR).resolve()

# SQLite 数据库路径
DB_PATH = Path(str(getattr(cfg, "DB_PATH", "photos.db") or "photos.db")).expanduser()
if not DB_PATH.is_absolute():
    DB_PATH = (ROOT_DIR / DB_PATH).resolve()

# ---- 渠道列表（支持 429 自动切换） ----
_raw_channels = getattr(cfg, "API_CHANNELS", None) or []
if not _raw_channels:
    # 向后兼容：从旧的单变量配置构建单渠道
    _compat_url = str(
        getattr(cfg, "API_URL", None)
        or os.environ.get("LMSTUDIO_URL", "http://127.0.0.1:1234/v1/chat/completions")
    )
    _compat_model = str(
        getattr(cfg, "MODEL_NAME", None)
        or os.environ.get("LMSTUDIO_MODEL", "qwen3-vl-32b-instruct")
    )
    _compat_key = str(
        getattr(cfg, "API_KEY", None)
        or os.environ.get("LMSTUDIO_API_KEY", "")
    )
    _raw_channels = [
        {"api_url": _compat_url, "model_name": _compat_model, "api_key": _compat_key}
    ]

API_CHANNELS: list[dict] = list(_raw_channels)
_channel_index: int = 0  # 记住上次成功的渠道位置
_channel_cooldown_until: list[float] = [0.0] * len(API_CHANNELS)
_channel_inflight: list[int] = [0] * len(API_CHANNELS)
_channel_lock = threading.Lock()  # 保护 _channel_index 的并发访问

# 每次处理多少张；None 为不限制
BATCH_LIMIT = getattr(cfg, "BATCH_LIMIT", None)

# 请求超时时间（秒）
TIMEOUT = float(getattr(cfg, "TIMEOUT", 600) or 600)

# 某个渠道失败后，临时降低其优先级的冷却时间（秒）。
# 例如 A 失败、B 成功后，后续会优先从 B 开始，而不是每次都先打 A。
_raw_failover_cooldown = getattr(cfg, "CHANNEL_FAILOVER_COOLDOWN_SEC", 300)
if _raw_failover_cooldown is None:
    CHANNEL_FAILOVER_COOLDOWN_SEC = 300.0
else:
    CHANNEL_FAILOVER_COOLDOWN_SEC = float(_raw_failover_cooldown)

# 发送给 VLM 之前，先把图片长边缩放到该值（像素）。
# 0 表示不缩放。
# 本地推理可保持较高值；云端推理建议降低（减少 token/成本）。
VLM_MAX_LONG_EDGE = int(getattr(cfg, "VLM_MAX_LONG_EDGE", 1024) or 1024)

# 中文城市数据库位置
WORLD_CITIES_CSV = Path(str(getattr(cfg, "WORLD_CITIES_CSV", "data/world_cities_zh.csv") or "data/world_cities_zh.csv")).expanduser()
if not WORLD_CITIES_CSV.is_absolute():
    WORLD_CITIES_CSV = (ROOT_DIR / WORLD_CITIES_CSV).resolve()

CITY_GRID_DEG = float(getattr(cfg, "CITY_GRID_DEG", 1.0) or 1.0)
CITY_MAX_DISTANCE_KM = float(getattr(cfg, "CITY_MAX_DISTANCE_KM", 80.0) or 80.0)
# 最近城市距离超过该值时，地名后面带上公里数（如 "深圳 32km"）—— 提示这只是
# "最近的城市"而不是拍摄地本身，一眼能看出这个地名的可信度。
# 城市库够密时绝大多数照片都在阈值内，显示的就是干净的城市名。
CITY_DIST_HINT_KM = float(getattr(cfg, "CITY_DIST_HINT_KM", 20.0) or 20.0)
# 在「最近城市 + 该缓冲」范围内优先选人口最多的点。cities500 会把一座城市拆成很多
# 区/町级点，纯按距离取最近会拿到认不出的名字（东京市中心最近的点叫「永福」，
# 而「东京」本体在 4km 外）。缓冲要够大才能把它们纳入比较；10km 在城市尺度上
# 通常仍属同一座城市，不会跨到隔壁市。
CITY_NEAR_BUFFER_KM = float(getattr(cfg, "CITY_NEAR_BUFFER_KM", 10.0) or 10.0)



HOME_LAT = float(getattr(cfg, "HOME_LAT", 22.543096) or 22.543096)
HOME_LON = float(getattr(cfg, "HOME_LON", 114.057865) or 114.057865)
HOME_RADIUS_KM = float(getattr(cfg, "HOME_RADIUS_KM", 60.0) or 60.0)
# ==================================================

# 调试模式（由 --debug 命令行参数控制）
DEBUG: bool = False

# exiftool 是否可用：缺失时只降级 GPS/部分 EXIF，不中断流程
EXIFTOOL_AVAILABLE = False

def require_exiftool() -> None:
    global EXIFTOOL_AVAILABLE
    EXIFTOOL_AVAILABLE = shutil.which("exiftool") is not None
    if not EXIFTOOL_AVAILABLE:
        print(
            "[WARN] 未找到 exiftool，将跳过 exiftool 辅助的 GPS/EXIF 读取（不影响主流程）。\n"
            "       如需更完整的 GPS 信息，请安装：\n"
            "       macOS: brew install exiftool\n"
            "       Ubuntu/Debian: sudo apt-get install -y libimage-exiftool-perl\n"
            "       Windows: choco install exiftool"
        )

def encode_image_to_b64(path: Path) -> str:
    """读取图片并（可选）缩放长边后，重新编码为 JPEG，再转 base64。

    目的：
    1) 控制输入分辨率（尤其是 200MP 这类超大图），避免推理成本/延迟暴涨；
    2) 通过重新编码，尽量规避某些 JPEG 在 libvips 侧解码报错（如 marker 前多余字节）。
    """
    data = _read_bytes_with_nas_retry(path)

    # 尽量用 PIL 容错打开，然后统一转成干净的 JPEG bytes
    try:
        # RAW/HEIC/普通图：统一由 image_utils 解码成 RGB PIL Image
        img = load_image_any(path)

        # 可选缩放
        try:
            w, h = img.size
            long_edge = max(w, h)
            if VLM_MAX_LONG_EDGE and long_edge > VLM_MAX_LONG_EDGE:
                scale = float(VLM_MAX_LONG_EDGE) / float(long_edge)
                new_w = max(1, int(round(w * scale)))
                new_h = max(1, int(round(h * scale)))
                img = img.resize((new_w, new_h), resample=Image.LANCZOS)
        except Exception:
            pass

        out = io.BytesIO()
        # quality 92 在观感和体积之间比较平衡；optimize 可能更慢但通常能降体积
        img.save(out, format="JPEG", quality=92, optimize=True)
        clean_bytes = out.getvalue()
        return base64.b64encode(clean_bytes).decode("utf-8")

    except Exception:
        # 兜底：如果 PIL 也打不开，就退回原始 bytes（让上游报错更直观）
        return base64.b64encode(data).decode("utf-8")


def ensure_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS photo_scores (
            path              TEXT PRIMARY KEY,
            caption           TEXT,
            type              TEXT,
            memory_score      REAL,
            beauty_score      REAL,
            reason            TEXT,
            width             INTEGER,
            height            INTEGER,
            orientation       TEXT,
            used_at           TEXT,
            used_count        INTEGER,
            exif_json         TEXT,
            raw_json          TEXT,
            exif_datetime     TEXT,
            exif_make         TEXT,
            exif_model        TEXT,
            exif_iso          INTEGER,
            exif_exposure_time REAL,
            exif_f_number     REAL,
            exif_focal_length REAL,
            exif_gps_lat      REAL,
            exif_gps_lon      REAL,
            exif_gps_alt      REAL,
            side_caption      TEXT,
            exif_city         TEXT
        )
        """
    )
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_json TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN width INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN height INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN orientation TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN used_at TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN used_count INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_datetime TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_make TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_model TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_iso INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_exposure_time REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_f_number REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_focal_length REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_gps_lat REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_gps_lon REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_gps_alt REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN side_caption TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_city TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN crop_x REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN crop_y REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN crop_w REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN crop_h REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN is_meaningful INTEGER")
    except sqlite3.OperationalError:
        pass
    conn.commit()

# 生成一句话文案
def generate_side_caption(image_path: Path) -> str | None:
    system_prompt = (
        "你是一位为「电子相框」撰写旁白短句的中文文案助手。\n"
        "你的目标不是描述画面，而是为画面补上一点“画外之意”。\n\n"

        "创作原则：\n"
        "1. 避免使用以下词语：世界、梦、时光、岁月、温柔、治愈、刚刚好、悄悄、慢慢 等（但不是绝对禁止）。\n"
        "2. 严禁使用如下句式：……里……着整个世界；……里……着整个夏天；……得像……（简单的比喻）; ……比……还……； ……得比……更……。\n"
        "3. 只基于图片中能确定的信息进行联想，不要虚构时间、人物关系、事件背景。\n"
        "4. 文案应自然、有趣，带一点幽默或者诗意，但请避免煽情、鸡汤。\n"
        "5. 不要复述画面内容本身，而是写“看完画面后，心里多出来的一句话”。\n"
        "6. 可以偏向以下风格之一：\n"
        "   - 日常中的微妙情绪\n"
        "   - 轻微自嘲或冷幽默\n"
        "   - 对时间、记忆、瞬间的含蓄感受\n"
        "   - 看似平淡但有余味的一句判断\n"
        "7. 避免小学生作文式的、套路式的模板化表达\n"

        "格式要求：\n"
        "1. 只输出一句中文短句，不要换行，不要引号，不要任何解释。\n"
        "2. 建议长度 8～24 个汉字，最多不超过 30 个汉字。\n"
        "3. 不要出现“这张照片”“这一刻”“那天”等指代照片本身的词。\n"
    )
    user_prompt = "请基于这张照片，生成一句符合规则的中文文案。"
    try:
        img_b64 = encode_image_to_b64(image_path)
    except Exception:
        return None

    def _build(ch):
        headers = {"Content-Type": "application/json"}
        key = ch.get("api_key", "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = {
            "model": ch["model_name"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                        },
                    ],
                },
            ],
            "temperature": 0.7,
            "max_tokens": 256,
            "top_p": 0.9,
            "stream": False,
        }
        # MiniMax-M3 默认会输出 <think> 思考块，占满 max_tokens。
        # 这里显式关闭推理，让有限的 token 都留给旁白正文。
        if _is_think_model(ch.get("model_name", "")):
            body["thinking"] = {"type": "disabled"}
        return ch["api_url"], headers, body

    try:
        resp = _post_with_channel_fallback(_build, timeout=min(120, TIMEOUT))
    except Exception:
        return None

    if not resp.ok:
        return None

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except Exception:
        return None

    if not isinstance(content, str):
        content = str(content)

    # 剥离 <think> 块与 markdown 围栏（MiniMax-M3 会带思考过程）
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    content = re.sub(r"```[\s\S]*?```", "", content, flags=re.S)
    content = content.strip()

    caption = content.strip().strip("“”\"'")
    return caption or None


def list_images(limit: int | None = None) -> list[Path]:
    # 支持常见 RAW 相机格式（.dng/.cr2/.nef/.arw/.orf/.raf/.rw2/.pef 等）。
    # RAW 解码依赖 rawpy（LibRaw）；未安装时仅提示，不影响 JPG/PNG/HEIC 常规照片。
    exts = {
        ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".heic", ".heif",   # 常规
        ".dng", ".cr2", ".nef", ".arw", ".orf", ".raf", ".rw2", ".pef",  # RAW
    }
    files = []
    print("[INFO] 正在递归扫描图片目录，请稍候……")
    scanned = 0
    for p in IMAGE_DIR.rglob("*"):
        scanned += 1
        if scanned % 500 == 0:
            print(f"[SCAN] 已扫描文件数：{scanned} …")
        if p.is_file() and p.suffix.lower() in exts:
            if is_ignored(p):
                continue
            files.append(p)
    print(f"[INFO] 扫描完成，共发现 {len(files)} 张图片（文件总数 {scanned}）。")
    if limit is not None:
        files = files[:limit]
    return files


# NAS / 系统目录名（小写比较）：这些目录里放的是缩略图或索引文件，不是用户照片。
# 群晖 @eaDir、威联通 .@__thumb、Synology Photo 的 @Recycle、回收站等。
# 若不过滤，群晖缩略图会以正常照片身份送去 VLM 打分，白烧 token。
_IGNORED_DIR_PARTS = {
    "@eadir", ".@__thumb", ".thumbnails", "#recycle", ".#recycle",
    "@recycle", ".trash", ".trashes", ".snapshot", "synophotothumb",
    ".photostation", ".appledouble", "@tmp", "recycler", "$recycle.bin",
}


def is_ignored(path: Path) -> bool:
    """排除截图、NAS 缩略图目录、系统资源分支文件。

    目录按【路径组件】整段比较，而非整串子串匹配 —— 否则用户目录名里带
    "photo"、"thumb" 之类的字样会连累其下全部照片。截图沿用原有的子串匹配
    （要能命中 Screenshot_20230101.jpg 这种文件名）。
    """
    if "screenshot" in str(path).lower():
        return True
    if any(part.lower() in _IGNORED_DIR_PARTS for part in path.parts):
        return True
    # macOS 的 ._xxx 资源分支（与真照片同名但带前缀，会被当独立照片重复入库）
    if path.name.startswith("._"):
        return True
    return False


# 旧名保留：外部/历史调用点少，但避免遗漏。
is_screenshot = is_ignored


def filter_unscored(conn: sqlite3.Connection, paths: list[Path]) -> list[Path]:
    """返回 paths 中尚未入库的那些（保持入参顺序）。

    用 TEMP 表 JOIN 而非 IN (?,?,…)：8000 张时占位符数量会撞上 SQLite 的变量
    上限（老版本为 999），且超长 SQL 的解析本身就慢。与 main() 里清理残留记录
    的临时表是同一个套路（表名不同，互不影响）。
    """
    if not paths:
        return []

    cur = conn.cursor()
    cur.execute("CREATE TEMP TABLE IF NOT EXISTS _temp_quoted_paths (path TEXT PRIMARY KEY)")
    cur.execute("DELETE FROM _temp_quoted_paths")
    cur.executemany(
        "INSERT OR IGNORE INTO _temp_quoted_paths (path) VALUES (?)",
        [(str(p),) for p in paths],
    )
    rows = cur.execute(
        "SELECT t.path FROM _temp_quoted_paths t "
        "JOIN photo_scores s ON s.path = t.path"
    ).fetchall()
    already = {row[0] for row in rows}
    cur.execute("DELETE FROM _temp_quoted_paths")
    return [p for p in paths if str(p) not in already]


# =======================
# 照片去重索引（独立的 photo_hashes 表）
# =======================
# 为什么不塞进 photo_scores：
#   1. photo_scores 的写入走 INSERT OR REPLACE，它不认识的列在重扫时会被抹掉；
#   2. 哈希是"派生数据"，生命周期和打分结果不同（可整表重算、可中断续跑）；
#   3. 还没入库的新照片也要能参与判重，photo_scores 装不下它们。

def ensure_hash_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS photo_hashes (
            path        TEXT PRIMARY KEY,
            file_size   INTEGER,
            file_sha1   TEXT,
            dhash       TEXT,
            exif_dt     TEXT,
            exif_make   TEXT,
            exif_model  TEXT,
            dup_of      TEXT,
            dup_kind    TEXT,
            indexed_at  TEXT
        )
        """
    )
    conn.commit()


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _sha1_file(path: Path) -> str | None:
    """文件内容的 sha1（分块读，大 RAW 不会一次性进内存）。"""
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError as e:
        print(f"[WARN] 读取失败，跳过内容哈希 {path}: {e}")
        return None
    return h.hexdigest()


def _load_raw_thumbnail(path: Path):
    """RAW 的内嵌缩略图（快）；没有缩略图才退化为半尺寸解码。

    全尺寸 postprocess 一张 DNG 要好几秒，8000 张就是几小时；而 dHash 只需要
    9x8 灰度，内嵌缩略图完全够用。
    """
    import rawpy
    try:
        with rawpy.imread(str(path)) as raw:
            try:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    return Image.open(io.BytesIO(thumb.data)).convert("L")
                return Image.fromarray(thumb.data).convert("L")
            except Exception:
                pass                      # 无内嵌缩略图（线性 DNG 等常见）
            rgb = raw.postprocess(use_camera_wb=True, half_size=True)
        return Image.fromarray(rgb).convert("L")
    except Exception as e:
        print(f"[WARN] RAW 解码失败，跳过感知哈希 {path}: {e}")
        return None


def _dhash(path: Path, size: int = 8) -> str | None:
    """感知哈希 dHash，返回 16 位十六进制（64 bit）。

    做法：缩到 (size+1) x size 灰度，逐行比较相邻像素亮暗 → 每行 size 个 bit。
    对 JPEG 用 draft() 走 libjpeg 的 DCT 缩略（毫秒级）；HEIC 不支持 draft，
    只能直接 resize。
    """
    img = None
    try:
        if path.suffix.lower() in _RAW_EXTS:
            img = _load_raw_thumbnail(path)
            if img is None:
                return None
            img = img.resize((size + 1, size), Image.LANCZOS)
        else:
            with Image.open(path) as im:
                try:
                    im.draft("L", (size + 1, size))   # JPEG：DCT 缩略，不解全图
                except Exception:
                    pass
                img = im.convert("L").resize((size + 1, size), Image.LANCZOS)

        pixels = list(img.getdata())
        width = size + 1
        bits = 0
        for row in range(size):
            base = row * width
            for col in range(size):
                bits = (bits << 1) | (1 if pixels[base + col] > pixels[base + col + 1] else 0)
        return f"{bits:016x}"
    except Exception as e:
        print(f"[WARN] 感知哈希失败，跳过 {path}: {e}")
        return None
    finally:
        if img is not None:
            try:
                img.close()
            except Exception:
                pass


def hamming_hex(a: str, b: str) -> int:
    """两个 16 位十六进制 dHash 的汉明距离（0~64）。"""
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except (TypeError, ValueError):
        return 64


_EXIF_DT_FORMATS = ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M")


def _parse_exif_dt(value) -> dt.datetime | None:
    """解析 EXIF 拍摄时间。

    EXIF 标准写法是 "2023:10:05 14:23:11"（日期用冒号分隔），
    datetime.fromisoformat 解析不了，必须显式给格式串。
    """
    if not value:
        return None
    txt = str(value).strip()
    for fmt in _EXIF_DT_FORMATS:
        try:
            return dt.datetime.strptime(txt, fmt)
        except ValueError:
            continue
    return None


def _read_exif_light(path: Path) -> tuple:
    """轻量读 EXIF：(拍摄时间, 相机厂商, 机型)，不解码像素。

    JPEG/HEIC 走 PIL 的 header 读取（最快）；RAW 用 PIL 打不开，退回 exiftool。
    RAW 恰恰是相机用户重复照片的大头（连拍、RAW+JPEG 同拍），不补这条路径，
    连拍与相似判重对它们就完全失效。
    """
    try:
        info = read_exif(path)
    except Exception:
        info = {}
    if not isinstance(info, dict):
        info = {}
    if info.get("datetime"):
        return (_parse_exif_dt(info.get("datetime")),
                (info.get("make") or "").strip() or None,
                (info.get("model") or "").strip() or None)
    # PIL 没拿到日期（RAW，或该文件的 EXIF 缺日期）→ 交给 exiftool 再试一次
    ex = read_exif_with_exiftool(path)
    return (_parse_exif_dt(ex.get("datetime")),
            (ex.get("make") or "").strip() or None,
            (ex.get("model") or "").strip() or None)


def run_dedupe(conn: sqlite3.Connection, paths: list, *,
               enable_similar: bool = True, hamming_max: int = 6,
               enable_burst: bool = True, burst_gap: int = 3,
               reindex: bool = False, progress_cb=None) -> dict:
    """建立/更新去重索引，并标记重复项，返回统计。

    分层，按成本从低到高，贵的那层只在嫌疑集上跑：
      ① file_size 分桶  —— stat 就有；size 不同的文件不可能字节相同
      ② sha1           —— 只在同 size 组内算            → dup_kind="exact"
      ③ 连拍聚类        —— (机型, 拍摄时间) 邻近，纯 EXIF  → dup_kind="burst"
      ④ dHash          —— 只对同机型同拍摄日的照片算      → dup_kind="similar"

    留一手不吃亏的顺序：④ 限定在"同机型同一天"的组内做，把 8000x8000 的两两
    比较降到每组几张小表，同时也不必为全库算感知哈希（那要开 8000 个文件）。

    保留规则（确定性）：已打过分的胜出，否则路径字典序最小者胜。确定性很关键 ——
    否则每轮跑出来的 dup_of 会翻转，用户会觉得"重复对象"变来变去。
    """
    ensure_hash_table(conn)
    cur = conn.cursor()
    now_iso = dt.datetime.now().isoformat(timespec="seconds")

    # ---- 载入既有索引 ----
    known = {}
    for r in cur.execute("SELECT path, file_size, file_sha1, dhash, exif_dt, "
                         "exif_make, exif_model FROM photo_hashes"):
        known[r[0]] = {"size": r[1], "sha1": r[2], "dhash": r[3],
                       "dt": _parse_exif_dt(r[4]), "_dt_raw": r[4],
                       "make": r[5], "model": r[6]}

    # ---- 一次 stat 拿全部文件大小（远比逐个 Image.open 便宜）----
    info = {}
    todo = 0
    for p in paths:
        sp = str(p)
        sz = _file_size(p)
        old = known.get(sp)
        if old and not reindex and old["size"] == sz and sz is not None:
            info[sp] = dict(old)
            continue
        todo += 1
        ex_dt, ex_make, ex_model = _read_exif_light(p)
        info[sp] = {"size": sz, "sha1": None, "dhash": None, "dt": ex_dt,
                    "_dt_raw": ex_dt.isoformat() if ex_dt else None,
                    "make": ex_make, "model": ex_model}
        if progress_cb and todo % 200 == 0:
            progress_cb(todo, len(paths), "读取 EXIF")

    # ---- union-find（代表取字典序最小，保证结果稳定）----
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rb < ra:
            ra, rb = rb, ra
        parent[rb] = ra

    kinds: dict[str, str] = {}          # 记录"被判重的原因"，取首次判定
    stats = {"exact": 0, "burst": 0, "similar": 0}

    # ---- ① size 分桶 + ② 同 size 内算 sha1 ----
    by_size: dict[int, list] = {}
    for sp, meta in info.items():
        if meta["size"] is not None:
            by_size.setdefault(meta["size"], []).append(sp)

    for size, group in by_size.items():
        if len(group) < 2:
            continue
        for sp in group:
            if info[sp]["sha1"] is None:
                info[sp]["sha1"] = _sha1_file(Path(sp))
        by_sha: dict[str, list] = {}
        for sp in group:
            if info[sp]["sha1"]:
                by_sha.setdefault(info[sp]["sha1"], []).append(sp)
        for sha, same in by_sha.items():
            if len(same) < 2:
                continue
            same.sort()
            for sp in same[1:]:
                union(same[0], sp)
                kinds.setdefault(sp, "exact")
                stats["exact"] += 1

    # ---- ③ 划定"时间邻近"候选簇（这一步不判重，只决定谁值得比感知哈希）----
    # 光按时间判重会误杀：扫街时连拍十张构图各不相同，时间都在几秒内，一刀切会把
    # 其中九张当重复丢掉 —— 那是真的丢内容。所以时间只用来【缩小候选范围】，
    # 是否真的重复一律交给 ④ 的 dHash 确认。
    burst_clusters: list[list] = []
    if enable_burst:
        by_cam: dict[tuple, list] = {}
        for sp, meta in info.items():
            if meta["dt"] is None or not meta["model"]:
                continue
            by_cam.setdefault((meta["make"] or "", meta["model"]), []).append(sp)
        for _, group in by_cam.items():
            group.sort(key=lambda s: (info[s]["dt"], s))
            window: list = []
            for sp in group:
                if window and (info[sp]["dt"] - info[window[-1]]["dt"]).total_seconds() > burst_gap:
                    if len(window) >= 2:
                        burst_clusters.append(window)
                    window = []
                window.append(sp)
            if len(window) >= 2:
                burst_clusters.append(window)

    # ---- ④ dHash 确认：候选集 = 时间邻近簇 ∪ 同机型同拍摄日 ----
    suspect_sets: list[tuple[list, str]] = []
    if enable_burst:
        suspect_sets += [(c, "burst") for c in burst_clusters]
    if enable_similar:
        by_day: dict[tuple, list] = {}
        for sp, meta in info.items():
            if meta["dt"] is None:
                continue
            key = (meta["make"] or "", meta["model"] or "", meta["dt"].date().isoformat())
            by_day.setdefault(key, []).append(sp)
        for group in by_day.values():
            if len(group) >= 2:
                suspect_sets.append((group, "similar"))

    for group, kind in suspect_sets:
        if not enable_similar:
            # 关掉视觉校验时退化为纯时间判重：快，但会丢掉连拍里构图不同的那些
            head = group[0]
            for sp in group[1:]:
                if find(head) != find(sp):
                    union(head, sp)
                    kinds.setdefault(sp, kind)
                    stats[kind] += 1
            continue
        for sp in group:
            if info[sp]["dhash"] is None:
                info[sp]["dhash"] = _dhash(Path(sp))
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                da, db_ = info[a]["dhash"], info[b]["dhash"]
                if not da or not db_:
                    continue
                if hamming_hex(da, db_) <= hamming_max and find(a) != find(b):
                    union(a, b)
                    kinds.setdefault(b, kind)
                    stats[kind] += 1

    # ---- 汇总：每组选一个保留者 ----
    groups: dict[str, list] = {}
    for sp in info:
        groups.setdefault(find(sp), []).append(sp)

    scored = {r[0] for r in cur.execute(
        "SELECT path FROM photo_scores WHERE memory_score IS NOT NULL")}

    rows, dup_total = [], 0
    for root, members in groups.items():
        members.sort()
        scored_members = [m for m in members if m in scored]
        keep = (min(scored_members) if scored_members else members[0]) if members else None
        for sp in members:
            meta = info[sp]
            is_dup = sp != keep
            if is_dup:
                dup_total += 1
            rows.append((
                sp, meta["size"], meta["sha1"], meta["dhash"],
                meta.get("_dt_raw"), meta["make"], meta["model"],
                keep if is_dup else None,
                kinds.get(sp) if is_dup else None,
                now_iso,
            ))

    cur.executemany(
        """INSERT OR REPLACE INTO photo_hashes
           (path, file_size, file_sha1, dhash, exif_dt, exif_make, exif_model,
            dup_of, dup_kind, indexed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()

    stats.update({"indexed": len(rows), "newly_indexed": todo, "duplicates": dup_total})
    return stats


def orphan_recheck(conn: sqlite3.Connection) -> int:
    """释放"保留者已经不存在/已失效"的重复标记。

    没有这一步会有个死结：原图被删（或重扫后变成无意义）之后，指向它的
    重复照片永远带着 dup_of，扫描一直跳过它，于是永久沉底、再也无法触及。
    每轮扫描开头跑一次，开销只是一条 GROUP BY。
    """
    cur = conn.cursor()
    try:
        dup_rows = cur.execute(
            "SELECT path, dup_of FROM photo_hashes WHERE dup_of IS NOT NULL").fetchall()
    except sqlite3.OperationalError:
        return 0          # 表还没建（从未跑过去重），没什么可释放的
    alive = {r[0]: (r[1], r[2]) for r in cur.execute(
        "SELECT path, memory_score, is_meaningful FROM photo_scores")}
    freed = 0
    for path, dup_of in dup_rows:
        parent = alive.get(dup_of)
        # 保留者没了，或它自己就是无意义/没打分的 → 这个"重复"没有意义了
        if parent is None or parent[0] is None or parent[1] == 0:
            cur.execute(
                "UPDATE photo_hashes SET dup_of = NULL, dup_kind = NULL WHERE path = ?",
                (path,))
            freed += 1
    if freed:
        conn.commit()
    return freed


def load_dup_map(conn: sqlite3.Connection) -> dict:
    """返回 {path: (dup_of, dup_kind)}，供扫描时跳过已判重的照片。"""
    try:
        return {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT path, dup_of, dup_kind FROM photo_hashes WHERE dup_of IS NOT NULL")}
    except sqlite3.OperationalError:
        return {}          # 表还没建（从未跑过去重）


def open_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """统一的 photos.db 连接（含 WAL 与锁等待）。

    db_path 省略时用本模块的 DB_PATH；调用方有自己的库路径时应显式传入，
    否则会静默连到错误的库（render_daily_photo 就属于这种情况）。

    WAL + busy_timeout 是必需的：从 v6 起 render_daily_photo.py 也会写库，
    photos.db 从此有两个并发写入进程，默认 5 秒的锁等待会在长扫描中途抛
    "database is locked"。WAL 让读不阻塞写；synchronous=NORMAL 是 WAL 下的
    推荐搭配（崩溃最多丢最后一个事务，不会损坏库文件）。
    """
    target = DB_PATH if db_path is None else db_path
    conn = sqlite3.connect(target, check_same_thread=False, timeout=30.0)
    for pragma in ("PRAGMA journal_mode=WAL",
                   "PRAGMA busy_timeout=30000",
                   "PRAGMA synchronous=NORMAL"):
        try:
            conn.execute(pragma)
        except sqlite3.DatabaseError:
            pass
    return conn


def _convert_gps_to_deg(value):
    try:
        d, m, s = value
        return float(d[0]) / float(d[1]) + float(m[0]) / float(m[1]) / 60.0 + float(s[0]) / float(s[1]) / 3600.0
    except Exception:
        return None


def read_gps_with_exiftool(path: Path):
    if not EXIFTOOL_AVAILABLE:
        return None
    try:
        result = subprocess.run(
            ["exiftool", "-n", "-json", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        # 没装 exiftool，则直接跳过
        return None
    except subprocess.CalledProcessError:
        return None

    try:
        data = json.loads(result.stdout)[0]
    except Exception:
        return None

    lat = data.get("GPSLatitude")
    lon = data.get("GPSLongitude")
    alt = data.get("GPSAltitude")
    if lat is None or lon is None:
        return None
    return {
        "lat": float(lat),
        "lon": float(lon),
        "alt": float(alt) if alt is not None else None,
    }


def read_exif_with_exiftool(path: Path) -> dict:
    """用 exiftool 读拍摄时间与机型（PIL 打不开 RAW 时的兜底）。

    RAW 是相机用户重复照片的大头（连拍、RAW+JPEG 同拍），而 read_exif 靠
    Image.open，对 DNG/CR2 这些直接返回空 —— 不补这条路，连拍与相似判重
    对 RAW 就完全失效。一次调用带全所需 tag，避免每张起多个子进程。
    """
    if not EXIFTOOL_AVAILABLE:
        return {}
    try:
        result = subprocess.run(
            ["exiftool", "-json", "-DateTimeOriginal", "-Make", "-Model", str(path)],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)[0]
    except Exception:
        return {}
    return {
        "datetime": data.get("DateTimeOriginal"),
        "make": (data.get("Make") or "").strip() or None,
        "model": (data.get("Model") or "").strip() or None,
    }


def read_exif(path: Path) -> dict:
    info: dict = {}
    try:
        img = Image.open(path)
        try:
            # 用 exif_transpose 得到"显示方向"（与 load_image_any 渲染一致），避免存储方向与实际不符
            disp = ImageOps.exif_transpose(img)
            width, height = disp.size
            info["width"] = int(width)
            info["height"] = int(height)
            if width > height:
                info["orientation"] = "landscape"
            elif height > width:
                info["orientation"] = "portrait"
            else:
                info["orientation"] = "square"
        except Exception:
            pass

        # 使用公共 API getexif()，兼容 JPEG / PNG / WebP / HEIC 等格式
        # （_getexif() 是 JPEG 专有的私有方法，HEIC 不支持）
        exif_obj = img.getexif()
        if not exif_obj:
            # 兜底：尝试 _getexif()（旧版 Pillow 或特殊格式）
            try:
                exif_raw = img._getexif() or {}
            except (AttributeError, Exception):
                exif_raw = {}
        else:
            exif_raw = dict(exif_obj)
    except Exception:
        return info

    exif = {}
    for tag_id, value in exif_raw.items():
        tag = ExifTags.TAGS.get(tag_id, tag_id)
        exif[tag] = value

    # 基本字段
    info["datetime"] = exif.get("DateTimeOriginal") or exif.get("DateTime")
    info["make"] = exif.get("Make")
    info["model"] = exif.get("Model")
    info["iso"] = exif.get("ISOSpeedRatings") or exif.get("PhotographicSensitivity")
    info["exposure_time"] = exif.get("ExposureTime")
    info["f_number"] = exif.get("FNumber")
    info["focal_length"] = exif.get("FocalLength")

    # 如果主 EXIF 中缺少 DateTimeOriginal，尝试从 ExifIFD 子 IFD 获取
    if not info.get("datetime"):
        try:
            exif_ifd = exif_obj.get_ifd(ExifTags.IFD.Exif)
            if exif_ifd:
                dt = exif_ifd.get(0x9003)  # DateTimeOriginal
                if dt:
                    info["datetime"] = dt
                if not info.get("iso"):
                    info["iso"] = exif_ifd.get(0x8827)  # ISOSpeedRatings
                if not info.get("exposure_time"):
                    info["exposure_time"] = exif_ifd.get(0x829A)  # ExposureTime
                if not info.get("f_number"):
                    info["f_number"] = exif_ifd.get(0x829D)  # FNumber
                if not info.get("focal_length"):
                    info["focal_length"] = exif_ifd.get(0x920A)  # FocalLength
        except Exception:
            pass

    # GPS 信息：优先从 get_ifd() 获取（兼容 HEIC），再降级到旧方式
    lat = lon = None

    # 方式 1：通过 get_ifd(GPSInfo) 获取（推荐，HEIC/JPEG 通用）
    try:
        gps_ifd = exif_obj.get_ifd(ExifTags.IFD.GPSInfo)
        if gps_ifd:
            gps_tags = {}
            for k, v in gps_ifd.items():
                name = ExifTags.GPSTAGS.get(k, k)
                gps_tags[name] = v

            lat_ref = gps_tags.get("GPSLatitudeRef")
            lat_raw = gps_tags.get("GPSLatitude")
            lon_ref = gps_tags.get("GPSLongitudeRef")
            lon_raw = gps_tags.get("GPSLongitude")

            if lat_raw and lat_ref:
                lat = _convert_gps_to_deg(lat_raw)
                if lat is not None and lat_ref in ["S", "s"]:
                    lat = -lat
            if lon_raw and lon_ref:
                lon = _convert_gps_to_deg(lon_raw)
                if lon is not None and lon_ref in ["W", "w"]:
                    lon = -lon
    except Exception:
        pass

    # 方式 2：降级到旧的 GPSInfo dict（某些 JPEG 可能走这条路径）
    if lat is None or lon is None:
        gps_info = exif.get("GPSInfo")
        if isinstance(gps_info, dict):
            gps_tags = {}
            for k, v in gps_info.items():
                name = ExifTags.GPSTAGS.get(k, k)
                gps_tags[name] = v

            lat_ref = gps_tags.get("GPSLatitudeRef")
            lat_raw = gps_tags.get("GPSLatitude")
            lon_ref = gps_tags.get("GPSLongitudeRef")
            lon_raw = gps_tags.get("GPSLongitude")

            if lat_raw and lat_ref:
                lat = _convert_gps_to_deg(lat_raw)
                if lat is not None and lat_ref in ["S", "s"]:
                    lat = -lat
            if lon_raw and lon_ref:
                lon = _convert_gps_to_deg(lon_raw)
                if lon is not None and lon_ref in ["W", "w"]:
                    lon = -lon

    info["gps_lat"] = lat
    info["gps_lon"] = lon

    if info.get("gps_lat") is None or info.get("gps_lon") is None:
        gps = read_gps_with_exiftool(path)
        if gps is not None:
            info["gps_lat"] = gps["lat"]
            info["gps_lon"] = gps["lon"]
            if gps.get("alt") is not None:
                info["gps_alt"] = gps["alt"]

    return info


def in_home(lat: float | None, lon: float | None) -> bool:
    """判断是否在“本地/常驻地”范围内。"""
    if lat is None or lon is None:
        return False
    try:
        d = haversine_km(float(lat), float(lon), float(HOME_LAT), float(HOME_LON))
        return d <= float(HOME_RADIUS_KM)
    except Exception:
        return False


def format_eta(seconds: float) -> str:
    if seconds <= 0:
        return "00:00:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


import csv
import math
from typing import Dict, List, Tuple, Optional

# (lat, lon, name_zh, name_en, population, is_city)
CityRecord = Tuple[float, float, str, str, int, int]

_CITY_CACHE_CITIES: List[CityRecord] | None = None
_CITY_CACHE_GRID: Dict[Tuple[int, int], List[int]] | None = None

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c

def grid_key(lat: float, lon: float) -> Tuple[int, int]:
    gx = int(math.floor(lat / CITY_GRID_DEG))
    gy = int(math.floor(lon / CITY_GRID_DEG))
    return gx, gy

def load_world_cities(csv_path: Path) -> Tuple[List[CityRecord], Dict[Tuple[int, int], List[int]]]:
    if not csv_path.exists():
        # 城市库只是 GPS 增强（给 VLM 喂"拍摄地点"），缺失时优雅降级：返回空城市库，
        # resolve() 会返回空地点串，绝不阻塞扫描。避免容器/降权环境下 FATAL 中断整个批次。
        print(f"[WARN] 城市索引文件不存在（{csv_path}），跳过地点信息，仅按无地点打分（不影响打分/出图）")
        return [], {}

    cities: List[CityRecord] = []
    grid_index: Dict[Tuple[int, int], List[int]] = {}

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                lat = float((row.get("lat") or "").strip())
                lon = float((row.get("lon") or "").strip())
            except Exception:
                continue
            name_en = (row.get("name_en") or "").strip()
            name_zh = (row.get("name_zh") or "").strip()
            try:
                pop = int((row.get("population") or "0").strip() or 0)
            except ValueError:
                pop = 0
            try:
                is_city = int((row.get("is_city") or "0").strip() or 0)
            except ValueError:
                is_city = 0
            cities.append((lat, lon, name_zh, name_en, pop, is_city))

    for idx, (lat, lon, name_zh, name_en, _pop, _is_city) in enumerate(cities):
        key = grid_key(lat, lon)
        grid_index.setdefault(key, []).append(idx)

    print(f"[INFO] 已加载中文城市库: {csv_path}")
    return cities, grid_index

def find_nearest_city(
    lat: float,
    lon: float,
    cities: List[CityRecord],
    grid_index: Dict[Tuple[int, int], List[int]],
    max_km: float = 80.0,
) -> Tuple[str, Optional[float]]:
    """返回 (城市名, 距离公里数)；附近没有城市时返回 ("", None)。"""
    if not cities:
        return ("", None)

    gx, gy = grid_key(lat, lon)

    def collect_candidates(radius: int) -> List[int]:
        cand: List[int] = []
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                bucket = grid_index.get((gx + dx, gy + dy))
                if bucket:
                    cand.extend(bucket)
        return cand

    candidates = collect_candidates(radius=1)
    if not candidates:
        candidates = collect_candidates(radius=2)
    if not candidates:
        return ("", None)

    dists: Dict[int, float] = {}
    best_dist = float("inf")
    for idx in candidates:
        city_lat, city_lon = cities[idx][0], cities[idx][1]
        d = haversine_km(lat, lon, city_lat, city_lon)
        dists[idx] = d
        if d < best_dist:
            best_dist = d

    if best_dist > max_km:
        return ("", None)

    nearest_idx = min(dists, key=lambda i: dists[i])
    if cities[nearest_idx][5] == 1:
        # 最近的点本身就是座正经城市（首都/省会/地级市，见 geonames feature code）
        # → 直接用。这里不能只看人口：上海的黄浦区有 30 万人，会把「上海」顶掉；
        # 而澳门作为特别行政区必须用它自己，不能被 8km 外的珠海盖过去。
        best_idx = nearest_idx
    else:
        # 最近的点只是街区/街道办/村镇（cities500 把一座城市拆成大量区/町级点，
        # 东京市中心最近的点叫「永福」、巴黎市中心是「Paris 04 Hôtel-de-Ville」），
        # 这时在「最近距离 + 缓冲」范围内挑人口最大的那个，人口并列时优先有中文名。
        near = [i for i, d in dists.items()
                if d <= min(best_dist + CITY_NEAR_BUFFER_KM, max_km)]
        best_idx = max(near, key=lambda i: (cities[i][4], 1 if cities[i][2] else 0, -dists[i]))

    name_zh, name_en = cities[best_idx][2], cities[best_idx][3]
    return (name_zh or name_en or "", dists[best_idx])

def get_city_resolver():
    global _CITY_CACHE_CITIES, _CITY_CACHE_GRID
    if _CITY_CACHE_CITIES is None or _CITY_CACHE_GRID is None:
        _CITY_CACHE_CITIES, _CITY_CACHE_GRID = load_world_cities(WORLD_CITIES_CSV)

    def resolve(lat: float | None, lon: float | None) -> str:
        if lat is None or lon is None:
            return ""
        name, km = find_nearest_city(lat, lon, _CITY_CACHE_CITIES, _CITY_CACHE_GRID,
                                     max_km=CITY_MAX_DISTANCE_KM)
        if not name:
            return ""
        if km is not None and km >= CITY_DIST_HINT_KM:
            return f"{name} {km:.0f}km"
        return name

    return resolve



# =======================
# 渠道负载均衡：遇错自动切换
# =======================
def _channel_usable(idx: int) -> bool:
    """该渠道是否参与降级：enabled 为 True 且 api_key 非空。"""
    ch = API_CHANNELS[idx]
    if not ch.get("enabled", True):
        return False
    key = str(ch.get("api_key", "") or "").strip()
    return bool(key)


def _reserve_next_channel(tried: set[int]) -> int | None:
    n = len(API_CHANNELS)
    now = time.monotonic()
    with _channel_lock:
        start = _channel_index % n
        ordered = [
            (start + i) % n
            for i in range(n)
            if ((start + i) % n) not in tried and _channel_usable((start + i) % n)
        ]

        ready_idle: list[int] = []
        ready_busy: list[int] = []
        cooling_idle: list[int] = []
        cooling_busy: list[int] = []

        for idx in ordered:
            cooling = _channel_cooldown_until[idx] > now
            busy = _channel_inflight[idx] > 0
            if not cooling and not busy:
                ready_idle.append(idx)
            elif not cooling:
                ready_busy.append(idx)
            elif not busy:
                cooling_idle.append(idx)
            else:
                cooling_busy.append(idx)

        candidates = ready_idle or ready_busy or cooling_idle or cooling_busy
        if not candidates:
            return None

        idx = candidates[0]
        _channel_inflight[idx] += 1
        return idx


def _release_channel(idx: int) -> None:
    with _channel_lock:
        if 0 <= idx < len(_channel_inflight) and _channel_inflight[idx] > 0:
            _channel_inflight[idx] -= 1


def _mark_channel_failure(idx: int, ch_label: str, reason: str) -> None:
    if CHANNEL_FAILOVER_COOLDOWN_SEC <= 0:
        return

    until = time.monotonic() + CHANNEL_FAILOVER_COOLDOWN_SEC
    with _channel_lock:
        if 0 <= idx < len(_channel_cooldown_until):
            _channel_cooldown_until[idx] = max(_channel_cooldown_until[idx], until)

    print(
        f"[WARN] 渠道 {ch_label} 进入冷却 {CHANNEL_FAILOVER_COOLDOWN_SEC:g} 秒：{reason}"
    )


def _mark_channel_success(idx: int) -> None:
    global _channel_index
    with _channel_lock:
        _channel_index = idx
        if 0 <= idx < len(_channel_cooldown_until):
            _channel_cooldown_until[idx] = 0.0


def _post_with_channel_fallback(
    payload_builder,
    timeout: float = TIMEOUT,
    response_parser=None,
) -> requests.Response | tuple:
    """依次尝试各渠道发送请求，遇到错误自动切换到下一个渠道。

    Args:
        payload_builder: callable(channel_dict) -> (url, headers, json_body)
        timeout: 请求超时秒数
        response_parser: 可选，callable(response) -> parsed_result。
            若提供，会在收到 2xx 后调用；若解析抛异常则视为失败并切换渠道。
            若未提供，直接返回 response 对象。
    Returns:
        response_parser 未提供时返回 requests.Response；
        提供时返回 response_parser 的返回值。
    Raises:
        RuntimeError: 所有渠道均请求失败
    """
    n = len(API_CHANNELS)
    if n == 0:
        raise RuntimeError("未配置任何 VLM 渠道（API_CHANNELS 为空）")

    last_error: str | None = None
    tried: set[int] = set()

    for _ in range(n):
        idx = _reserve_next_channel(tried)
        if idx is None:
            break
        tried.add(idx)
        ch = API_CHANNELS[idx]
        url, headers, body = payload_builder(ch)
        ch_label = ch.get("model_name", url)

        try:
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=timeout)
            except Exception as e:
                print(f"[WARN] 渠道 {ch_label} 请求异常：{e}，尝试下一个渠道")
                last_error = str(e)
                _mark_channel_failure(idx, ch_label, f"请求异常：{e}")
                continue

            if not resp.ok:
                print(f"[WARN] 渠道 {ch_label} 返回 HTTP {resp.status_code}，切换到下一个渠道")
                last_error = f"HTTP {resp.status_code}"
                _mark_channel_failure(idx, ch_label, f"HTTP {resp.status_code}")
                if DEBUG:
                    try:
                        _body_str = json.dumps(body, ensure_ascii=False)
                        # base64 内容太长，截断后打印
                        import re as _re
                        _body_debug = _re.sub(
                            r'("data:[^;]+;base64,)([A-Za-z0-9+/=]{200})[A-Za-z0-9+/=]+',
                            r'\1\2…<truncated>',
                            _body_str,
                        )
                        print(f"[DEBUG] 请求体（base64 已截断）:\n{_body_debug}")
                    except Exception:
                        pass
                    try:
                        print(f"[DEBUG] 响应体:\n{resp.text}")
                    except Exception:
                        pass
                continue

            # 2xx 成功，如果有 parser 则尝试解析
            if response_parser is not None:
                try:
                    parsed = response_parser(resp)
                except Exception as e:
                    print(f"[WARN] 渠道 {ch_label} 响应解析失败：{e}，切换到下一个渠道")
                    last_error = str(e)
                    _mark_channel_failure(idx, ch_label, f"响应解析失败：{e}")
                    continue
                _mark_channel_success(idx)
                return parsed

            # 无 parser，直接返回 response
            _mark_channel_success(idx)
            return resp
        finally:
            _release_channel(idx)

    # 所有渠道都失败了
    raise RuntimeError(
        f"所有 {n} 个渠道均请求失败（最后错误：{last_error}），请检查渠道配置"
    )


def call_vlm(image_path: Path, city_resolver=None) -> dict:
    try:
        img_b64 = encode_image_to_b64(image_path)
    except Exception as e:
        raise RuntimeError(f"读取图片失败：{e}")

    exif_info = read_exif(image_path)
    exif_json = json.dumps(exif_info, ensure_ascii=False, default=str)

    # —— GPS/地点上下文：把拍摄地点喂给大模型，让打分能结合"在哪拍的" ——
    gps_ctx = ""
    lat = exif_info.get("gps_lat")
    lon = exif_info.get("gps_lon")
    if lat is not None and lon is not None:
        city = ""
        if city_resolver is not None:
            try:
                city = city_resolver(lat, lon) or ""
            except Exception:
                city = ""
        away = not in_home(lat, lon)
        where = "旅行/异地拍摄" if away else "常住地范围"
        gps_ctx = (
            f"拍摄地点信息：位置城市约[{city or '未知'}]，经纬度约({float(lat):.5f}, {float(lon):.5f})，"
            f"判定为{where}。\n"
            "若为异地/旅行拍摄，请在 memory_score 中酌情体现“旅行意义”的加分。\n"
        )

    system_prompt = (
        "你是一个“个人相册照片评估助手”，擅长理解真实照片的内容，并从回忆价值和美观角度打分。\n"
        "你会收到一张照片（以 base64 形式提供），你的任务是：\n"
        "1）用中文详细描述照片内容（80~200 字），\n"
        "2）判断照片的大致类型：人物/孩子/猫咪/家庭/旅行/风景/美食/宠物/日常/文档/杂物/其他，一张照片可以有不止一个类型。\n"
        "3）给出 0~100 的“值得回忆度” memory_score（精确到一位小数），\n"
        "4）给出 0~100 的“美观程度” beauty_score（精确到一位小数），\n"
        "5）用简短中文 reason 解释原因（不超过 40 字）。\n\n"

        "【值得回忆度（memory_score）评分方法】\n"
        "请先按照值得回忆的程度，先确定照片的'得分区间'，再进行精调：\n"
        "如何判定值得回忆度（memory_score）的得分区间：\n"
        "- 垃圾/随手拍/无意义记录：40.0 分以下（常见为 0~25；若还能勉强辨认但无故事，也不要超过 39.9）。\n"
        "- 稍微有点可回忆价值：以 65.0 分为中心（大多落在 58.1~70.3）。\n"
        "- 不错的回忆价值：以 75 分为中心（大多落在 68.7~82.4）。\n"
        "- 特别精彩、强烈值得珍藏：以 85 分为中心（大多落在 79.1~95.9；\n"
        "如何继续精调memory_score得分（若同时符合几条加分项，加分可叠加）：\n"
        "- 人物与关系：画面中含有面积较大的人脸，有人物互动，或属于合影 → 大幅提高评分；\n"
        "- 事件性：生日/聚会/仪式/舞台/明显事件 → 少许提高评分；\n"
        "- 稀缺性与不可复现：明显“这一刻很难再来一次” → 大幅提高评分；\n"
        "- 情绪强度：笑、哭、惊喜、拥抱、互动、氛围强 → 少许提高评分；\n"
        "- 信息密度：画面能讲清楚发生了什么 → 微微提高评分；\n"
        "- 优美风景：画面中含有壮丽的自然风光，或精美、有秩序感的构图 → 少许提高评分；\n"
        "- 旅行意义：异地、地标、旅途情景 → 少许提高评分。\n\n"
        "- 画质：画面不清晰、模糊、有残影、虚焦 → 微微降低评分。\n\n"

        "【重点照片的处理】\n"
        "如果画面中含有：孩子/猫咪/宠物题材，这些主题更容易产生高回忆价值，请直接以75分为中心，并大幅提高评分”。\n"

        "【明显低价值图片的处理】\n"
        "对以下低价值图片，必须将 memory_score 压低到 0~25（最多不超过 39）。\n"
        "- 裸露、低俗、色情或违反公序良俗的图片。\n\n"
        "- 账单、收据、广告、随手拍的杂物、测试图片、屏幕截图等。\n\n"
        
        "【美观分（beauty_score）评分方法】\n"
        "美观分只评价视觉：构图、光线、清晰度、色彩、主体突出。\n"
        "不要被“孩子/猫/旅行”主题绑架美观分：主题不等于好看。\n"

        "请严格只输出 JSON，格式如下：\n"
        "{\n"
        "  \"caption\": \"……\",\n"
        "  \"type\": \"人物/家庭/旅行/…… 可以带多个type\",\n"
        "  \"memory_score\": 0.0-100.0 的数字, 精确到 1 位小数\n"
        "  \"beauty_score\": 0.0-100.0 的数字, 精确到 1 位小数\n"
        "  \"reason\": \"不超过 60 字的中文理由\"\n"
        "  \"meaningful\": true 或 false   照片是否值得拍进相框（低价值杂物/截图/收据/测试图=false）\n"
        "  \"crop\": {\"x\":0.5,\"y\":0.5,\"w\":0.8,\"h\":0.8}   主体/构图焦点的归一化区域(0~1)中心+宽高\n"
        "}\n"
        "说明：crop 中 x/y 是焦点区域中心（0=最左/最上，1=最右/最下），w/h 是该区域占整幅的比例。\n"
        "请把人物脸部、有趣主体尽量放在 crop 的中心，避免被裁掉。若照片无明确主体（如纯风景全景），用接近 0.5/0.5 居中即可。\n"
        "不要输出任何多余文字，不要加注释。"
    )

    user_text = (
        "下面是照片的内容，请结合图像本身完成上述任务。\n"
        f"{gps_ctx}"
    )

    def _build(ch):
        headers = {"Content-Type": "application/json"}
        key = ch.get("api_key", "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = {
            "model": ch["model_name"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{img_b64}"
                            },
                        },
                    ],
                },
            ],
            "temperature": 0.2,
            "max_tokens": 1024,
            "stream": False,
        }
        # MiniMax-M3 默认带 <think> 思考块，占 token；显式关闭推理更稳
        if _is_think_model(ch.get("model_name", "")):
            body["thinking"] = {"type": "disabled"}
        return ch["api_url"], headers, body

    def _parse_vlm_response(resp):
        """解析 VLM 响应，失败时抛异常以触发渠道切换。

        兼容不同模型的返回风格：
        - 纯 JSON（本地 VLM 常见）
        - 带 ```json ... ``` markdown 围栏（如 GLM、部分云端）
        - 带 <think>...</think> 思考块（MiniMax-M3 等推理模型）
        统一先剥离非 JSON 外壳，再 json.loads。
        """
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()

        # 1) 去掉 <think>...</think> 思考块
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
        # 2) 去掉 ```json ... ``` 或 ``` ... ``` markdown 代码块
        content = re.sub(r"```(?:json)?\s*(.*?)```", r"\1", content, flags=re.S)
        # 3) 若还有剩余围栏残留，兜底剥掉
        content = content.strip().strip("`").strip()

        obj = json.loads(content)
        return obj

    result = _post_with_channel_fallback(_build, timeout=TIMEOUT, response_parser=_parse_vlm_response)

    return result, exif_info


def _process_one_photo(path: Path, city_resolver) -> dict | None:
    """处理单张照片：调用 VLM + 生成文案 + 提取 EXIF。

    成功返回包含所有数据库字段的 dict，失败返回 None。
    此函数仅做计算 + 网络 IO（不写数据库），线程安全。
    """
    t_photo_start = time.perf_counter()
    try:
        result, exif_info = call_vlm(path, city_resolver)
    except Exception as e:
        print(f"[WARN] 调用模型失败: {e}")
        return None

    caption = str(result.get("caption", "")).strip()
    ptype = str(result.get("type", "")).strip()
    try:
        memory_score = float(result.get("memory_score", 0.0))
    except Exception:
        memory_score = 0.0
    try:
        beauty_score = float(result.get("beauty_score", 0.0))
    except Exception:
        beauty_score = 0.0
    reason = str(result.get("reason", "")).strip()

    # —— 无意义判定（模块3）：AI 判断 + memory_score 硬兜底 ——
    try:
        ai_meaningful = bool(result.get("meaningful", True))
    except Exception:
        ai_meaningful = True
    is_meaningful = ai_meaningful and (memory_score >= UNMEANINGFUL_THRESHOLD)

    # 归一化 AI crop（0~1，缺省回退居中）
    crop = normalize_crop(result.get("crop"))

    # 无意义照片跳过第 2 次 VLM（旁白），省一半 token
    side_caption = None
    if is_meaningful:
        side_caption = generate_side_caption(path)

    width = exif_info.get("width")
    height = exif_info.get("height")
    orientation = exif_info.get("orientation")

    exif_datetime = exif_info.get("datetime")
    exif_make = exif_info.get("make")
    exif_model_val = exif_info.get("model")

    def _to_int(v):
        try:
            return int(v) if v is not None else None
        except Exception:
            return None

    def _to_float(v):
        try:
            return float(v) if v is not None else None
        except Exception:
            return None

    exif_iso = _to_int(exif_info.get("iso"))
    exif_exposure_time = _to_float(exif_info.get("exposure_time"))
    exif_f_number = _to_float(exif_info.get("f_number"))
    exif_focal_length = _to_float(exif_info.get("focal_length"))
    exif_gps_lat = _to_float(exif_info.get("gps_lat"))
    exif_gps_lon = _to_float(exif_info.get("gps_lon"))
    exif_gps_alt = _to_float(exif_info.get("gps_alt"))

    if exif_gps_lat is not None and exif_gps_lon is not None:
        exif_city = city_resolver(exif_gps_lat, exif_gps_lon)
    else:
        exif_city = ""

    lat = exif_info.get("gps_lat")
    lon = exif_info.get("gps_lon")
    if lat is not None and lon is not None and not in_home(lat, lon):
        memory_score = min(memory_score + 5.0, 100.0)

    t_photo_end = time.perf_counter()

    return {
        "path": str(path),
        "caption": caption,
        "type": ptype,
        "memory_score": memory_score,
        "beauty_score": beauty_score,
        "reason": reason,
        "width": width,
        "height": height,
        "orientation": orientation,
        "exif_json": json.dumps(exif_info, ensure_ascii=False, default=str),
        "raw_json": json.dumps(result, ensure_ascii=False),
        "exif_datetime": exif_datetime,
        "exif_make": exif_make,
        "exif_model": exif_model_val,
        "exif_iso": exif_iso,
        "exif_exposure_time": exif_exposure_time,
        "exif_f_number": exif_f_number,
        "exif_focal_length": exif_focal_length,
        "exif_gps_lat": exif_gps_lat,
        "exif_gps_lon": exif_gps_lon,
        "exif_gps_alt": exif_gps_alt,
        "side_caption": side_caption,
        "exif_city": exif_city,
        "crop_x": crop["x"],
        "crop_y": crop["y"],
        "crop_w": crop["w"],
        "crop_h": crop["h"],
        "is_meaningful": 1 if is_meaningful else 0,
        "cost": t_photo_end - t_photo_start,
    }


def _save_result_to_db(cur, conn, rec: dict):
    """将一条处理结果写入数据库。

    ⚠️ INSERT OR REPLACE 在 path 这种主键表上等于【先 DELETE 再 INSERT】，整行
    由列清单重建 —— 所以任何"不由本次打分产生"的列（used_at / used_count，
    它们是渲染端写的出图记录）都必须用 COALESCE 子查询把旧值捞回来，否则
    用户点一次「重新扫描」，出图历史就被清空，选片会立刻重推同一批照片。
    今后再往这张表加这类列，记得同样处理。
    """
    cur.execute(
        """
        INSERT OR REPLACE INTO photo_scores
        (path, caption, type, memory_score, beauty_score, reason,
         width, height, orientation, used_at, used_count,
         exif_json, raw_json,
         exif_datetime, exif_make, exif_model,
         exif_iso, exif_exposure_time, exif_f_number, exif_focal_length,
         exif_gps_lat, exif_gps_lon, exif_gps_alt, side_caption, exif_city,
         crop_x, crop_y, crop_w, crop_h, is_meaningful)
        VALUES (?, ?, ?, ?, ?, ?,
                ?, ?, ?, COALESCE((SELECT used_at FROM photo_scores WHERE path = ?), NULL),
                COALESCE((SELECT used_count FROM photo_scores WHERE path = ?), NULL),
                ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?)
        """,
        (
            rec["path"],
            rec["caption"],
            rec["type"],
            rec["memory_score"],
            rec["beauty_score"],
            rec["reason"],
            rec["width"],
            rec["height"],
            rec["orientation"],
            rec["path"],          # ← COALESCE used_at 的子查询参数
            rec["path"],          # ← COALESCE used_count 的子查询参数
            rec["exif_json"],
            rec["raw_json"],
            rec["exif_datetime"],
            rec["exif_make"],
            rec["exif_model"],
            rec["exif_iso"],
            rec["exif_exposure_time"],
            rec["exif_f_number"],
            rec["exif_focal_length"],
            rec["exif_gps_lat"],
            rec["exif_gps_lon"],
            rec["exif_gps_alt"],
            rec["side_caption"],
            rec["exif_city"],
            rec["crop_x"],
            rec["crop_y"],
            rec["crop_w"],
            rec["crop_h"],
            rec["is_meaningful"],
        ),
    )
    conn.commit()


def _print_result(rec: dict):
    """打印单张照片处理结果摘要。"""
    print(f"  类型    ：{rec['type']}")
    print(f"  回忆分  ：{rec['memory_score']:.1f}")
    print(f"  美观分  ：{rec['beauty_score']:.1f}")
    if rec["side_caption"]:
        print(f"  一句话文案：{rec['side_caption']}")
    else:
        print("  一句话文案：(无)")
    print(f"  画面描述：{rec['caption']}")
    print(f"  理由    ：{rec['reason']}")
    print(f"  有意义  ：{'是' if rec.get('is_meaningful') else '否(跳过旁白/成品)'}")
    if rec.get("crop_x") is not None:
        print(f"  裁切焦点：(x={rec['crop_x']:.2f}, y={rec['crop_y']:.2f}, w={rec['crop_w']:.2f}, h={rec['crop_h']:.2f})")


def _generate_converted_for_photo(rec: dict) -> None:
    """对有意义照片生成各启用屏预裁切成品；无意义/无启用屏则跳过。"""
    if not rec.get("is_meaningful"):
        return
    if not SCREENS_ENABLED:
        return
    try:
        img = load_image_any(rec["path"]) if Path(rec["path"]).exists() else None
        if img is None:
            return
        crop = {
            "x": rec.get("crop_x", 0.5),
            "y": rec.get("crop_y", 0.5),
            "w": rec.get("crop_w", 0.8),
            "h": rec.get("crop_h", 0.8),
        }
        generate_converted_for_screens(img, rec["path"], crop, SCREENS_ENABLED, CONVERTED_DIR)
    except Exception as e:
        print(f"[WARN] 生成成品失败({rec.get('path')}): {e}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="分析照片并生成评分")
    parser.add_argument("--cache", action="store_true",
                        help="调试用途：缓存文件列表以跳过目录扫描；不适合生产同步")
    parser.add_argument("-j", "--concurrency", type=int, default=1,
                        help="并发处理线程数（默认 1，即串行处理）")
    parser.add_argument("--debug", action="store_true",
                        help="调试模式：请求失败时打印请求体和响应体")
    parser.add_argument("--batch-limit", type=int, default=None,
                        help="本次最多处理 N 张（覆盖 config.BATCH_LIMIT，供 Token 感知调度限流）")
    parser.add_argument("--rescan", action="store_true",
                        help="重新扫描：跳过 filter_unscored，对目录里所有照片（含已入库）重新打分（INSERT OR REPLACE 覆盖更新）")
    parser.add_argument("--dedupe-only", action="store_true",
                        help="只建立/更新去重索引并标记重复项，不调用 VLM 打分（可独立 cron 调度）")
    parser.add_argument("--dedupe-reindex", action="store_true",
                        help="配合 --dedupe-only：忽略已有索引全部重算（默认只补未索引/文件已变的）")
    args = parser.parse_args()

    # 应用 Web 设置页保存的动态配置（API key / 屏幕 / 阈值），并重建渠道状态
    try:
        import web_settings
        web_settings.apply_to_config(cfg)
        web_settings.rebuild_channels(sys.modules[__name__])
    except Exception as _e:
        print(f"[WARN] 应用 web_settings 失败：{_e}，仍按 config.py 运行")

    # 重新读启用屏列表 + 无意义阈值（设置页可能改了 SCREENS / CONVERTED_DIR / UNMEANINGFUL）
    global SCREENS, SCREENS_ENABLED, CONVERTED_DIR, UNMEANINGFUL_THRESHOLD, VLM_MAX_LONG_EDGE
    _sr = getattr(cfg, "SCREENS", None) or getattr(cfg, "SCREEN", None) or {}
    SCREENS = [_sr] if isinstance(_sr, dict) else [x for x in _sr if isinstance(x, dict)]
    SCREENS_ENABLED = [
        s for s in SCREENS if s.get("enabled", True) and int(s.get("width", 0)) > 0 and int(s.get("height", 0)) > 0
    ]
    _cd = Path(str(getattr(cfg, "CONVERTED_DIR", "./converted") or "./converted")).expanduser()
    CONVERTED_DIR = _cd.resolve() if _cd.is_absolute() else (ROOT_DIR / _cd).resolve()
    UNMEANINGFUL_THRESHOLD = float(getattr(cfg, "UNMEANINGFUL_THRESHOLD", 40.0) or 40.0)
    VLM_MAX_LONG_EDGE = int(getattr(cfg, "VLM_MAX_LONG_EDGE", 1024) or 1024)  # encode_image_to_b64 用，不同步就永远用旧值
    if SCREENS_ENABLED:
        print(f"[INFO] 启用成品屏：{[s.get('name') for s in SCREENS_ENABLED]}")

    global DEBUG
    DEBUG = args.debug
    if DEBUG:
        print("[INFO] 调试模式已启用")

    concurrency = max(1, args.concurrency)
    if concurrency > 1:
        print(f"[INFO] 并发模式：{concurrency} 个工作线程")

    # 临时文件写到"数据目录"（Docker 里 CONVERTED_DIR.parent=/app/data，已 chown 给降权用户），
    # 不能写 ROOT_DIR(/app)——那是 root 所有，降权用户 Permission denied。
    tmp_dir = CONVERTED_DIR.parent if str(CONVERTED_DIR.parent) else ROOT_DIR
    tmp_dir.mkdir(parents=True, exist_ok=True)
    filelist_path = tmp_dir / "filelist.txt"
    cache_path = tmp_dir / ".filelist_cache.txt"

    if args.cache:
        print("[WARN] --cache 仅建议用于调试提速，不适合生产环境。")
        print("[WARN] 使用缓存会跳过目录重扫：新增照片不会被发现，已删除照片的旧记录也可能保留在数据库中。")

    if args.cache and cache_path.exists():
        print(f"[INFO] 读取缓存文件列表：{cache_path}")
        cached = cache_path.read_text(encoding="utf-8").strip().splitlines()
        imgs = [Path(p) for p in cached if p.strip()]
        print(f"[INFO] 从缓存加载 {len(imgs)} 个文件。")
    else:
        print("[INFO] 正在扫描图片目录……")
        imgs = list_images()
        if args.cache:
            cache_path.write_text("\n".join(str(p) for p in imgs), encoding="utf-8")
            print(f"[INFO] 已写入缓存文件：{cache_path}")

    filelist_path.write_text("\n".join(str(p) for p in imgs), encoding="utf-8")
    print(f"[INFO] 已更新文件列表 filelist.txt，共 {len(imgs)} 个文件。")
    if not imgs:
        raise SystemExit(f"目录下没有图片文件: {IMAGE_DIR}")

    imgs = [p for p in imgs if not is_ignored(p)]
    if not imgs:
        raise SystemExit("[INFO] 所有图片都被忽略规则（截图/NAS 缩略图目录）排除了，没有可处理的图片。")

    conn = open_db()
    ensure_table(conn)
    city_resolver = get_city_resolver()

    # =======================
    # 同步删除：NAS/磁盘上已不存在的文件，也从数据库里删除
    # 只处理当前 IMAGE_DIR 前缀下的记录，避免误删其它历史路径。
    # =======================
    image_dir_prefix = str(IMAGE_DIR)

    try:
        # 用临时表避免 IN (...) 过长导致的 SQLite 参数上限问题
        conn.execute("DROP TABLE IF EXISTS _temp_existing_paths")
        conn.execute("CREATE TEMP TABLE _temp_existing_paths (path TEXT PRIMARY KEY)")

        # 批量插入当前扫描到的文件列表
        CHUNK = 2000
        total_files = len(imgs)
        inserted = 0
        for i in range(0, total_files, CHUNK):
            chunk = imgs[i : i + CHUNK]
            conn.executemany(
                "INSERT OR IGNORE INTO _temp_existing_paths(path) VALUES (?)",
                [(str(p),) for p in chunk],
            )
            inserted += len(chunk)
            if inserted % 10000 == 0:
                print(f"[CLEAN] 已写入存在文件清单：{inserted}/{total_files} …")

        # 删除：数据库里有记录，但磁盘上已不存在的文件
        cur_clean = conn.cursor()
        before_cnt = cur_clean.execute(
            "SELECT COUNT(*) FROM photo_scores WHERE path LIKE ?",
            (image_dir_prefix + "%",),
        ).fetchone()[0]

        cur_clean.execute(
            """
            DELETE FROM photo_scores
            WHERE path LIKE ?
              AND NOT EXISTS (
                    SELECT 1 FROM _temp_existing_paths t
                    WHERE t.path = photo_scores.path
              )
            """,
            (image_dir_prefix + "%",),
        )
        deleted = cur_clean.rowcount if cur_clean.rowcount is not None else 0
        conn.commit()

        after_cnt = cur_clean.execute(
            "SELECT COUNT(*) FROM photo_scores WHERE path LIKE ?",
            (image_dir_prefix + "%",),
        ).fetchone()[0]

        if deleted > 0:
            print(f"[CLEAN] 已同步删除 {deleted} 条数据库残留记录（当前目录：{before_cnt} → {after_cnt}）。")
        else:
            print("[CLEAN] 数据库与磁盘文件一致，无需清理。")

    except Exception as e:
        # 清理失败不应影响主流程
        print(f"[WARN] 同步清理数据库残留记录失败（已忽略，不影响主流程）：{e}")

    cur_test = conn.cursor()
    # 只统计当前 IMAGE_DIR 下的已分析照片，避免数据库里其它路径/历史残留影响进度计算
    counted = cur_test.execute(
        "SELECT COUNT(*) FROM photo_scores WHERE path LIKE ?",
        (image_dir_prefix + "%",),
    ).fetchone()[0]
    print(f"[INFO] 数据库中已有 {counted} 张已分析照片（仅统计当前目录）。")

    # ---------- 只做去重：独立任务，不碰 VLM、不烧额度 ----------
    # 单独成一个模式（而不是并进扫描主流程）的理由：首次全量建索引要读遍 NAS 上
    # 每个文件（8000 张可能要几十分钟），塞进扫描里会跟 token 调度的窗口抢时间，
    # 而且一旦被 batch_limit 之类截断，索引就残缺了。
    if args.dedupe_only:
        freed = orphan_recheck(conn)
        if freed:
            print(f"[INFO] 已释放 {freed} 条失效的重复标记（其保留者已删除或已失效）")
        print(f"[INFO] 开始建立去重索引，共 {len(imgs)} 张……")
        report_scan_progress(0, len(imgs), phase="dedupe", current="", detail="建立去重索引")
        t_dedupe = time.time()

        def _dedupe_progress(done: int, total_: int, phase: str) -> None:
            report_scan_progress(done, total_, phase="dedupe", current="",
                                 detail=f"{phase} {done}/{total_}")

        stats = run_dedupe(
            conn, imgs,
            enable_similar=bool(getattr(cfg, "DEDUPE_SIMILAR_ENABLED", True)),
            hamming_max=int(getattr(cfg, "DEDUPE_HAMMING_MAX", 6)),
            enable_burst=bool(getattr(cfg, "DEDUPE_BURST_ENABLED", True)),
            burst_gap=int(getattr(cfg, "DEDUPE_BURST_GAP_SEC", 3)),
            reindex=bool(args.dedupe_reindex),
            progress_cb=_dedupe_progress,
        )
        print(f"[OK] 去重索引完成，用时 {time.time() - t_dedupe:.1f}s："
              f"本次新算 {stats['newly_indexed']} 张 / 共索引 {stats['indexed']} 张 / "
              f"标记重复 {stats['duplicates']} 张 "
              f"（内容相同 {stats['exact']}、连拍 {stats['burst']}、视觉相似 {stats['similar']}）")
        conn.close()
        clear_scan_progress()
        return

    # 重新扫描：跳过 filter_unscored，全量重扫（含已入库）；否则只扫未入库的新照片
    is_rescan = bool(args.rescan) or (os.environ.get("INKTIME_RESCAN") == "1")
    if is_rescan:
        target_paths = list(imgs)
        print(f"[INFO] 【重新扫描】将重扫全部 {len(target_paths)} 张（含已打分，INSERT OR REPLACE 覆盖更新）")
    else:
        target_paths = filter_unscored(conn, imgs)
        if not target_paths:
            print("[INFO] 所有图片都已经在 photo_scores 中有记录。")
            conn.close()
            return

    # 跳过已判定为重复的照片 —— 这正是去重省 token 的地方。
    # 重扫（--rescan）也同样跳过：否则重扫会把每一张重复照片重新送一遍 VLM，
    # 去重就完全白做了。要让某张重复照片重新参与，重算去重索引即可。
    # 放在 batch 截断【之前】：重复照片不该占用本批的额度名额。
    dup_map = load_dup_map(conn)
    if dup_map:
        before_dup = len(target_paths)
        target_paths = [p for p in target_paths if str(p) not in dup_map]
        skipped_dup = before_dup - len(target_paths)
        if skipped_dup:
            print(f"[INFO] 已跳过 {skipped_dup} 张判定为重复的照片（不送 VLM 打分，省额度）")

    if args.batch_limit is not None and args.batch_limit > 0:
        target_paths = target_paths[:args.batch_limit]
    elif os.environ.get("INKTIME_BATCH_LIMIT"):
        try:
            _env_batch = int(os.environ.get("INKTIME_BATCH_LIMIT"))
            if _env_batch > 0:
                target_paths = target_paths[:_env_batch]
        except Exception:
            pass
    elif BATCH_LIMIT is not None:
        target_paths = target_paths[:BATCH_LIMIT]

    # 进度条口径：以“本次启动时的快照”为准。
    # total = 已分析(当前目录) + 本次待处理（filter_unscored 产生的目标集合）
    # 重扫时已分析不叠加（全量重扫），避免 7000+7000 的错位。
    already_done = 0 if is_rescan else counted
    total = already_done + len(target_paths)
    print(f"[INFO] 本次准备处理 {len(target_paths)} 张图片（快照总数 {total}，已分析 {already_done}）。")
    # 尽早上报一次初始进度，让网页立刻显示"正在扫描"，而不是等第一张处理完才冒出来
    report_scan_progress(already_done, total, phase="prepare", current="", detail=f"共 {len(target_paths)} 张待处理")

    cur = conn.cursor()
    # 用 RLock 而非 Lock：当前没有嵌套加锁，但一旦将来在持锁区间里调用另一个
    # 也会加锁的辅助函数，Lock 会直接死锁且没有任何提示；RLock 成本为零。
    db_lock = threading.RLock()   # 保护 SQLite 写入操作
    start_time = time.time()

    if concurrency <= 1:
        # ==================== 串行模式（原有逻辑）====================
        for idx, path in enumerate(target_paths, start=1):
            t_photo_start = time.perf_counter()
            sep = "=" * 60
            print("\n" + sep)
            print(f"[{idx}/{len(target_paths)}] 处理: {path}")

            rec = _process_one_photo(path, city_resolver)
            if rec is None:
                continue

            _print_result(rec)
            _save_result_to_db(cur, conn, rec)
            _generate_converted_for_photo(rec)

            t_photo_end = time.perf_counter()
            total_cost = t_photo_end - t_photo_start

            processed_now = already_done + idx
            denom = total if total > 0 else 1
            progress = max(0.0, min(1.0, processed_now / denom))

            bar_width = 30
            filled = int(bar_width * progress)
            bar = "\u2588" * filled + "\u2591" * (bar_width - filled)

            elapsed = time.time() - start_time
            avg_per = elapsed / idx if idx > 0 else 0
            remaining = max(total - processed_now, 0)
            eta = format_eta(remaining * avg_per) if avg_per > 0 else "00:00:00"

            print(f"[进度] {bar} {progress*100:5.1f}%  {processed_now}/{total}  本张耗时 {total_cost:4.1f}s  预计剩余 {eta} ")
            report_scan_progress(processed_now, total, phase="analyze", current=str(path), detail=f"耗时 {total_cost:.1f}s")
    else:
        # ==================== 并发模式 ====================
        print_lock = threading.Lock()
        completed_count = 0
        completed_lock = threading.Lock()

        def _worker(idx: int, path: Path) -> tuple[int, Path, dict | None]:
            return idx, path, _process_one_photo(path, city_resolver)

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(_worker, idx, path): (idx, path)
                for idx, path in enumerate(target_paths, start=1)
            }

            for future in as_completed(futures):
                idx, path, rec = future.result()

                with completed_lock:
                    completed_count += 1
                    done_so_far = completed_count

                with print_lock:
                    sep = "=" * 60
                    print("\n" + sep)
                    print(f"[{done_so_far}/{len(target_paths)}] 完成: {path}")

                    if rec is not None:
                        _print_result(rec)
                        with db_lock:
                            _save_result_to_db(cur, conn, rec)
                        _generate_converted_for_photo(rec)
                    else:
                        print("  (处理失败，已跳过)")

                    processed_now = already_done + done_so_far
                    denom = total if total > 0 else 1
                    progress = max(0.0, min(1.0, processed_now / denom))

                    bar_width = 30
                    filled = int(bar_width * progress)
                    bar = "\u2588" * filled + "\u2591" * (bar_width - filled)

                    elapsed = time.time() - start_time
                    avg_per = elapsed / done_so_far if done_so_far > 0 else 0
                    remaining = max(total - processed_now, 0)
                    eta = format_eta(remaining * avg_per) if avg_per > 0 else "00:00:00"

                    cost_str = f"{rec['cost']:4.1f}s" if rec else "N/A"
                    print(f"[进度] {bar} {progress*100:5.1f}%  {processed_now}/{total}  本张耗时 {cost_str}  预计剩余 {eta} ")
                    report_scan_progress(processed_now, total, phase="analyze", current=str(path), detail=f"耗时 {cost_str}")

    conn.close()
    print("\n[完成] 本批次处理完成。")
    clear_scan_progress()


if __name__ == "__main__":
    require_exiftool()
    main()
