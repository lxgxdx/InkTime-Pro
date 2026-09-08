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
import sqlite3
import json
import datetime as dt
import os
from typing import List, Dict, Any, Tuple, Optional
from PIL import Image, ImageDraw, ImageFont, ImageOps
import config as cfg
from image_utils import load_image_any, register_heif
from converter import crop_with_ai_box, load_converted_or_crop, orientation_dims, render_orientation
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

MEMORY_THRESHOLD = float(getattr(cfg, "MEMORY_THRESHOLD", 70.0) or 70.0)
DAILY_PHOTO_QUANTITY = int(getattr(cfg, "DAILY_PHOTO_QUANTITY", 5) or 5)

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
    base_w = int(screen.get("width", 480))
    base_h = int(screen.get("height", 800))
    canvas_w, canvas_h = orientation_dims(screen, orient)   # landscape→(h,w) 对调
    cx_h_ratio = canvas_h / 800.0
    base_ta = float(screen.get("text_area_height", 100 * (base_h / 800.0))) if screen.get("text_area_height") else (100 * (base_h / 800.0))
    ta = int(round(float(base_ta) * (canvas_h / base_h)))   # 文字区按画布高等比缩放（横摆自动变小，条带比例一致）
    pal = [tuple(c) for c in screen.get("palette", [(0, 0, 0), (255, 255, 255), (200, 0, 0), (220, 180, 0)])]
    bfmt = str(screen.get("bin_format", "1byte_per_px"))
    return canvas_w, canvas_h, ta, pal, bfmt


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

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # crop 列（crop_x/y/w/h + is_meaningful）是新加的；旧库可能没有该列，需检测
    col_names = {r[1] for r in c.execute("PRAGMA table_info(photo_scores)").fetchall()}
    crop_cols = ["crop_x", "crop_y", "crop_w", "crop_h", "is_meaningful"]
    sel_crop = ", ".join(col_names & set(crop_cols)) or "NULL AS crop_x"

    rows = c.execute(
        f"""
        SELECT path,
               exif_json,
               side_caption,
               memory_score,
               exif_gps_lat,
               exif_gps_lon,
               exif_city,
               {sel_crop}
        FROM photo_scores
        WHERE exif_json IS NOT NULL
        """
    ).fetchall()
    conn.close()

    items: List[Dict[str, Any]] = []
    for row in rows:
        path, exif_json, side_caption, memory_score, gps_lat, gps_lon, exif_city = row[:7]
        # 第 8 列起是 crop 字段（按 sel_crop 顺序）
        crop_vals = row[7:]
        date_str = extract_date_from_exif(exif_json)
        if not date_str:
            continue
        # 再次兜底过滤 Screenshot 等
        if "screenshot" in str(path).lower():
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

def choose_photos_for_today(items: List[Dict[str, Any]], today: dt.date, count: int = 5) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    选片规则（多张版，按月日）：
    - 以 today 的月日为目标，例如 12 月 2 日 -> "12-02"
    - 在所有年份该月日的照片中，找 memory > MEMORY_THRESHOLD 的候选，尽量随机选 count 张
    - 如果该月日没有任何 > 阈值的，则往前一天（月日）继续找（12-01, 11-30, ...），最多回溯 365 天
    - 如果整个 365 天都没有任何 > 阈值的照片，则在全局中选回忆度最高的若干张作为兜底
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

        # 随机选不重复的多张
        if len(candidates) >= count:
            chosen_list = random.sample(candidates, count)
        else:
            # 候选不足 count 张，用该日剩余的高分照片补齐
            chosen_list = list(candidates)
            for extra in arr:
                if extra in chosen_list:
                    continue
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
        }
        return chosen_list, info

    # 兜底：全局回忆度最高的若干张（同样排除无意义）；若全部无意义，则退化为不过滤（只要有图可显示）
    meaningful_items = [x for x in items if x.get("is_meaningful", 1) != 0]
    if not meaningful_items:
        meaningful_items = items
    sorted_all = sorted(meaningful_items, key=lambda x: x.get("memory", -1.0), reverse=True)
    chosen_list = sorted_all[:count]
    info = {
        "target_md": target_md,
        "used_md": chosen_list[0]["md"] if chosen_list else "",
        "day_offset": None,
        "candidate_count": len(chosen_list),
        "total_count_md": len(items),
        "threshold": MEMORY_THRESHOLD,
        "fallback_global_max": True,
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


def wrap_text_chinese(draw: ImageDraw.ImageDraw,
                      text: str,
                      font: ImageFont.FreeTypeFont,
                      max_width: int,
                      max_lines: int) -> List[str]:
    """
    简单中文按字符宽度折行。
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
    return lines


def format_date_display(date_str: str) -> str:
    """
    "YYYY-MM-DD" -> "YYYY.M.D"
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
    return f"{y}.{m}.{d}"


def format_location(lat, lon, city: str) -> str:
    """
    地点字符串：
    - 有 city 用 city
    - 否则如果有 lat/lon，用 "lat, lon"（5 位小数）
    - 否则空字符串（不写“未知地点”）
    """
    if city and str(city).strip():
        return str(city).strip()
    if lat is None or lon is None:
        return ""
    try:
        return f"{float(lat):.5f}, {float(lon):.5f}"
    except Exception:
        return ""


def render_image(item: Dict[str, Any], screen: Optional[Dict[str, Any]] = None) -> Image.Image:
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

    img_area_w = cw
    img_area_h = ch - ta  # 底部留给文字
    eff_screen = dict(screen or SCREEN)
    eff_screen["text_area_height"] = ta

    # item 里可带 AI crop（来自 analyze 入库的 crop_x/y/w/h），尝试用 AI 焦点裁切
    crop = None
    if item.get("crop_x") is not None:
        crop = {"x": item.get("crop_x"), "y": item.get("crop_y"),
                "w": item.get("crop_w"), "h": item.get("crop_h")}

    # 优先用 CONVERTED_DIR 已裁切好的成品基图（analyze 阶段写盘，避免重解码原图）；
    # 缺失则现裁 + 回填。传入已解码 img，内部按方向取。
    img_cropped, _ = load_converted_or_crop(img, str(img_path), crop, eff_screen, CONVERTED_DIR)

    # 稳健性：成品若尺寸不匹配，重新按当前屏裁
    if img_cropped.size != (img_area_w, img_area_h):
        img_cropped = crop_with_ai_box(img, crop, img_area_w, img_area_h)

    # 贴到上方
    canvas.paste(img_cropped, (0, 0))

    # ---------- 底部文字区域（两种方向都放底部横条） ----------
    padding_x = max(8, int(round(24 * cw / 480.0)))   # 按画布宽等比
    text_area_top = ch - ta + max(6, int(round(10 * ch / 800.0)))
    text_width = cw - 2 * padding_x

    try:
        font_big = ImageFont.truetype(str(FONT_PATH), 22)  # 文案
        font_small = ImageFont.truetype(str(FONT_PATH), 20)  # 日期/地点
    except Exception:
        font_big = ImageFont.load_default()
        font_small = ImageFont.load_default()

    side_text = item.get("side") or ""

    # 文案：最多两行，从 text_area_top 开始
    y = text_area_top
    if side_text:
        lines = wrap_text_chinese(draw, side_text, font_big, text_width, max_lines=2)
        for line in lines:
            draw.text((padding_x, y), line, font=font_big, fill=(0, 0, 0))
            y += max(18, int(round(24 * ch / 800.0)))  # 行高等比

    # 日期 + 地点：固定在底部区域内的第二行
    date_display = format_date_display(item["date"])
    loc_display = format_location(item.get("lat"), item.get("lon"), item.get("city") or "")

    second_line_y = text_area_top + max(44, int(round(54 * ch / 800.0)))
    draw.text((padding_x, second_line_y), date_display, font=font_small, fill=(0, 0, 0))

    loc_w = draw.textlength(loc_display, font=font_small)
    loc_x = padding_x + text_width - loc_w
    if loc_x < padding_x:
        loc_x = padding_x
    draw.text((loc_x, second_line_y), loc_display, font=font_small, fill=(0, 0, 0))

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

def main():
    items = load_sim_rows()
    if not items:
        raise SystemExit("没有可用照片（exif_json 为空或解析失败）。")

    photos, info = choose_photos_for_today(items, TODAY, count=DAILY_PHOTO_QUANTITY)

    print("[INFO] 目标月日:", info["target_md"])
    print("[INFO] 实际使用月日:", info["used_md"])
    print("[INFO] 回溯天数(day_offset):", info["day_offset"])
    print("[INFO] 候选数(>阈值):", info["candidate_count"])
    print("[INFO] 当日总数:", info["total_count_md"])
    print("[INFO] 使用兜底全局最大:", info["fallback_global_max"])

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
            cw, ch, _, _, _ = _screen_params(screen, orient)
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
                "photos": entries,
            }
            (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  [OK] {orient} 已写 manifest.json (count={len(entries)}, canvas={cw}x{ch})")

    # 兼容旧路径：默认屏 portrait 方向的产物复制到 BIN_OUTPUT_DIR 顶层（旧 ESP/模拟器用 latest.bin）
    default_name = str(DEFAULT_SCREEN.get("name", "default"))
    default_dir = BIN_OUTPUT_DIR / default_name / "portrait"
    top_latest_bin = BIN_OUTPUT_DIR / "latest.bin"
    top_latest_h = BIN_OUTPUT_DIR / "latest.h"
    top_preview = BIN_OUTPUT_DIR / "preview.png"
    if (default_dir / "latest.bin").exists():
        shutil.copyfile(default_dir / "latest.bin", top_latest_bin)
        print(f"[OK] 已同步默认屏 portrait latest.bin -> {top_latest_bin}")
    if (default_dir / "latest.h").exists():
        shutil.copyfile(default_dir / "latest.h", top_latest_h)
    if (default_dir / "preview.png").exists():
        shutil.copyfile(default_dir / "preview.png", top_preview)


if __name__ == "__main__":
    main()