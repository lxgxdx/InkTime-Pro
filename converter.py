# -*- coding: utf-8 -*-
"""AI 指导裁切 + 多分辨率成品图生成。

把"渲染时现场从原图居中 cover 裁剪"升级为：
- analyze 阶段用 AI 给的归一化焦点 bbox（0~1）确定构图；
- 一次为"每个启用分辨率"各出一张铺满且主体不切的预裁切基图；
- 按指纹命名写入 CONVERTED_DIR，render/daily 直接复用，避免重复解码原图。

不依赖 config / cfg（避免交叉导入），路径/指纹由调用方传入或从 image_utils 拿。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from PIL import Image

from image_utils import load_image_any

# 默认焦点框（居中、占 80% 面积），当 AI 未给 crop 或解析失败时回退。
DEFAULT_CROP = {"x": 0.5, "y": 0.5, "w": 0.8, "h": 0.8}


def _clamp01(v: float, default: float) -> float:
    try:
        v = float(v)
    except Exception:
        return default
    if v != v:  # NaN
        return default
    return max(0.0, min(1.0, v))


def normalize_crop(crop: Optional[dict]) -> dict:
    """把 AI 返回的 crop 归一化为合法的 (x, y, w, h)（0~1），缺省回退 DEFAULT_CROP。

    兼容三种形态：
    - {"x":..,"y":..,"w":..,"h":..}   中心 + 宽高
    - {"x1":..,"y1":..,"x2":..,"y2":..} 两个角点
    其它 / 字段不全 / 越界 → 回退居中。
    """
    if not isinstance(crop, dict):
        return dict(DEFAULT_CROP)

    # 形态 2：两个角点 → 转成中心+宽高
    if "x1" in crop or "x2" in crop:
        try:
            x1 = float(crop.get("x1", 0.0))
            y1 = float(crop.get("y1", 0.0))
            x2 = float(crop.get("x2", 1.0))
            y2 = float(crop.get("y2", 1.0))
            x = (x1 + x2) / 2.0
            y = (y1 + y2) / 2.0
            w = abs(x2 - x1)
            h = abs(y2 - y1)
            if w <= 0.01 or h <= 0.01:
                return dict(DEFAULT_CROP)
            return {
                "x": _clamp01(x, 0.5),
                "y": _clamp01(y, 0.5),
                "w": _clamp01(w, 0.8),
                "h": _clamp01(h, 0.8),
            }
        except Exception:
            return dict(DEFAULT_CROP)

    # 形态 1：中心+宽高
    try:
        x = _clamp01(crop.get("x", 0.5), 0.5)
        y = _clamp01(crop.get("y", 0.5), 0.5)
        w = _clamp01(crop.get("w", 0.8), 0.8)
        h = _clamp01(crop.get("h", 0.8), 0.8)
        if w <= 0.01 or h <= 0.01:
            return dict(DEFAULT_CROP)
        return {"x": x, "y": y, "w": w, "h": h}
    except Exception:
        return dict(DEFAULT_CROP)


def render_orientation(img: Image.Image) -> str:
    """按解码后图片的显示方向返回 "landscape" / "portrait" / "square"。

    用 img.size（经 load_image_any 的 exif_transpose，已是人眼看的方向）判断：
    宽>高=风景(landscape)、高>宽=人像(portrait)、相等=square。
    """
    w, h = img.size
    if w > h:
        return "landscape"
    if h > w:
        return "portrait"
    return "square"


def orientation_dims(screen: dict, orient: Optional[str]) -> tuple:
    """返回某方向下的画布尺寸 (canvas_w, canvas_h)。

    屏幕配置记的是默认方向（竖用）。landscape(风景横图) 时对调宽高（横用），
    portrait/square 保持默认。orient 为空时按默认（portrait）。
    """
    screen = dict(screen or {})
    w = int(screen.get("width", 480))
    h = int(screen.get("height", 800))
    if orient == "landscape":
        return (h, w)          # 对调：横用
    return (w, h)              # portrait / square / 缺省：默认竖用


def photo_fingerprint(path, screen) -> str:
    """由原图路径 + 屏幕尺寸 + 方向生成稳定指纹（不含随机性），用于 CONVERTED_DIR 下文件名。

    - 避免不同原图/不同屏/不同方向重名；
    - 同一原图同一屏同一方向再次生成时覆盖同名文件，天然幂等。
    - 方向维度必须纳入，否则横竖成品互相覆盖。
    """
    p = str(path)
    w = int((screen or {}).get("width", 0))
    h = int((screen or {}).get("height", 0))
    orient = str((screen or {}).get("orientation", "portrait"))
    raw = f"{p}|{orient}|{w}x{h}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def crop_with_ai_box(img: Image.Image, crop: Optional[dict], target_w: int, target_h: int) -> Image.Image:
    """按 AI 焦点 bbox + 目标宽高比做**真 cover 裁剪**，返回铺满 target_w x target_h 的图。

    关键（v4 修复）：真正"保比例 cover" —— 裁掉溢出边（横图裁左右、竖图裁上下），
    而不是把整幅图拉伸到目标比例（旧版 cover 计算 `need>=整图` + resize 导致变形）。
    AI 的 crop 框只用来定位窗口中心、决定是否放大到主体，绝不影响目标比例。

    原则：
    - 目标比例固定为 target_w/target_h，裁剪窗口恒为该比例。
    - 默认取"整幅图在该比例下的最大取景框"（cover：横裁左右/竖裁上下）。
    - 若 AI bbox 足够小、能放进图内，则放大到恰好包含 bbox 的最小目标比例窗（真聚焦主体）。
    - 窗口以焦点中心定位，clamp 进图（平移不改变比例）。
    """
    img = img.convert("RGB")
    img_w, img_h = img.size
    if img_w == 0 or img_h == 0:
        raise RuntimeError(f"图片尺寸非法: {img.size}")

    if target_w <= 0 or target_h <= 0:
        raise RuntimeError(f"目标尺寸非法: {target_w}x{target_h}")

    target_ratio = target_w / target_h
    c = normalize_crop(crop)

    # 焦点 bbox 在原图上的像素范围
    bx0 = c["x"] * img_w - c["w"] * img_w / 2.0
    by0 = c["y"] * img_h - c["h"] * img_h / 2.0
    bx1 = c["x"] * img_w + c["w"] * img_w / 2.0
    by1 = c["y"] * img_h + c["h"] * img_h / 2.0
    cx = (bx0 + bx1) / 2.0
    cy = (by0 + by1) / 2.0
    box_w = max(bx1 - bx0, 1.0)
    box_h = max(by1 - by0, 1.0)

    # 真 cover：整幅图在目标比例下的最大取景框（恒 <= 图，绝不失真）
    if img_w / img_h > target_ratio:      # 图比目标更宽 → 裁左右，保留全高
        cover_w = img_h * target_ratio
        cover_h = img_h
    else:                                 # 图更窄/更方 → 裁上下，保留全宽
        cover_w = img_w
        cover_h = img_w / target_ratio

    # AI 缩放窗：恰好包含 bbox 的最小目标比例窗
    if box_w / box_h > target_ratio:
        zoom_w = box_w
        zoom_h = box_w / target_ratio
    else:
        zoom_w = box_h * target_ratio
        zoom_h = box_h

    # 选择：AI 窗能放进图内则用（真裁剪到焦点），否则退回整图 cover
    if zoom_w <= img_w and zoom_h <= img_h:
        win_w, win_h = zoom_w, zoom_h
    else:
        win_w, win_h = cover_w, cover_h

    # 以焦点中心定位，clamp 到原图内（平移不改变比例）
    max_left = max(0.0, img_w - win_w)
    max_top = max(0.0, img_h - win_h)
    left = _clamp(cx - win_w / 2.0, 0.0, max_left)
    top = _clamp(cy - win_h / 2.0, 0.0, max_top)

    box = (int(round(left)), int(round(top)), int(round(left + win_w)), int(round(top + win_h)))
    # 确保不越界（浮点舍入防御）
    box = (
        max(0, box[0]),
        max(0, box[1]),
        min(img_w, box[2]),
        min(img_h, box[3]),
    )
    if box[2] - box[0] <= 0 or box[3] - box[1] <= 0:
        # 兜底：居中 cover（取整幅图在该比例下最大取景）
        box = (0, 0, int(round(min(img_w, img_h * target_ratio))), int(round(min(img_h, img_w / target_ratio))))
        if box[2] <= 0 or box[3] <= 0:
            box = (0, 0, img_w, img_h)

    cropped = img.crop(box)
    # 等比缩放回目标尺寸（box 比例已=target_ratio，缩放无扭曲）
    return cropped.resize((target_w, target_h), Image.LANCZOS)


def _clamp(v: float, lo: float, hi: float) -> float:
    """数值 clamp 到 [lo, hi]，hi<lo 时返回 lo。"""
    if lo > hi:
        return lo
    return max(lo, min(hi, v))


def generate_converted_for_screens(
    img: Image.Image,
    photo_path,
    crop: Optional[dict],
    screens: list[dict],
    converted_dir: Path,
) -> dict[str, Path]:
    """为每个启用屏生成一张预裁切基图，写入 converted_dir/<screen_name>/<orientation>/<fp>.jpg。

    方向化（v4）：每张照片**只按它的真实方向**生成一档（横照横向、竖照竖向），
    因此每张照片只出一张成品（省一半 token/IO）。方向由 img.size（显示方向）决定。

    Args:
        img: 已解码的 RGB 原图（analyze 阶段顺手传入，避免二次解码）。
        photo_path: 原图路径（参与指纹，保证不同照片不同文件）。
        crop: AI 给的归一化焦点框，可 None（回退居中）。
        screens: 屏幕配置列表（取 enabled==True 且含 width/height 的项）。
        converted_dir: 成品根目录。

    Returns:
        {"<screen_name>/<orientation>": Path} —— 写出的成品图文件路径。
    """
    converted_dir = Path(converted_dir)
    written: dict[str, Path] = {}
    orient = render_orientation(img)   # 照片真实方向

    for sc in screens:
        if not sc.get("enabled", True):
            continue
        name = str(sc.get("name", "default"))
        w = int(sc.get("width", 0))
        h = int(sc.get("height", 0))
        if w <= 0 or h <= 0:
            continue
        # 成品图裁到"照片显示区"（画布减去底部文字区）：按该照片方向对调宽高
        ta = int(sc.get("text_area_height", 100))
        dim_w, dim_h = orientation_dims(sc, orient)
        img_area_h = max(1, dim_h - ta)

        eff_screen = dict(sc)
        eff_screen["orientation"] = orient
        fp = photo_fingerprint(photo_path, eff_screen)
        fname = f"{fp}.jpg"
        out_dir = converted_dir / name / orient
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / fname

        try:
            img_cropped = crop_with_ai_box(img, crop, dim_w, img_area_h)
            img_cropped.save(out_path, format="JPEG", quality=90)
            written[f"{name}/{orient}"] = out_path
        except Exception as e:
            print(f"[converter] 生成成品失败({name}/{orient}): {e}")

    return written


def load_converted_or_crop(
    img: Image.Image,
    photo_path,
    crop: Optional[dict],
    screen: dict,
    converted_dir: Path,
) -> tuple:
    """优先返回已在 CONVERTED_DIR 的成品图；缺失则按该照片方向现裁 + 回填。

    v4 改签名：第一个参数传**已解码的 img**（避免二次解码、并取真实方向）。

    Returns:
        (PIL.Image, Path | None) —— 图像 + 写出的成品路径（None 表示未写盘）。
    """
    if img is None:
        img = load_image_any(photo_path)
    orient = render_orientation(img)
    try:
        converted_dir = Path(converted_dir)
        name = str(screen.get("name", "default"))
        w = int(screen.get("width", 0))
        h = int(screen.get("height", 0))
        if w > 0 and h > 0:
            eff_screen = dict(screen)
            eff_screen["orientation"] = orient
            fp = photo_fingerprint(photo_path, eff_screen)
            cache_path = converted_dir / name / orient / f"{fp}.jpg"
            if cache_path.exists():
                cached = Image.open(cache_path)
                cached = cached.convert("RGB")
                return cached, cache_path
    except Exception:
        pass

    # 缺失：现裁 + 回填
    try:
        written = generate_converted_for_screens(img, photo_path, crop, [screen], converted_dir)
        saved = written.get(f"{str(screen.get('name', 'default'))}/{orient}")
        return img, saved
    except Exception:
        return img, None
