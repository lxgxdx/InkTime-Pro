# -*- coding: utf-8 -*-
"""图片通用读取工具：支持 HEIC/RAW 等 Pillow 打不开的格式，统一转成 RGB PIL Image。

被 analyze_photos.py（VLM 打分）、server.py（/images 转码）、render_daily_photo.py（渲染）复用。
不依赖 config / cfg，避免交叉导入循环。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps

# 常见 RAW 相机格式（Pillow 打不开，需 rawpy/LibRaw 解码）
_RAW_EXTS = {".dng", ".cr2", ".nef", ".arw", ".orf", ".raf", ".rw2", ".pef"}

# 需要转码才能在浏览器显示的格式（WebUI 用）
_TRANSCODE_EXTS = {".heic", ".heif"} | set(_RAW_EXTS)


def register_heif() -> None:
    """幂等注册 HEIC 解码到 Pillow（多次调用无害）。"""
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass


def load_image_any(src) -> Image.Image:
    """读取图片并统一转成 RGB PIL Image（含 EXIF 方向纠正）。

    Args:
        src: 路径（str/Path）或原始 bytes。

    Returns:
        PIL Image（RGB 模式）
    """
    # 1) RAW 相机格式：Pillow 打不开，用 rawpy(LibRaw) 解码
    suffix = ""
    if isinstance(src, (str, Path)):
        suffix = Path(src).suffix.lower()
    if suffix in _RAW_EXTS:
        try:
            import rawpy
            with rawpy.imread(str(src)) as rf:
                rgb = rf.postprocess(use_camera_wb=True, no_auto_bright=False)
            return Image.fromarray(rgb)
        except Exception as e:
            print(f"[WARN] RAW 解码失败({Path(src).name}): {e}，退回普通读取")

    # 2) 其它：Pillow 打开（HEIC 需先 register_heif()）
    if isinstance(src, (str, Path)):
        img = Image.open(src)
    else:
        import io
        img = Image.open(io.BytesIO(src))
    try:
        img = ImageOps.exif_transpose(img)  # type: ignore
    except Exception:
        pass

    # 3) 统一 RGB：RGBA/LA 白底合成，其它 convert
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    return img
