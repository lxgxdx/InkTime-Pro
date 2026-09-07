#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
13.3 寸 6色墨水屏渲染脚本（新文件，不改原 render_daily_photo.py / render_daily_photo_133c.py）：

在保持 13.3 独立输出目录 output/inktime_13in3_6c/ 的同时，
额外把文件复制到旧的 BIN_OUTPUT_DIR（server 已经映射的静态目录）下，
用不同文件名区分，避免影响原来的 7.3 寸服务与文件。
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import json
import datetime as dt
from typing import List, Dict, Any, Tuple, Optional
from PIL import Image, ImageDraw, ImageFont, ImageOps
import config as cfg
import shutil


TODAY = dt.date.today()

# === 路径配置（来自 config.py，沿用旧字段） ===
ROOT_DIR = Path(__file__).resolve().parent

DB_PATH = Path(str(getattr(cfg, "DB_PATH", "photos.db") or "photos.db")).expanduser()
if not DB_PATH.is_absolute():
    DB_PATH = (ROOT_DIR / DB_PATH).resolve()

FONT_PATH = Path(str(getattr(cfg, "FONT_PATH", "") or "")).expanduser()
if str(FONT_PATH) and not FONT_PATH.is_absolute():
    FONT_PATH = (ROOT_DIR / FONT_PATH).resolve()

MEMORY_THRESHOLD = float(getattr(cfg, "MEMORY_THRESHOLD", 70.0) or 70.0)
DAILY_PHOTO_QUANTITY = int(getattr(cfg, "DAILY_PHOTO_QUANTITY", 5) or 5)

# ====== 旧输出目录（7.3 寸服务正在用的静态目录，server.py 已经映射它）======
BIN_OUTPUT_DIR = Path(str(getattr(cfg, "BIN_OUTPUT_DIR", "output/inktime") or "output/inktime")).expanduser()
if not BIN_OUTPUT_DIR.is_absolute():
    BIN_OUTPUT_DIR = (ROOT_DIR / BIN_OUTPUT_DIR).resolve()
BIN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ====== 13.3 独立输出目录（保留一份“原始产物”，不污染旧目录）======
BIN_OUTPUT_DIR_13 = (BIN_OUTPUT_DIR.parent / "inktime_13in3_6c").resolve()
BIN_OUTPUT_DIR_13.mkdir(parents=True, exist_ok=True)

# ====== 服务器静态目录（不改 server.py：沿用旧路由所映射的目录） ======
SERVER_STATIC_DIR = BIN_OUTPUT_DIR
SERVER_STATIC_DIR.mkdir(parents=True, exist_ok=True)

# ====== 13.3 寸屏参数 ======
CANVAS_WIDTH = 1200
CANVAS_HEIGHT = 1600
TEXT_AREA_HEIGHT = 200  # 1600*0.125


# ========== DB 与 EXIF 处理 ==========

def extract_date_from_exif(exif_json: Optional[str]) -> str:
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
    if not DB_PATH.exists():
        raise SystemExit(f"找不到数据库文件: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    rows = c.execute(
        """
        SELECT path,
               exif_json,
               side_caption,
               memory_score,
               exif_gps_lat,
               exif_gps_lon,
               exif_city
        FROM photo_scores
        WHERE exif_json IS NOT NULL
        """
    ).fetchall()
    conn.close()

    items: List[Dict[str, Any]] = []
    for path, exif_json, side_caption, memory_score, gps_lat, gps_lon, exif_city in rows:
        date_str = extract_date_from_exif(exif_json)
        if not date_str:
            continue
        if "screenshot" in str(path).lower():
            continue

        try:
            y, m, d = map(int, date_str.split("-"))
        except Exception:
            continue
        md = f"{m:02d}-{d:02d}"

        item = {
            "path": str(path),
            "date": date_str,
            "md": md,
            "side": side_caption or "",
            "memory": float(memory_score) if memory_score is not None else -1.0,
            "lat": gps_lat,
            "lon": gps_lon,
            "city": exif_city or "",
        }
        items.append(item)

    return items


# ========== “历史上的今天”选片（原逻辑保留） ==========

def md_to_day_of_year(md: str) -> Optional[int]:
    try:
        m, d = map(int, md.split("-"))
        days_before = [0, 0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
        if m < 1 or m > 12:
            return None
        return days_before[m] + d
    except Exception:
        return None


def day_of_year_to_md(day: int) -> str:
    base = dt.date(2001, 1, 1) + dt.timedelta(days=day - 1)
    return f"{base.month:02d}-{base.day:02d}"


def choose_photos_for_today(items: List[Dict[str, Any]], today: dt.date, count: int = 5) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not items:
        raise RuntimeError("没有任何可用照片")

    by_md: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        md = it["md"]
        by_md.setdefault(md, []).append(it)

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
        candidates = [p for p in arr if p.get("memory", -1.0) > MEMORY_THRESHOLD]
        if not candidates:
            continue

        if len(candidates) >= count:
            chosen_list = random.sample(candidates, count)
        else:
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

    sorted_all = sorted(items, key=lambda x: x.get("memory", -1.0), reverse=True)
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


# ========== 绘制（布局不变，只做等比放大） ==========

def wrap_text_chinese(draw: ImageDraw.ImageDraw,
                      text: str,
                      font: ImageFont.FreeTypeFont,
                      max_width: int,
                      max_lines: int) -> List[str]:
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
    if city and str(city).strip():
        return str(city).strip()
    if lat is None or lon is None:
        return ""
    try:
        return f"{float(lat):.5f}, {float(lon):.5f}"
    except Exception:
        return ""


def render_image(item: Dict[str, Any]) -> Image.Image:
    canvas = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    img_path = Path(item["path"])
    if not img_path.exists():
        raise RuntimeError(f"图片不存在: {img_path}")
    img = Image.open(img_path)
    img = ImageOps.exif_transpose(img).convert("RGB")

    img_w, img_h = img.size
    if img_w == 0 or img_h == 0:
        raise RuntimeError(f"图片尺寸非法: {img.size}")

    img_area_w = CANVAS_WIDTH
    img_area_h = CANVAS_HEIGHT - TEXT_AREA_HEIGHT

    scale = max(img_area_w / img_w, img_area_h / img_h)
    draw_w = int(img_w * scale)
    draw_h = int(img_h * scale)

    img_resized = img.resize((draw_w, draw_h), Image.LANCZOS)

    left = max(0, (draw_w - img_area_w) // 2)
    top = max(0, (draw_h - img_area_h) // 2)
    img_cropped = img_resized.crop((left, top, left + img_area_w, top + img_area_h))

    canvas.paste(img_cropped, (0, 0))

    padding_x = int(round(24 * 2.5))       # 60
    text_area_top = CANVAS_HEIGHT - TEXT_AREA_HEIGHT + int(round(10 * 2.0))  # +20
    text_width = CANVAS_WIDTH - 2 * padding_x

    try:
        font_big = ImageFont.truetype(str(FONT_PATH), int(round(22 * 2.0)))
        font_small = ImageFont.truetype(str(FONT_PATH), int(round(20 * 2.0)))
    except Exception:
        font_big = ImageFont.load_default()
        font_small = ImageFont.load_default()

    side_text = item.get("side") or ""

    y = text_area_top
    if side_text:
        lines = wrap_text_chinese(draw, side_text, font_big, text_width, max_lines=2)
        line_h = int(round(24 * 2.0))
        for line in lines:
            draw.text((padding_x, y), line, font=font_big, fill=(0, 0, 0))
            y += line_h

    date_display = format_date_display(item["date"])
    loc_display = format_location(item.get("lat"), item.get("lon"), item.get("city") or "")

    second_line_y = text_area_top + int(round(54 * 2.0))
    draw.text((padding_x, second_line_y), date_display, font=font_small, fill=(0, 0, 0))

    loc_w = draw.textlength(loc_display, font=font_small)
    loc_x = padding_x + text_width - loc_w
    if loc_x < padding_x:
        loc_x = padding_x
    draw.text((loc_x, second_line_y), loc_display, font=font_small, fill=(0, 0, 0))

    return canvas


# ========== 6 色抖动与 4bpp 打包 ==========

PALETTE_6 = [
    (0,   0,   0),       # 0 Black
    (255, 255, 255),     # 1 White
    (255, 255, 0),       # 2 Yellow
    (255, 0,   0),       # 3 Red
    (0,   0,   255),     # 5 Blue
    (0,   255, 0),       # 6 Green
]
PALETTE_6_INDEX = [0, 1, 2, 3, 5, 6]


def nearest_palette_index_6(r: float, g: float, b: float) -> int:
    best_i = 0
    best_dist = float("inf")
    for i, (pr, pg, pb) in enumerate(PALETTE_6):
        dr = r - pr
        dg = g - pg
        db = b - pb
        dist = dr * dr + dg * dg + db * db
        if dist < best_dist:
            best_dist = dist
            best_i = i
    return PALETTE_6_INDEX[best_i]


def index_to_rgb(idx: int):
    if idx == 0: return (0, 0, 0)
    if idx == 1: return (255, 255, 255)
    if idx == 2: return (255, 255, 0)
    if idx == 3: return (255, 0, 0)
    if idx == 5: return (0, 0, 255)
    if idx == 6: return (0, 255, 0)
    return (255, 255, 255)


def apply_6color_dither(img: Image.Image) -> Image.Image:
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

            idx = nearest_palette_index_6(r, g, b)
            pr, pg, pb = index_to_rgb(idx)
            pixels[x, y] = (pr, pg, pb)

            er = r - pr
            eg = g - pg
            eb = b - pb

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
            for i in range(w):
                err_r[i] = next_err_r[i]
                err_g[i] = next_err_g[i]
                err_b[i] = next_err_b[i]
                next_err_r[i] = 0.0
                next_err_g[i] = 0.0
                next_err_b[i] = 0.0

    return img


def pack2_ino(p0: int, p1: int) -> int:
    """Match esp32wifi.ino pack2(): low nibble = first pixel, high nibble = second pixel."""
    return (p0 & 0x0F) | ((p1 & 0x0F) << 4)


def image_rgb_to_13in3e_idx(px) -> int:
    """Map exact palette RGB to E6 indices; fallback to nearest."""
    r, g, b = px
    if (r, g, b) == (0, 0, 0):
        return 0
    if (r, g, b) == (255, 255, 255):
        return 1
    if (r, g, b) == (255, 255, 0):
        return 2
    if (r, g, b) == (255, 0, 0):
        return 3
    if (r, g, b) == (0, 0, 255):
        return 5
    if (r, g, b) == (0, 255, 0):
        return 6
    return nearest_palette_index_6(r, g, b)


def image_to_half_4bpp_packed_bin_13in3e(img: Image.Image, x_offset: int) -> bytes:
    """
    Export one half of the 1200x1600 image into a byte stream that matches esp32wifi.ino::write_half().

    - Half width: 600px (HALF_W)
    - Per row bytes: 600/2 = 300
    - Packing: pack2_ino(p0, p1) => low nibble first pixel, high nibble second pixel

    The returned bytes are laid out row-major, exactly as the ino sends via EPD_dispLoad(lineBuf, 300)
    for y=0..1599.
    """
    img = img.convert("RGB")
    if img.size != (CANVAS_WIDTH, CANVAS_HEIGHT):
        raise RuntimeError(f"图像尺寸错误：{img.size}，应为 {(CANVAS_WIDTH, CANVAS_HEIGHT)}")

    if x_offset not in (0, 600):
        raise RuntimeError(f"x_offset 只能是 0 或 600，当前: {x_offset}")

    half_w = 600
    out = bytearray((half_w * CANVAS_HEIGHT) // 2)  # 600*1600/2 = 480000

    o = 0
    for y in range(CANVAS_HEIGHT):
        x = 0
        while x < half_w:
            p0 = image_rgb_to_13in3e_idx(img.getpixel((x_offset + x, y)))
            p1 = image_rgb_to_13in3e_idx(img.getpixel((x_offset + x + 1, y)))
            out[o] = pack2_ino(p0, p1)
            o += 1
            x += 2

    return bytes(out)


def image_to_full_4bpp_packed_bin_13in3e(img: Image.Image) -> bytes:
    """Optional: export full frame (1200x1600) in the same nibble order as esp32wifi.ino."""
    img = img.convert("RGB")
    if img.size != (CANVAS_WIDTH, CANVAS_HEIGHT):
        raise RuntimeError(f"图像尺寸错误：{img.size}，应为 {(CANVAS_WIDTH, CANVAS_HEIGHT)}")

    out = bytearray((CANVAS_WIDTH * CANVAS_HEIGHT) // 2)

    o = 0
    for y in range(CANVAS_HEIGHT):
        x = 0
        while x < CANVAS_WIDTH:
            p0 = image_rgb_to_13in3e_idx(img.getpixel((x, y)))
            p1 = image_rgb_to_13in3e_idx(img.getpixel((x + 1, y)))
            out[o] = pack2_ino(p0, p1)
            o += 1
            x += 2

    return bytes(out)


def main():
    items = load_sim_rows()
    if not items:
        raise SystemExit("没有可用照片（exif_json 为空或解析失败）。")

    photos, info = choose_photos_for_today(items, TODAY, count=DAILY_PHOTO_QUANTITY)

    print("[INFO-13in3-6c] used_md:", info["used_md"], "offset:", info["day_offset"], "fallback:", info["fallback_global_max"])

    if not photos:
        raise SystemExit("选片结果为空。")

    for idx, chosen in enumerate(photos):
        img = render_image(chosen)
        img_dithered = apply_6color_dither(img)

        preview_path = BIN_OUTPUT_DIR_13 / f"preview_13in3_6c_{idx}.png"
        img_dithered.save(preview_path)
        print(f"[OK-13in3-6c] preview: {preview_path}")

        # ---- 按 esp32wifi.ino 的“半屏逐行流式写入”逻辑导出 ----
        left_data = image_to_half_4bpp_packed_bin_13in3e(img_dithered, x_offset=0)
        right_data = image_to_half_4bpp_packed_bin_13in3e(img_dithered, x_offset=600)

        left_path = BIN_OUTPUT_DIR_13 / f"photo_13in3_6c_{idx}_L.bin"
        right_path = BIN_OUTPUT_DIR_13 / f"photo_13in3_6c_{idx}_R.bin"
        left_path.write_bytes(left_data)
        right_path.write_bytes(right_data)
        print(f"[OK-13in3-6c] left bin:  {left_path} size={len(left_data)}")
        print(f"[OK-13in3-6c] right bin: {right_path} size={len(right_data)}")

        # 可选：导出整帧（同样的 nibble 顺序），方便离线检查/备用
        full_data = image_to_full_4bpp_packed_bin_13in3e(img_dithered)
        full_path = BIN_OUTPUT_DIR_13 / f"photo_13in3_6c_{idx}_FULL.bin"
        full_path.write_bytes(full_data)
        print(f"[OK-13in3-6c] full bin:  {full_path} size={len(full_data)}")

        # ---- 额外复制一份到服务器静态目录（沿用旧路由，不改 server.py）----
        server_preview_path = SERVER_STATIC_DIR / f"preview_13in3_6c_{idx}.png"
        server_left_path = SERVER_STATIC_DIR / f"photo_13in3_6c_{idx}_L.bin"
        server_right_path = SERVER_STATIC_DIR / f"photo_13in3_6c_{idx}_R.bin"
        server_full_path = SERVER_STATIC_DIR / f"photo_13in3_6c_{idx}_FULL.bin"

        shutil.copyfile(preview_path, server_preview_path)
        shutil.copyfile(left_path, server_left_path)
        shutil.copyfile(right_path, server_right_path)
        shutil.copyfile(full_path, server_full_path)

        print(f"[OK-13in3-6c] server preview: {server_preview_path}")
        print(f"[OK-13in3-6c] server left bin:  {server_left_path} size={server_left_path.stat().st_size}")
        print(f"[OK-13in3-6c] server right bin: {server_right_path} size={server_right_path.stat().st_size}")
        print(f"[OK-13in3-6c] server full bin:  {server_full_path} size={server_full_path.stat().st_size}")

    # latest 指向第 0 张（名字也区分）
    first_left = BIN_OUTPUT_DIR_13 / "photo_13in3_6c_0_L.bin"
    first_right = BIN_OUTPUT_DIR_13 / "photo_13in3_6c_0_R.bin"
    first_full = BIN_OUTPUT_DIR_13 / "photo_13in3_6c_0_FULL.bin"
    first_preview = BIN_OUTPUT_DIR_13 / "preview_13in3_6c_0.png"

    latest_left = BIN_OUTPUT_DIR_13 / "latest_13in3_6c_L.bin"
    latest_right = BIN_OUTPUT_DIR_13 / "latest_13in3_6c_R.bin"
    latest_full = BIN_OUTPUT_DIR_13 / "latest_13in3_6c_FULL.bin"
    latest_preview = BIN_OUTPUT_DIR_13 / "preview_13in3_6c.png"

    server_latest_left = SERVER_STATIC_DIR / "latest_13in3_6c_L.bin"
    server_latest_right = SERVER_STATIC_DIR / "latest_13in3_6c_R.bin"
    server_latest_full = SERVER_STATIC_DIR / "latest_13in3_6c_FULL.bin"
    server_latest_preview = SERVER_STATIC_DIR / "preview_13in3_6c.png"

    if first_left.exists():
        shutil.copyfile(first_left, latest_left)
        shutil.copyfile(first_left, server_latest_left)
        print(f"[OK-13in3-6c] latest left bin -> {first_left.name}")
        print(f"[OK-13in3-6c] server latest left bin -> {server_latest_left}")

    if first_right.exists():
        shutil.copyfile(first_right, latest_right)
        shutil.copyfile(first_right, server_latest_right)
        print(f"[OK-13in3-6c] latest right bin -> {first_right.name}")
        print(f"[OK-13in3-6c] server latest right bin -> {server_latest_right}")

    if first_full.exists():
        shutil.copyfile(first_full, latest_full)
        shutil.copyfile(first_full, server_latest_full)
        print(f"[OK-13in3-6c] latest full bin -> {first_full.name}")
        print(f"[OK-13in3-6c] server latest full bin -> {server_latest_full}")

    if first_preview.exists():
        shutil.copyfile(first_preview, latest_preview)
        shutil.copyfile(first_preview, server_latest_preview)
        print(f"[OK-13in3-6c] latest preview -> {first_preview.name}")
        print(f"[OK-13in3-6c] server latest preview -> {server_latest_preview}")


if __name__ == "__main__":
    main()