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


def photo_fingerprint(path, screen) -> str:
    """由原图路径 + 屏幕尺寸生成稳定指纹（不含随机性），用于 CONVERTED_DIR 下文件名。

    - 避免不同原图/不同屏重名；
    - 同一原图同一屏再次生成时覆盖同名文件，天然幂等。
    """
    p = str(path)
    w = int((screen or {}).get("width", 0))
    h = int((screen or {}).get("height", 0))
    raw = f"{p}|{w}x{h}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def crop_with_ai_box(img: Image.Image, crop: Optional[dict], target_w: int, target_h: int) -> Image.Image:
    """按 AI 焦点 bbox + 目标宽高比裁剪，返回铺满 target_w x target_h 的图。

    原则（cover）：以焦点中心为锚，区域面积最小且完全包含 bbox，然后按目标宽高比
    横向/纵向伸展到覆盖整个原图；对越界做 clamp，bbox 无效则回退居中裁剪。
    """
    img = img.convert("RGB")
    img_w, img_h = img.size
    if img_w == 0 or img_h == 0:
        raise RuntimeError(f"图片尺寸非法: {img.size}")

    if target_w <= 0 or target_h <= 0:
        raise RuntimeError(f"目标尺寸非法: {target_w}x{target_h}")

    c = normalize_crop(crop)

    # 焦点 bbox 在原图上的像素范围
    bx0 = c["x"] * img_w - c["w"] * img_w / 2.0
    by0 = c["y"] * img_h - c["h"] * img_h / 2.0
    bx1 = c["x"] * img_w + c["w"] * img_w / 2.0
    by1 = c["y"] * img_h + c["h"] * img_h / 2.0
    cx = (bx0 + bx1) / 2.0
    cy = (by0 + by1) / 2.0

    # 候选框宽高 = 恰好包含 bbox 的宽高
    box_w = max(bx1 - bx0, 1.0)
    box_h = max(by1 - by0, 1.0)

    # 目标宽高比 cover：保证候选框铺满目标，主体不被裁
    target_ratio = target_w / target_h
    # 以中心为锚。先假设候选框占比正好把原图 cover 到目标比例：
    # 所需原图取景框的宽高（在"铺满目标"前提下）。
    # 我们从 bbox 出发：把 bbox 框按目标比例放大到足以覆盖整个原图。
    #
    # 设计：无论中心在哪，取一个至少覆盖 bbox、且比例 = target_ratio 的矩形，
    # 其尺寸 = max(bbox_w, bbox_h * target_ratio) 作为"核心尺寸"，再放大到覆盖全图。
    core_w = box_w
    core_h = box_h
    if box_w < box_h * target_ratio:
        core_w = box_h * target_ratio
    else:
        core_h = box_w / target_ratio

    # 覆盖整幅原图所需的放大系数：候选框要 >= 原图在"该比例 cover"下的最小取景。
    # 原图整体在目标比例下 cover 所需的最小矩形：
    need_w = img_w
    need_h = img_w / target_ratio
    if need_h < img_h:
        need_h = img_h
        need_w = img_h * target_ratio

    scale = max(need_w / core_w, need_h / core_h, 1.0)
    crop_w = core_w * scale
    crop_h = core_h * scale

    # 以焦点中心定位，clamp 到原图内
    max_left = max(0.0, img_w - crop_w)
    max_top = max(0.0, img_h - crop_h)
    left = _clamp(cx - crop_w / 2.0, 0.0, max_left)
    top = _clamp(cy - crop_h / 2.0, 0.0, max_top)

    box = (int(round(left)), int(round(top)), int(round(left + crop_w)), int(round(top + crop_h)))
    # 确保不越界（浮点舍入防御）
    box = (
        max(0, box[0]),
        max(0, box[1]),
        min(img_w, box[2]),
        min(img_h, box[3]),
    )
    if box[2] - box[0] <= 0 or box[3] - box[1] <= 0:
        # 兜底：居中 cover
        box = (0, 0, img_w, img_h)

    cropped = img.crop(box)
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
    """为每个启用屏生成一张预裁切基图，写入 converted_dir/<screen_name>/<fp>.jpg。

    Args:
        img: 已解码的 RGB 原图（analyze 阶段顺手传入，避免二次解码）。
        photo_path: 原图路径（参与指纹，保证不同照片不同文件）。
        crop: AI 给的归一化焦点框，可 None（回退居中）。
        screens: 屏幕配置列表（取 enabled==True 且含 width/height 的项）。
        converted_dir: 成品根目录。

    Returns:
        {screen_name: Path} —— 写出的成品图文件路径。
    """
    converted_dir = Path(converted_dir)
    written: dict[str, Path] = {}

    for sc in screens:
        if not sc.get("enabled", True):
            continue
        name = str(sc.get("name", "default"))
        w = int(sc.get("width", 0))
        h = int(sc.get("height", 0))
        if w <= 0 or h <= 0:
            continue
        # 成品图裁到"照片显示区"（画布减去底部文字区），铺进上方区域
        ta = int(sc.get("text_area_height", 100))
        img_area_h = max(1, h - ta)

        fp = photo_fingerprint(photo_path, sc)
        fname = f"{fp}.jpg"
        out_dir = converted_dir / name
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / fname

        try:
            img_cropped = crop_with_ai_box(img, crop, w, img_area_h)
            img_cropped.save(out_path, format="JPEG", quality=90)
            written[name] = out_path
        except Exception as e:
            print(f"[converter] 生成成品失败({name}): {e}")

    return written


def load_converted_or_crop(photo_path, crop: Optional[dict], screen: dict, converted_dir: Path):
    """优先返回已在 CONVERTED_DIR 的成品图；缺失则现裁 + 回填。

    Returns:
        (PIL.Image, Path | None) —— 图像 + 写出的成品路径（None 表示未写盘）。
    """
    try:
        converted_dir = Path(converted_dir)
        name = str(screen.get("name", "default"))
        w = int(screen.get("width", 0))
        h = int(screen.get("height", 0))
        if w > 0 and h > 0:
            fp = photo_fingerprint(photo_path, screen)
            cache_path = converted_dir / name / f"{fp}.jpg"
            if cache_path.exists():
                img = Image.open(cache_path)
                img = img.convert("RGB")
                return img, cache_path
    except Exception:
        pass

    # 缺失：现裁 + 回填
    img = load_image_any(photo_path)
    try:
        written = generate_converted_for_screens(img, photo_path, crop, [screen], converted_dir)
        saved = written.get(str(screen.get("name", "default")))
        return img, saved
    except Exception:
        return img, None
