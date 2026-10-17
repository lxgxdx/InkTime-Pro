#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
每日相册渲染脚本：
- 从 photos.db / photo_scores 中选出一张“历史上的今天”照片
- 按 InkTime 模拟器的布局渲染到 480x800
- 用 LXGWHeartSerifMN.ttf 把文案 / 日期 / 地点都画到图上
- 转成四色墨水屏（黑/白/红/黄）图像，并保存为 BIN（1 字节 1 像素，行优先）
- 同时导出 latest.h 头文件数组，给 ESP32 直接 include
"""

from __future__ import annotations

from pathlib import Path
import argparse
import sqlite3
import json
import datetime as dt
import os
from typing import List, Dict, Any, Tuple, Optional
from PIL import Image, ImageDraw, ImageFont, ImageOps
import config as cfg
from image_utils import load_image_any, register_heif
from converter import (crop_with_ai_box, layout_band_height, layout_scale,
                       load_converted_or_crop, orientation_dims, render_orientation)
register_heif()

# 渲染子进程启动即应用设置页（改屏/阈值生效），保证 SCREENS 快照拿的是最新值
try:
    import web_settings
    web_settings.apply_to_config(cfg)
except Exception as _e:
    print(f"[WARN] 应用 web_settings 失败：{_e}，按 config.py 运行")


TODAY = dt.date.today()

# === 路径配置（来自 config.py） ===
ROOT_DIR = Path(__file__).resolve().parent

DB_PATH = Path(str(getattr(cfg, "DB_PATH", "photos.db") or "photos.db")).expanduser()
if not DB_PATH.is_absolute():
    DB_PATH = (ROOT_DIR / DB_PATH).resolve()

BIN_OUTPUT_DIR = Path(str(getattr(cfg, "BIN_OUTPUT_DIR", "output/inktime") or "output/inktime")).expanduser()
if not BIN_OUTPUT_DIR.is_absolute():
    BIN_OUTPUT_DIR = (ROOT_DIR / BIN_OUTPUT_DIR).resolve()
BIN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

FONT_PATH = Path(str(getattr(cfg, "FONT_PATH", "") or "")).expanduser()
if str(FONT_PATH) and not FONT_PATH.is_absolute():
    FONT_PATH = (ROOT_DIR / FONT_PATH).resolve()

# ---- 中文字体自动探测（FONT_PATH 为空时用容器/本机内建 CJK，避免 load_default 无中文乱码） ----
_FONT_PATH_CACHE = None   # None=未探测 / str=命中路径 / ""=探测完成但无可用中文字体

def _resolve_cjk_font_path():
    """按优先级找一个可用的中文字体文件，返回路径字符串或 None。结果缓存在模块级。

    顺序：FONT_PATH(用户指定,须 is_file) → /app/fonts + ./fonts → 容器 NotoSansCJK(Reg/Bold)
          → glob 容器 /usr/share/fonts/**/*CJK* → Windows msyh/msyhbd/simhei/simsun → DejaVu(拉丁兜底)。
    注意用 is_file() 而非 exists()：FONT_PATH="" → Path("")=Path(".") 解析成目录，exists() 为 True 会误命中。
    """
    global _FONT_PATH_CACHE
    if _FONT_PATH_CACHE is not None:
        return _FONT_PATH_CACHE or None
    cands: list[Path] = []
    # 0) 用户显式 FONT_PATH（必须是文件）
    if str(FONT_PATH) and FONT_PATH.is_file():
        cands.append(FONT_PATH)
    # 1) 用户挂载 /app/fonts + 仓库 ./fonts
    for base in (Path("/app/fonts"), (ROOT_DIR / "fonts")):
        try:
            if base.is_dir():
                cands += [p for p in sorted(base.iterdir()) if p.suffix.lower() in (".ttc", ".ttf", ".otf")]
        except Exception:
            pass
    # 2) 容器 fonts-noto-cjk（Debian 落地路径）
    cands += [Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
              Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")]
    # 3) glob 兜底发现其它 CJK
    try:
        for p in Path("/usr/share/fonts").rglob("*CJK*"):
            if p.is_file() and p.suffix.lower() in (".ttc", ".ttf", ".otf"):
                cands.append(p)
    except Exception:
        pass
    # 4) Windows 本机
    cands += [Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/msyhbd.ttc"),
              Path("C:/Windows/Fonts/simhei.ttf"), Path("C:/Windows/Fonts/simsun.ttc")]
    # 5) 拉丁兜底（无中文但比 load_default 干净，用于纯数字/日期/地名行）
    cands += [Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]
    for p in cands:
        if not p.is_file():
            continue
        try:
            ImageFont.truetype(str(p), 14)   # 校验 Pillow 能打开（.ttc 取首个 face）
            _FONT_PATH_CACHE = str(p)
            return _FONT_PATH_CACHE
        except Exception:
            continue
    _FONT_PATH_CACHE = ""
    return None


def _get_font(size):
    """返回可用字体（优先 CJK，找不到回退 load_default）。"""
    path = _resolve_cjk_font_path()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()

MEMORY_THRESHOLD = float(getattr(cfg, "MEMORY_THRESHOLD", 70.0) or 70.0)
DAILY_PHOTO_QUANTITY = int(getattr(cfg, "DAILY_PHOTO_QUANTITY", 5) or 5)
# 选片时优先跳过"最近 N 天已出过图"的照片（软排除：候选不够会自动放宽）。
# 目的：避免连着几天推同一批 —— 周年纪念那天照片本来就少，没有这个过滤会天天重样。
RECENT_EXCLUDE_DAYS = int(getattr(cfg, "RECENT_EXCLUDE_DAYS", 30) or 30)

# 最近出过图的路径滚动记录（"换一张"的排除集用它，比翻 manifest 更可靠：
# 与屏数/方向无关，损坏时只需当作"没有排除项"，不会算错）
ROTATION_FILE = BIN_OUTPUT_DIR / ".rotation.json"
ROTATION_KEEP = max(10, DAILY_PHOTO_QUANTITY * 3)

# === 屏幕参数（来自 config.py 的 SCREENS 列表，多分辨率并存） ===
# 见 config-example.py 里的 SCREENS 说明。每个启用屏会各出一套成品；第一个启用屏为默认屏。
_SCREENS = getattr(cfg, "SCREENS", None) or ([getattr(cfg, "SCREEN", {})] if getattr(cfg, "SCREEN", None) else [])
if isinstance(_SCREENS, dict):
    _SCREENS = [_SCREENS]
SCREENS = [dict(s) for s in _SCREENS if isinstance(s, dict)]
# 向后兼容：旧的单屏场景若没有 SCREENS，用 SCREEN 兜底
if not SCREENS:
    SCREENS = [dict(getattr(cfg, "SCREEN", {"width": 480, "height": 800}))]
# 只保留启用且尺寸合法的屏
ENABLED_SCREENS = [
    s for s in SCREENS
    if s.get("enabled", True) and int(s.get("width", 0)) > 0 and int(s.get("height", 0)) > 0
]
DEFAULT_SCREEN = ENABLED_SCREENS[0] if ENABLED_SCREENS else SCREENS[0]

# 兼容旧调用：模块级的 CANVAS_WIDTH/HEIGHT 等，指向默认屏（sim 页等单屏场景用）
SCREEN = DEFAULT_SCREEN
CANVAS_WIDTH = int(SCREEN.get("width", 480))
CANVAS_HEIGHT = int(SCREEN.get("height", 800))
_canvas_h_ratio = (CANVAS_HEIGHT / 800.0)
TEXT_AREA_HEIGHT = int(round(float(SCREEN.get("text_area_height", 100 * _canvas_h_ratio))))
PALETTE = [tuple(c) for c in SCREEN.get("palette", [(0, 0, 0), (255, 255, 255), (200, 0, 0), (220, 180, 0)])]
BIN_FORMAT = str(SCREEN.get("bin_format", "1byte_per_px"))

# 成品目录（从 config 读，可用于预裁切基图复用）
CONVERTED_DIR = Path(str(getattr(cfg, "CONVERTED_DIR", "converted") or "converted")).expanduser()
if not CONVERTED_DIR.is_absolute():
    CONVERTED_DIR = (ROOT_DIR / CONVERTED_DIR).resolve()

# 屏幕参数 → 渲染函数配参（避免模块级常量写死）
def _screen_params(screen: dict, orient: Optional[str] = None) -> tuple:
    """返回 (canvas_w, canvas_h, text_area_h, palette, bin_format) 供单屏渲染。

    orient: 照片方向（"landscape"/"portrait"/"square"）。landscape(横图) 时对调宽高（横用），
            否则保持屏幕默认（竖用）。text_area_h 按画布高等比缩放，横摆时条带比例一致。
    """
    screen = dict(screen or {})
    canvas_w, canvas_h = orientation_dims(screen, orient)   # landscape→(h,w) 对调
    # 文字带高度由画布短边自动推导（见 _band_height）。config 里的 text_area_height
    # 从 v11 起只作历史兼容保留，不再参与计算 —— 写死的数字没法同时适配两种分辨率。
    ta = _band_height(canvas_w, canvas_h)
    pal = [tuple(c) for c in screen.get("palette", [(0, 0, 0), (255, 255, 255), (200, 0, 0), (220, 180, 0)])]
    bfmt = str(screen.get("bin_format", "1byte_per_px"))
    return canvas_w, canvas_h, ta, pal, bfmt


# === 版式尺度 ===
# 整套版式以「480 宽的竖屏」为 1 倍基准，所有尺寸都乘同一个 scale ——
# 这样 480×800 和 1200×1600 上看到的字/留白/图标，**物理比例一致**。
# 之前字号写死 22/20，7.09"（1200 宽）上只有 1.8% 画宽，看着像蚂蚁（2026-10-17 修）。
#
# scale 取画布**短边** /480：竖用取宽、横用取高。横用时不至于因为画布很宽就把字撑爆。
PAD_X_UNIT = 22           # 左右留白（scale=1）
CAPTION_FONT_UNIT = 22    # 文案字号（scale=1）
META_FONT_UNIT = 18       # 日期/地点字号（scale=1）
CAPTION_LINE_UNIT = 27    # 文案行高（scale=1）

# 电量图标槽（右下角）。相框固件会在屏上画一个电池图标 + 百分比，这里只留白并
# 把坐标写进 manifest.json 的 battery_slot。槽高 = 日期字号，固件按槽高推导
# 整组尺寸（见 _battery_group_width / overlay.cpp）。


def _layout_scale(cw: int, ch: int) -> float:
    """版式缩放系数（实现在 converter，预裁切和渲染共用同一个值）。"""
    return layout_scale(cw, ch)


def _band_height(cw: int, ch: int) -> int:
    """文字带高度。只由画布短边决定 —— 与照片方向无关，横竖摆同一块屏观感一致。"""
    return layout_band_height(cw, ch)


def _layout_metrics(cw: int, ch: int) -> dict:
    """文字带的全部版式参数（一次算清，画文字和定位电量槽都用它）。

    旧版把行高/第二行位置各算一遍，横用时字号没算进去 —— 日期直接被裁出画布。
    """
    s = _layout_scale(cw, ch)
    band = _band_height(cw, ch)
    pad_x = max(8, int(round(PAD_X_UNIT * s)))
    font_caption = max(12, int(round(CAPTION_FONT_UNIT * s)))
    font_meta = max(10, int(round(META_FONT_UNIT * s)))
    line_h = max(font_caption + 2, int(round(CAPTION_LINE_UNIT * s)))
    caption_top = ch - band + max(6, int(round(10 * s)))
    # 日期/地点行贴底：距画布底边留 12*s 的呼吸位
    meta_top = ch - max(8, int(round(12 * s))) - font_meta
    return {
        "scale": s,
        "band": band,
        "band_top": ch - band,
        "pad_x": pad_x,
        "font_caption": font_caption,
        "font_meta": font_meta,
        "line_h": line_h,
        "caption_top": caption_top,
        "meta_top": meta_top,
    }


# ========== 电量图标槽 ==========
# 固件按 d = 数字的**实际字形高度**画一个 2:1 的电池外形，里面的 5×8 点阵百分比
# 也用同样的高度 —— 这样图标和「2022.10.17」看起来一样高。
#
# ⚠️ 字号 ≠ 字形高：45px 的字体，数字实际只有约 35px（0.78×）。按字号定槽高
#    会让电池明显比文字高一截（用户 2026-10-17 指出）。所以这里真的去量 bbox。
# 这里的公式必须和 firmware/src/overlay.cpp 保持一致（那边是唯一的执行者）。

_VISUAL_METRICS_CACHE: dict[tuple[int, str], tuple[int, int]] = {}


def _visual_metrics(font_size: int, kind: str) -> tuple[int, int]:
    """返回 (字形高, 字形顶相对绘制原点的偏移)，按 (字号, 种类) 缓存。

    kind="cjk" 量中文（"深圳市"），kind="digit" 量数字（"0123456789"）。
    同一字号下中文比数字高约 20%（45px 字：中文 44、数字 35）—— 电池外壳跟中文
    对齐、里面的百分比跟数字对齐，整组看起来才和那一行文字一样高。
    """
    key = (font_size, kind)
    if key in _VISUAL_METRICS_CACHE:
        return _VISUAL_METRICS_CACHE[key]
    sample = "深圳市" if kind == "cjk" else "0123456789"
    ratio = 0.95 if kind == "cjk" else 0.78
    try:
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        bbox = probe.textbbox((0, 0), sample, font=_get_font(font_size))
        h = max(6, int(bbox[3] - bbox[1]))
        top = int(bbox[1])
    except Exception:
        h = max(6, int(round(font_size * ratio)))
        top = int(round(font_size * (1 - ratio) * 0.35))
    _VISUAL_METRICS_CACHE[key] = (h, top)
    return h, top


# 电量图标相对「数字字形高」的整体缩放。1.0 = 和日期一样高；用户 2026-10-18 说
# 那样还是太显眼，改成 0.7 —— 外壳和百分比一起等比缩小，不再追求和文字同高。
BATTERY_ICON_SCALE = 0.7


def _battery_group_width(body_h: int, glyph_h: int) -> int:
    """图标组（电池 + 凸点 + 缝 + "100%" 四个字）的总宽。"""
    body_w = 2 * body_h
    cap_w = max(1, int(round(body_h * 3 / 14)))
    gap = max(2, int(round(body_h * 4 / 14)))
    glyph_w = max(3, int(round(glyph_h * 5 / 8)))
    adv = glyph_w + max(1, int(round(glyph_w / 5)))
    return body_w + cap_w + gap + 4 * adv


def _battery_slot(cw: int, ch: int) -> dict:
    """右下角电量槽（固件在这块白底上画图标 + 百分比）。

    - 外壳和百分比**一起**按数字字形高的 0.7 倍缩小（BATTERY_ICON_SCALE）
    - 竖直居中在数字字形上；槽右边缘贴齐页面右边距
    - 槽宽按 "100%" 的最宽情况算，固件在里面右对齐（见 overlay.cpp）

    2026-10-18：先试过按中文字高（44px）、再试过按数字字高（35px），用户都觉得
    "太显眼"；最终不再和文字比高度，直接在数字字高基础上等比例缩到 0.7。
    """
    lm = _layout_metrics(cw, ch)
    digit_h, digit_top = _visual_metrics(lm["font_meta"], "digit")
    body_h = max(8, int(round(digit_h * BATTERY_ICON_SCALE)))
    glyph_h = body_h
    w = _battery_group_width(body_h, glyph_h)
    x = cw - lm["pad_x"] - w
    # 竖直中心对齐数字字形中心
    y = lm["meta_top"] + digit_top + (digit_h - body_h) // 2
    x = max(0, x)
    y = max(0, min(y, ch - body_h))
    return {"x": int(x), "y": int(y), "w": int(w), "h": int(body_h),
            "glyph_h": int(glyph_h), "scale": int(round(lm["scale"]))}


def _truncate_to_width(draw, text: str, font, max_w: float) -> str:
    """按像素宽度截断（不按字符数 —— 中英混排下字符数靠不住），截断了补省略号。"""
    if max_w <= 0 or not text:
        return ""
    if draw.textlength(text, font=font) <= max_w:
        return text
    ell_w = draw.textlength("…", font=font)
    out = text
    while out and draw.textlength(out, font=font) + ell_w > max_w:
        out = out[:-1]
    return (out + "…") if out else ""


def _palette_idx_map(palette: list) -> dict:
    return {tuple(c): i for i, c in enumerate(palette)}


# ========== DB 与 EXIF 处理 ==========

def extract_date_from_exif(exif_json: Optional[str]) -> str:
    """
    从 EXIF JSON 中提取拍摄日期，返回 YYYY-MM-DD 格式，失败则返回空字符串。
    逻辑与 review_web.py 中保持一致。
    """
    if not exif_json:
        return ""
    try:
        data = json.loads(exif_json)
    except Exception:
        return ""
    dt_str = data.get("datetime")
    if not dt_str:
        return ""
    try:
        date_part = str(dt_str).split()[0]
        parts = date_part.replace(":", "-").split("-")
        if len(parts) >= 3:
            return f"{parts[0]}-{parts[1]}-{parts[2]}"
    except Exception:
        return ""
    return ""


def load_sim_rows() -> List[Dict[str, Any]]:
    """
    加载 InkTime 用的核心字段：
    - path: 照片路径
    - exif_json: 用于解析日期 / GPS
    - side_caption: 文案
    - memory_score: 回忆度
    - exif_gps_lat / exif_gps_lon / exif_city: 地点信息（纯本地，不上网）
    """
    if not DB_PATH.exists():
        raise SystemExit(f"找不到数据库文件: {DB_PATH}")

    # 延迟 import：避免 render 模块在 server 启动时连带拉起 analyze_photos
    # 显式传本模块的 DB_PATH —— open_db 默认用 analyze_photos 的库路径，
    # 两者生产环境相同，但传参才不会在路径被改写时静默连错库。
    from analyze_photos import open_db, is_ignored
    conn = open_db(DB_PATH)
    c = conn.cursor()

    # crop 列（crop_x/y/w/h + is_meaningful）是新加的；旧库可能没有该列，需检测
    col_names = {r[1] for r in c.execute("PRAGMA table_info(photo_scores)").fetchall()}
    crop_cols = ["crop_x", "crop_y", "crop_w", "crop_h", "is_meaningful"]
    # ⚠️ 必须按 crop_cols 的固定顺序展开，缺失的补 NULL AS <col>。
    # 曾用 col_names & set(crop_cols)：集合交集顺序随 PYTHONHASHSEED 每次进程都不同，
    # 而下面回填是按 crop_cols 固定顺序 zip 的 → 列值整体错位（crop_x 拿到 crop_h 的值），
    # 其中 is_meaningful 会拿到浮点 crop 值，而判断是 != 0，浮点恒真 →
    # AI 判定的"无意义"照片此前一直没被过滤掉。
    sel_crop = ", ".join(c if c in col_names else f"NULL AS {c}" for c in crop_cols)

    rows = c.execute(
        f"""
        SELECT path,
               exif_json,
               side_caption,
               memory_score,
               exif_gps_lat,
               exif_gps_lon,
               exif_city,
               used_at,
               used_count,
               {sel_crop}
        FROM photo_scores
        WHERE exif_json IS NOT NULL
        """
    ).fetchall()
    conn.close()

    items: List[Dict[str, Any]] = []
    for row in rows:
        (path, exif_json, side_caption, memory_score, gps_lat, gps_lon, exif_city,
         used_at, used_count) = row[:9]
        # 第 10 列起是 crop 字段（按 sel_crop 固定顺序）
        crop_vals = row[9:]
        date_str = extract_date_from_exif(exif_json)
        if not date_str:
            continue
        # 再次兜底过滤：截图 / NAS 缩略图目录（@eaDir 等）/ macOS 资源分支
        if is_ignored(Path(path)):
            continue

        try:
            y, m, d = map(int, date_str.split("-"))
        except Exception:
            continue
        md = f"{m:02d}-{d:02d}"

        # 把 crop 列按名映射（少的补 None）
        crop_map = dict(zip(crop_cols, crop_vals))
        item = {
            "path": str(path),
            "date": date_str,  # YYYY-MM-DD
            "md": md,          # MM-DD
            "side": side_caption or "",
            "memory": float(memory_score) if memory_score is not None else -1.0,
            "lat": gps_lat,
            "lon": gps_lon,
            "city": exif_city or "",
            "crop_x": crop_map.get("crop_x"),
            "crop_y": crop_map.get("crop_y"),
            "crop_w": crop_map.get("crop_w"),
            "crop_h": crop_map.get("crop_h"),
            "is_meaningful": crop_map.get("is_meaningful"),
            "used_at": used_at or "",                                       # 上次出图时间（ISO），空=从未
            "used_count": int(used_count) if used_count is not None else 0,  # 累计出图次数
        }
        items.append(item)

    return items


# ========== “历史上的今天”选片 ==========

def md_to_day_of_year(md: str) -> Optional[int]:
    """把 'MM-DD' 转成非闰年的第几天（1~365）。"""
    try:
        m, d = map(int, md.split("-"))
        days_before = [0, 0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
        if m < 1 or m > 12:
            return None
        return days_before[m] + d
    except Exception:
        return None


def day_of_year_to_md(day: int) -> str:
    # 选一个非闰年（2001/2005 随便），只依赖 day-of-year。
    base = dt.date(2001, 1, 1) + dt.timedelta(days=day - 1)
    return f"{base.month:02d}-{base.day:02d}"


def choose_photo_for_today(items: List[Dict[str, Any]], today: dt.date) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    选片规则（按月日）：
    - 以 today 的月日为目标，例如 12 月 2 日 -> "12-02"
    - 在所有年份该月日的照片中，找 memory > MEMORY_THRESHOLD 的候选，随机选一张
    - 如果该月日没有任何 > 阈值的，则往前一天（月日）继续找（12-01, 11-30, ...），最多回溯 365 天
    - 如果整个 365 天都没有任何 > 阈值的照片，则在全局中选 memory 最大的一张作为兜底
    """

    if not items:
        raise RuntimeError("没有任何可用照片")

    # 按 md 分组
    by_md: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        md = it["md"]
        by_md.setdefault(md, []).append(it)

    # 每组内按 memory 从高到低排序
    for arr in by_md.values():
        arr.sort(key=lambda x: x.get("memory", -1.0), reverse=True)

    target_md = f"{today.month:02d}-{today.day:02d}"
    target_doy = md_to_day_of_year(target_md)
    if target_doy is None:
        raise RuntimeError(f"无法解析今天的月日: {target_md}")

    import random

    for offset in range(0, 365):
        doy = target_doy - offset
        if doy <= 0:
            doy += 365
        md = day_of_year_to_md(doy)

        arr = by_md.get(md, [])
        if not arr:
            continue
        # 候选：回忆度达标 且 非无意义（AI/阈值判定的 meaningful=0 不进每日一图）
        candidates = [p for p in arr if p.get("memory", -1.0) > MEMORY_THRESHOLD and p.get("is_meaningful", 1) != 0]
        if not candidates:
            continue

        chosen = random.choice(candidates)
        info = {
            "target_md": target_md,
            "used_md": md,
            "day_offset": -offset,
            "candidate_count": len(candidates),
            "total_count_md": len(arr),
            "threshold": MEMORY_THRESHOLD,
            "fallback_global_max": False,
        }
        return chosen, info

    global_best = max(items, key=lambda x: x.get("memory", -1.0))
    info = {
        "target_md": target_md,
        "used_md": global_best["md"],
        "day_offset": None,
        "candidate_count": 1,
        "total_count_md": len(by_md.get(global_best["md"], [])),
        "threshold": MEMORY_THRESHOLD,
        "fallback_global_max": True,
    }
    return global_best, info

def _is_recently_used(item: Dict[str, Any], now: dt.datetime, days: int) -> bool:
    """该照片是否在最近 days 天内出过图（used_at 由渲染端写入，ISO 格式）。

    解析不了就当作"没用过" —— 宽容处理：宁可偶尔重样，也不该因为一条脏数据
    把照片永久排除在候选之外。
    """
    raw = (item.get("used_at") or "").strip()
    if not raw:
        return False
    try:
        t = dt.datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return False
    if t.tzinfo is not None:
        t = t.replace(tzinfo=None)
    return (now - t) < dt.timedelta(days=days)


def _pick_candidates(pool: List[Dict[str, Any]], exclude_paths, now: dt.datetime, recent_days: int):
    """按"新鲜度"阶梯返回候选池，返回 (candidates, relaxed_level)。

    阶梯（前面的够用就不降级）：
      0 = 既不在 exclude_paths 里，最近 recent_days 天也没出过图
      1 = 放宽"最近出过图"（但仍排除 exclude_paths）
      2 = 连 exclude_paths 也放宽

    第 2 级是必需的：相册总量可能比 count 还小（本机测试库 5 张 vs count=5），
    此时硬排除会让"换一张"无图可选 —— 只能报错，或者推回同一张。
    返回的池若不足 count，由调用方从当日其余照片补齐，那一步天然完成了
    "放宽后再补足"，所以这里不需要做数量兜底。
    """
    excluded = exclude_paths or ()
    no_excluded = [p for p in pool if str(p.get("path")) not in excluded]
    fresh = [p for p in no_excluded if not _is_recently_used(p, now, recent_days)]
    if fresh:
        return fresh, 0
    if no_excluded:
        return no_excluded, 1
    return list(pool), 2


def choose_photos_for_today(items: List[Dict[str, Any]], today: dt.date, count: int = 5,
                            exclude_paths=None, recent_days: int | None = None,
                            now: dt.datetime | None = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    选片规则（多张版，按月日）：
    - 以 today 的月日为目标，例如 12 月 2 日 -> "12-02"
    - 在所有年份该月日的照片中，找 memory > MEMORY_THRESHOLD 的候选，尽量随机选 count 张
    - 优先跳过"最近 recent_days 天出过图"的照片（软排除，候选用完会自动放宽）
    - exclude_paths 里的照片优先跳过（"换一张"用它排掉刚展示过的一批）
    - 如果该月日没有任何 > 阈值的，则往前一天（月日）继续找（12-01, 11-30, ...），最多回溯 365 天
    - 如果整个 365 天都没有任何 > 阈值的照片，则在全局中选回忆度最高的若干张作为兜底
    """
    if not items:
        raise RuntimeError("没有任何可用照片")

    if now is None:
        now = dt.datetime.now()
    if recent_days is None:
        recent_days = RECENT_EXCLUDE_DAYS

    # 按 md 分组
    by_md: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        md = it["md"]
        by_md.setdefault(md, []).append(it)

    # 每组内按 memory 从高到低排序
    for arr in by_md.values():
        arr.sort(key=lambda x: x.get("memory", -1.0), reverse=True)

    target_md = f"{today.month:02d}-{today.day:02d}"
    target_doy = md_to_day_of_year(target_md)
    if target_doy is None:
        raise RuntimeError(f"无法解析今天的月日: {target_md}")

    import random

    for offset in range(0, 365):
        doy = target_doy - offset
        if doy <= 0:
            doy += 365
        md = day_of_year_to_md(doy)

        arr = by_md.get(md, [])
        if not arr:
            continue
        # 候选：回忆度达标 且 非无意义（AI/阈值判定的 meaningful=0 不进每日一图）
        base = [p for p in arr if p.get("memory", -1.0) > MEMORY_THRESHOLD and p.get("is_meaningful", 1) != 0]
        if not base:
            continue
        # 再按新鲜度阶梯筛一层（不够会自动降级，见 _pick_candidates）
        candidates, relax = _pick_candidates(base, exclude_paths, now, recent_days)

        # 随机选不重复的多张
        if len(candidates) >= count:
            chosen_list = random.sample(candidates, count)
        else:
            # 候选不足 count 张，用该日剩余的高分照片补齐（这一步就是阶梯的"放宽"落实处）
            chosen_list = list(candidates)
            # 按 path 去重，不要用 `extra in chosen_list` —— 那是对 dict 做值相等比较，
            # 两张只在 used_at 等字段上不同的照片会被误判成同一张而漏加入。
            seen = {str(c.get("path")) for c in chosen_list}
            for extra in arr:
                ep = str(extra.get("path"))
                if ep in seen:
                    continue
                seen.add(ep)
                chosen_list.append(extra)
                if len(chosen_list) >= count:
                    break

        info = {
            "target_md": target_md,
            "used_md": md,
            "day_offset": -offset,
            "candidate_count": len(candidates),
            "total_count_md": len(arr),
            "threshold": MEMORY_THRESHOLD,
            "fallback_global_max": False,
            "freshness_relax": relax,       # 0=全新 / 1=放宽了近期已用 / 2=连排除集也放宽
            "recent_days": recent_days,
        }
        return chosen_list, info

    # 兜底：全局回忆度最高的若干张（同样排除无意义）；若全部无意义，则退化为不过滤（只要有图可显示）
    meaningful_items = [x for x in items if x.get("is_meaningful", 1) != 0]
    if not meaningful_items:
        meaningful_items = items
    pool, relax = _pick_candidates(meaningful_items, exclude_paths, now, recent_days)
    sorted_all = sorted(pool, key=lambda x: x.get("memory", -1.0), reverse=True)
    chosen_list = sorted_all[:count]
    info = {
        "target_md": target_md,
        "used_md": chosen_list[0]["md"] if chosen_list else "",
        "day_offset": None,
        "candidate_count": len(chosen_list),
        "total_count_md": len(items),
        "threshold": MEMORY_THRESHOLD,
        "fallback_global_max": True,
        "freshness_relax": relax,
        "recent_days": recent_days,
    }
    return chosen_list, info
# ========== 绘制 + 抖动 ==========

def nearest_palette_color(r: float, g: float, b: float, palette=None) -> Tuple[int, int, int, int]:
    """
    返回 (idx, pr, pg, pb)，idx 为 palette 中最近颜色的索引。
    palette 缺省用模块级 PALETTE（默认屏）。
    """
    pal = [tuple(c) for c in (palette or PALETTE)]
    best_idx = 0
    best_dist = float("inf")
    for i, (pr, pg, pb) in enumerate(pal):
        dr = r - pr
        dg = g - pg
        db = b - pb
        dist = dr * dr + dg * dg + db * db
        if dist < best_dist:
            best_dist = dist
            best_idx = i
    pr, pg, pb = pal[best_idx]
    return best_idx, pr, pg, pb


# 行首不能出现的标点（中文排版里叫"避头点"）。贪婪折行最容易把最后一个"。"甩到
# 第二行单独一行 —— 看着像 bug，实际只是没做避头处理。
_NO_LINE_START = "。，、；：！？）」』】…·,.;:!?)]}"


def wrap_text_chinese(draw: ImageDraw.ImageDraw,
                      text: str,
                      font: ImageFont.FreeTypeFont,
                      max_width: int,
                      max_lines: int) -> List[str]:
    """中文按字符宽度折行。

    v11 加了两条排版规则（用户 2026-10-17 反馈第二行只剩一个「。」太丑）：
    1. **避头点**：行首不放句读标点；
    2. **两行时尽量均分**：第二行不足第一行 60% 就重新对半拆，
       避免「19 个字 + 1 个字」这种失衡。
    """
    if not text:
        return []
    lines: List[str] = []
    line = ""
    for ch in text:
        test = line + ch
        w = draw.textlength(test, font=font)
        if w <= max_width:
            line = test
        else:
            if line:
                lines.append(line)
            line = ch
            if len(lines) >= max_lines:
                break
    if line and len(lines) < max_lines:
        lines.append(line)

    # 避头点：某行以标点开头时，把上一行最后一个字挪下来
    for i in range(1, len(lines)):
        while lines[i] and lines[i][0] in _NO_LINE_START and len(lines[i - 1]) > 1:
            lines[i - 1], lines[i] = lines[i - 1][:-1], lines[i - 1][-1] + lines[i]

    # 两行失衡 → 重新对半分（挑"两行都放得下、且宽度最接近"的切点）
    if len(lines) == 2 and len(lines[1]) < len(lines[0]) * 0.6:
        best = None
        for i in range(1, len(text)):
            if text[i] in _NO_LINE_START:
                continue
            a, b = text[:i], text[i:]
            wa = draw.textlength(a, font=font)
            wb = draw.textlength(b, font=font)
            if wa <= max_width and wb <= max_width:
                score = abs(wa - wb)
                if best is None or score < best[0]:
                    best = (score, i)
        if best:
            lines = [text[:best[1]], text[best[1]:]]

    return lines


def format_date_display(date_str: str) -> str:
    """
    "YYYY-MM-DD" -> "2022年10月17日"（用户 2026-10-17 指定的中文格式，无前导零）
    """
    if not date_str:
        return ""
    parts = date_str.split("-")
    if len(parts) < 3:
        return date_str
    y = parts[0]
    try:
        m = str(int(parts[1]))
        d = str(int(parts[2]))
    except Exception:
        return date_str
    return f"{y}年{m}月{d}日"


def format_location(lat, lon, city: str) -> str:
    """
    地点字符串：只有拿到像样的地名才显示。

    解析不出地名时**留空**，不再退化成 "22.54310, 114.05787" —— 一串经纬度
    对看照片的人没有意义，反而很像 bug（2026-10-17 按用户要求改掉）。
    """
    if city and str(city).strip():
        return str(city).strip()
    return ""


def render_image(item: Dict[str, Any], screen: Optional[Dict[str, Any]] = None) -> Tuple[Image.Image, str]:
    """
    根据选中的 item 渲染一张 RGB 成品图（按照片方向自适应）：
    - **先解码原图一次**（load_image_any 已 exif_transpose），由 img.size 得真实显示方向。
    - 横照 → 画布对调宽高（横用，如 800×480）；竖照 → 默认（竖用，如 480×800）。
    - 上方图片：占 [0, canvas_h - text_area_h)，用 AI 裁切焦点（有则用，无则真 cover 保比例）。
    - 底部 text_area_h 像素为文字区：第一行 side 文案（最多两行），第二行日期 + 地点。
    screen: 某块屏的配置 dict；缺省用默认屏。多屏并存时逐屏调用各出一张。
    """
    img_path = Path(item["path"])
    if not img_path.exists():
        raise RuntimeError(f"图片不存在: {img_path}")

    # 解码一次取真实方向（不依赖 DB orientation，可能未 transpose 不一致）
    img = load_image_any(img_path)
    orient = render_orientation(img)

    cw, ch, ta, _, _ = _screen_params(screen or SCREEN, orient)
    canvas = Image.new("RGB", (cw, ch), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    eff_screen = dict(screen or SCREEN)
    eff_screen["text_area_height"] = ta

    # item 里可带 AI crop（来自 analyze 入库的 crop_x/y/w/h），尝试用 AI 焦点裁切
    crop = None
    if item.get("crop_x") is not None:
        crop = {"x": item.get("crop_x"), "y": item.get("crop_y"),
                "w": item.get("crop_w"), "h": item.get("crop_h")}

    # ---------- 照片 ----------
    lm = _layout_metrics(cw, ch)
    pw, ph = cw, ch - lm["band"]

    # 优先用 CONVERTED_DIR 已裁切好的成品基图（analyze 阶段写盘，避免重解码原图）；
    # 缺失或尺寸对不上则现裁（converter 内部会顺手覆盖旧缓存）。
    img_cropped, _ = load_converted_or_crop(img, str(img_path), crop, eff_screen, CONVERTED_DIR)
    if img_cropped.size != (pw, ph):
        img_cropped = crop_with_ai_box(img, crop, pw, ph)
    canvas.paste(img_cropped, (0, 0))

    # ---------- 文字区（经典版式：底部白条，文案左对齐，日期左 / 地点右） ----------
    # 字体：FONT_PATH 为空时自动探测容器/本机 CJK 中文字体（否则 load_default 无中文乱码）
    font_big = _get_font(lm["font_caption"])     # 文案
    font_small = _get_font(lm["font_meta"])      # 日期/地点
    text_left = lm["pad_x"]
    text_width = cw - 2 * text_left
    meta_gap = max(4, int(round(10 * lm["scale"])))   # 文字与电量槽之间的呼吸位
    slot = _battery_slot(cw, ch)

    # 文案：最多两行
    side_text = item.get("side") or ""
    y = lm["caption_top"]
    if side_text:
        lines = wrap_text_chinese(draw, side_text, font_big, text_width, max_lines=2)
        for line in lines:
            draw.text((text_left, y), line, font=font_big, fill=(0, 0, 0))
            y += lm["line_h"]

    # 日期 + 地点合成一行，**整行右对齐**，右端让开电量槽：
    #   2022年10月17日 深圳市南山区 [电量图标]
    date_display = format_date_display(item["date"])
    loc_display = format_location(item.get("lat"), item.get("lon"), item.get("city") or "")
    loc_right = slot["x"] - meta_gap
    meta_max_w = loc_right - text_left

    full_text = f"{date_display} {loc_display}".strip()
    if loc_display and draw.textlength(full_text, font=font_small) > meta_max_w:
        # 放不下：优先保住日期，把地点截短（截到只剩 1.5 个字宽就干脆不要）
        date_prefix = (date_display + " ") if date_display else ""
        date_w = draw.textlength(date_prefix, font=font_small)
        loc_max_w = meta_max_w - date_w
        min_loc_w = getattr(font_small, "size", lm["font_meta"]) * 1.5
        if loc_max_w >= min_loc_w:
            full_text = date_prefix + _truncate_to_width(draw, loc_display, font_small, loc_max_w)
        else:
            full_text = date_display
    full_text = _truncate_to_width(draw, full_text, font_small, meta_max_w)

    if full_text:
        meta_w = draw.textlength(full_text, font=font_small)
        draw.text((max(text_left, loc_right - meta_w), lm["meta_top"]),
                  full_text, font=font_small, fill=(0, 0, 0))

    return canvas, orient

def apply_four_color_dither(img: Image.Image, palette=None) -> Image.Image:
    """
    对图像做 Floyd–Steinberg 抖动，量化到 palette（若干个颜色）。
    palette 缺省用模块级 PALETTE（默认屏）。
    """
    pal = [tuple(c) for c in (palette or PALETTE)]
    img = img.convert("RGB")
    w, h = img.size
    pixels = img.load()

    err_r = [0.0] * w
    err_g = [0.0] * w
    err_b = [0.0] * w
    next_err_r = [0.0] * w
    next_err_g = [0.0] * w
    next_err_b = [0.0] * w

    for y in range(h):
        for x in range(w):
            r, g, b = pixels[x, y]
            r = max(0.0, min(255.0, r + err_r[x]))
            g = max(0.0, min(255.0, g + err_g[x]))
            b = max(0.0, min(255.0, b + err_b[x]))

            idx, pr, pg, pb = nearest_palette_color(r, g, b, pal)

            # 写回量化后的颜色
            pixels[x, y] = (pr, pg, pb)

            # 误差
            er = r - pr
            eg = g - pg
            eb = b - pb

            # Floyd–Steinberg:
            #        *   7/16
            #   3/16 5/16 1/16
            if x + 1 < w:
                err_r[x + 1] += er * (7.0 / 16.0)
                err_g[x + 1] += eg * (7.0 / 16.0)
                err_b[x + 1] += eb * (7.0 / 16.0)
            if y + 1 < h:
                if x > 0:
                    next_err_r[x - 1] += er * (3.0 / 16.0)
                    next_err_g[x - 1] += eg * (3.0 / 16.0)
                    next_err_b[x - 1] += eb * (3.0 / 16.0)
                next_err_r[x] += er * (5.0 / 16.0)
                next_err_g[x] += eg * (5.0 / 16.0)
                next_err_b[x] += eb * (5.0 / 16.0)
                if x + 1 < w:
                    next_err_r[x + 1] += er * (1.0 / 16.0)
                    next_err_g[x + 1] += eg * (1.0 / 16.0)
                    next_err_b[x + 1] += eb * (1.0 / 16.0)

        if y + 1 < h:
            # 把 next_err_* 移到当前行，并清零 next_err_*
            for i in range(w):
                err_r[i] = next_err_r[i]
                err_g[i] = next_err_g[i]
                err_b[i] = next_err_b[i]
                next_err_r[i] = 0.0
                next_err_g[i] = 0.0
                next_err_b[i] = 0.0

    return img


def image_to_palette_bin(img: Image.Image, width: int | None = None, height: int | None = None,
                         palette=None, bin_format: str | None = None) -> bytes:
    """
    把已经量化到 palette 的图像转换成 BIN（行优先，从上到下，从左到右）。
    - width/height: 画布尺寸；缺省用模块级 CANVAS_WIDTH/HEIGHT（默认屏）
    - palette: 调色板；缺省用模块级 PALETTE
    - bin_format: "1byte_per_px" | "2px_per_byte"；缺省用模块级 BIN_FORMAT
    """
    w = int(width or CANVAS_WIDTH)
    h = int(height or CANVAS_HEIGHT)
    pal = [tuple(c) for c in (palette or PALETTE)]
    bfmt = str(bin_format or BIN_FORMAT)
    idx_map = {c: i for i, c in enumerate(pal)}

    img = img.convert("RGB")
    if img.size != (w, h):
        raise RuntimeError(f"图像尺寸错误：{img.size}，应为 {(w, h)}")

    def idx_at(x: int, y: int) -> int:
        r, g, b = img.getpixel((x, y))
        key = (int(r), int(g), int(b))
        idx = idx_map.get(key)
        if idx is None:
            idx, _, _, _ = nearest_palette_color(r, g, b, pal)
        return idx

    if bfmt == "2px_per_byte":
        # 每字节 = 左像素(高4bit) <<4 | 右像素(低4bit)
        data = bytearray((w * h) // 2)
        o = 0
        for y in range(h):
            x = 0
            while x < w:
                p0 = idx_at(x, y)
                p1 = idx_at(x + 1, y) if x + 1 < w else 0
                data[o] = ((p0 & 0x0F) << 4) | (p1 & 0x0F)
                o += 1
                x += 2
        return bytes(data)

    # 默认 1byte_per_px
    data = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            data[y * w + x] = idx_at(x, y)
    return bytes(data)


def write_h_array(bin_path: Path, h_path: Path, array_name: str = "daily_bin"):
    """
    把 BIN 转成 C 数组头文件 latest.h：
    const unsigned int daily_bin_size = ...;
    const uint8_t daily_bin[] = { 0x00, 0x01, ... };
    """
    data = bin_path.read_bytes()
    with open(h_path, "w", encoding="utf-8") as f:
        f.write("// Auto-generated from render_daily_photo.py\n")
        f.write(f"// Size = {len(data)} bytes ({CANVAS_WIDTH}x{CANVAS_HEIGHT}, {BIN_FORMAT})\n\n")
        f.write(f"const unsigned int {array_name}_size = {len(data)};\n")
        f.write(f"const uint8_t {array_name}[] = {{\n    ")

        for i, b in enumerate(data):
            f.write(f"0x{b:02X}, ")
            if (i + 1) % 16 == 0:
                f.write("\n    ")

        f.write("\n};\n")


# ========== 主流程 ==========

def _load_rotation() -> List[str]:
    """读取最近出过图的路径（新的在前）。文件缺失/损坏一律当作空。

    排除集"宁少勿错"：读不出来最多是这次可能重选一张，而误判成"全都排除过"
    会让换一张无图可选。
    """
    try:
        data = json.loads(ROTATION_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x) for x in data]
    except Exception:
        pass
    return []


def _append_rotation(paths: List[str]) -> None:
    """把本轮出图的路径追加到轮转记录，只保留最近 ROTATION_KEEP 条。

    原子写（tmp + os.replace）：server 可能同时在读它。
    """
    if not paths:
        return
    try:
        merged = [str(p) for p in paths] + _load_rotation()
        seen, uniq = set(), []
        for p in merged:
            if p in seen:
                continue
            seen.add(p)
            uniq.append(p)
        BIN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = ROTATION_FILE.with_name(ROTATION_FILE.name + ".tmp")
        tmp.write_text(json.dumps(uniq[:ROTATION_KEEP], ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, ROTATION_FILE)
    except Exception as e:
        print(f"[WARN] 写轮转记录失败（不影响本次渲染）: {e}")


def _mark_used(paths: List[str]) -> None:
    """回写照片的出图时间与累计次数。

    只 UPDATE 这两列，绝不能用 _save_result_to_db 那种整行 REPLACE —— 那会把
    打分结果一并重写（还得逐个字段兜住）。计数用 COALESCE 累加而非直接赋值。
    """
    if not paths:
        return
    try:
        from analyze_photos import open_db
        conn = open_db(DB_PATH)
    except Exception as e:
        print(f"[WARN] 打开数据库失败，跳过回写出图记录: {e}")
        return
    try:
        stamp = dt.datetime.now().isoformat(timespec="seconds")
        conn.executemany(
            "UPDATE photo_scores SET used_at = ?, "
            "used_count = COALESCE(used_count, 0) + 1 WHERE path = ?",
            [(stamp, str(p)) for p in paths],
        )
        conn.commit()
        print(f"[OK] 已回写出图记录 {len(paths)} 张（used_at={stamp}）")
    except Exception as e:
        print(f"[WARN] 回写出图记录失败（不影响已生成的成品）: {e}")
    finally:
        conn.close()


def _prune_screen_output(out_root: Path) -> None:
    """清掉该屏上一轮遗留的渲染产物。

    渲染不会自动删旧文件：上一轮 3 张横图、这一轮只有 2 张时，
    landscape/photo_2.bin 会残留且不在新 manifest 里 —— 而设备是按序号
    photo_<idx> 去拉的，会拉到一张不属于本批的照片。
    某个方向本轮一张都没有时，其目录也必须一并清掉，否则里面的旧
    latest.bin 会被顶层同步或"换一张"的排除集读到。
    """
    if not out_root.is_dir():
        return
    stale_patterns = ("photo_*.bin", "photo_*.h", "preview_*.png",
                      "latest.bin", "latest.h", "preview.png", "manifest.json")
    for d in sorted(out_root.iterdir()):
        if not d.is_dir():
            continue
        removed = 0
        for pat in stale_patterns:
            for f in d.glob(pat):
                try:
                    f.unlink()
                    removed += 1
                except OSError as e:
                    print(f"  [WARN] 无法删除旧产物 {f}: {e}")
        if removed:
            print(f"  [清理] {d.name}/ 删除 {removed} 个上一轮产物")
        try:
            d.rmdir()          # 只在已清空时成功；还有别的文件就保留
        except OSError:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="每日选片并渲染墨水屏成品（照片 → 调色板抖动 → BIN + 预览 PNG）")
    parser.add_argument("--next", action="store_true",
                        help="换一张：优先排除最近已出过图的照片，重出一批并覆盖 latest.bin")
    args = parser.parse_args(argv)

    items = load_sim_rows()
    if not items:
        raise SystemExit("没有可用照片（exif_json 为空或解析失败）。")

    # 「换一张」用轮转记录当排除集：它记的是最近出过图的 path，与屏数/方向无关，
    # 比翻各屏各方向的 manifest 更可靠。日常 cron 不带 --next，用不到它 ——
    # 日常只靠 used_at 的 30 天软排除。
    exclude_paths = _load_rotation() if args.next else None
    if args.next:
        print(f"[INFO] 换一张模式：排除最近已出图的 {len(exclude_paths or [])} 张")

    photos, info = choose_photos_for_today(items, TODAY, count=DAILY_PHOTO_QUANTITY,
                                           exclude_paths=exclude_paths)

    print("[INFO] 目标月日:", info["target_md"])
    print("[INFO] 实际使用月日:", info["used_md"])
    print("[INFO] 回溯天数(day_offset):", info["day_offset"])
    print("[INFO] 候选数(>阈值):", info["candidate_count"])
    print("[INFO] 当日总数:", info["total_count_md"])
    print("[INFO] 使用兜底全局最大:", info["fallback_global_max"])
    _relax = info.get("freshness_relax", 0)
    print("[INFO] 新鲜度:", {0: "全部为近期未出过图的照片", 1: "候选不足，已放宽『近期出过图』",
                             2: "候选不足，连排除集也一并放宽"}.get(_relax, _relax),
          f"(近 {info.get('recent_days')} 天)")

    if not photos:
        raise SystemExit("选片结果为空。")

    import shutil

    if not ENABLED_SCREENS:
        print("[WARN] 没有任何启用屏（SCREENS 全为 enabled=False），跳过渲染。")
        return

    # 对每个启用屏各出一套成品（每张照片按自身方向分目录）
    for screen in ENABLED_SCREENS:
        sname = str(screen.get("name", "default"))
        spal = [tuple(c) for c in screen.get("palette", [(0, 0, 0), (255, 255, 255), (200, 0, 0), (220, 180, 0)])]
        sbfmt = str(screen.get("bin_format", "1byte_per_px"))
        out_root = BIN_OUTPUT_DIR / sname
        out_root.mkdir(parents=True, exist_ok=True)
        # 先清上一轮产物，避免陈旧 photo_<idx>.bin 被设备按序号拉到
        _prune_screen_output(out_root)
        print(f"\n[屏幕] {sname} ({sbfmt})")

        # 每方向一个计数器（该方向内的相对序号 photo_0..N 连续）
        dir_counter: dict[str, int] = {}
        dir_manifest: dict[str, list] = {}

        for idx, chosen in enumerate(photos):
            print(f"  [第 {idx} 张] {chosen['path']} 回忆度={chosen['memory']}")

            # 渲染成完整成品图（照片 + 文案 + 日期 + 地点），返回其方向
            img, orient = render_image(chosen, screen)

            # 按方向分目录
            d = out_root / orient
            d.mkdir(parents=True, exist_ok=True)
            rel = dir_counter.get(orient, 0)
            dir_counter[orient] = rel + 1

            # 抖动成墨水屏风格
            img_dithered = apply_four_color_dither(img, spal)

            # 保存预览 PNG（已经是抖动后的效果），方向内相对序号
            preview_path = d / f"preview_{rel}.png"
            img_dithered.save(preview_path)
            print(f"  [OK] 已保存预览 PNG: {preview_path}")

            # 转 BIN（方向对调后的画布尺寸）
            fw, fh, _, _, _ = _screen_params(screen, orient)
            bin_data = image_to_palette_bin(img_dithered, fw, fh, spal, sbfmt)
            bin_path = d / f"photo_{rel}.bin"
            with open(bin_path, "wb") as f:
                f.write(bin_data)
            print(f"  [OK] 已生成 BIN: {bin_path} （大小 {len(bin_data)} 字节）")

            # 头文件数组
            h_path = d / f"photo_{rel}.h"
            array_name = f"daily_bin_{rel}"
            write_h_array(bin_path, h_path, array_name=array_name)
            print(f"  [OK] 已生成头文件数组: {h_path}")

            # 记录 manifest 条目
            dir_manifest.setdefault(orient, []).append({
                "rel": rel,
                "path": chosen["path"],
                "date": chosen.get("date", ""),
                "side": chosen.get("side", ""),
                "memory": chosen.get("memory"),
            })

        # 每方向同步 latest.* 指向该方向 photo_0 + 写 manifest.json
        for orient, entries in dir_manifest.items():
            d = out_root / orient
            cw, ch, ta, _, _ = _screen_params(screen, orient)
            first_bin = d / "photo_0.bin"
            first_h = d / "photo_0.h"
            first_preview = d / "preview_0.png"
            if first_bin.exists():
                shutil.copyfile(first_bin, d / "latest.bin")
                print(f"  [OK] {orient} 已更新 latest.bin -> photo_0.bin")
            if first_h.exists():
                shutil.copyfile(first_h, d / "latest.h")
            if first_preview.exists():
                shutil.copyfile(first_preview, d / "preview.png")

            manifest = {
                "screen": sname,
                "orientation": orient,
                "canvas": {"width": cw, "height": ch},
                "bin_format": sbfmt,
                "palette_size": len(spal),
                "count": len(entries),
                "latest": "photo_0.bin",
                # 本次渲染的时间戳。相框按「换一张」之后是异步等结果的：它轮询这个
                # 字段，变了才说明新图真的出来了（光看 count 不行，重渲染后张数
                # 常常一样，区分不出来）。
                "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
                # 右下角留给固件画电量图标的空白区（画布坐标）。固件照这个坐标画，
                # 不硬编码 —— 版式改了只要动服务器，固件不用重编。
                "battery_slot": _battery_slot(cw, ch),
                "photos": entries,
            }
            (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  [OK] {orient} 已写 manifest.json (count={len(entries)}, canvas={cw}x{ch})")

    # 出图记录：跨所有屏只写一次（回写是按 path 的，与屏无关；按屏各写一次会把计数刷高）
    chosen_paths = [str(p["path"]) for p in photos]
    _mark_used(chosen_paths)
    _append_rotation(chosen_paths)

    # 兼容旧路径：默认屏的产物复制到 BIN_OUTPUT_DIR 顶层（旧固件/模拟器只认顶层 latest.bin）
    # 必须按方向兜底：当天选中的照片可能全是横图或全是竖图，只找 portrait 会在
    # 该方向不存在时静默跳过 → 顶层 latest.bin 一直是旧内容，相框永远显示昨天的图。
    default_name = str(DEFAULT_SCREEN.get("name", "default"))
    top_latest_bin = BIN_OUTPUT_DIR / "latest.bin"
    top_latest_h = BIN_OUTPUT_DIR / "latest.h"
    top_preview = BIN_OUTPUT_DIR / "preview.png"

    src_dir = None
    for orient in ("portrait", "landscape"):
        cand = BIN_OUTPUT_DIR / default_name / orient
        if (cand / "latest.bin").is_file():
            src_dir = cand
            break

    if src_dir is None:
        print(f"[WARN] 默认屏 {default_name} 没有任何方向的 latest.bin，顶层 latest.bin 未更新")
    else:
        for fname, dest in (("latest.bin", top_latest_bin),
                            ("latest.h", top_latest_h),
                            ("preview.png", top_preview)):
            src = src_dir / fname
            if src.is_file():
                shutil.copyfile(src, dest)
        print(f"[OK] 已同步默认屏 {default_name}/{src_dir.name} 产物 -> {BIN_OUTPUT_DIR} 顶层")

    # 「换一张」的诚实验收：阶梯降到 2 说明候选全都落在排除集里，
    # 这时选出来的还是那批 —— 用户点了等于没点，必须明确告诉他原因。
    if args.next and info.get("freshness_relax") == 2:
        print("[WARN] 今日可选照片都已推送过（候选全部落在排除集里），本次换不出新内容；"
              "相册里这一天的照片太少了。")


if __name__ == "__main__":
    main()