# -*- coding: utf-8 -*-
"""逆地理编码：把 GPS 坐标变成像样的中文地名。

为什么要有这个模块
------------------
原来只用本地 `world_cities_zh.csv`（geonames cities500 生成）找"最近的点"，
两个硬伤：
1. **只有 20% 的点有中文名**，其余回退成 `name_en` → 屏上出现 "Huanghua 27km"
   这种看着像乱码的拼音；
2. 它找的是"最近的地名点"，不是"这张照片在哪" —— 最近的点常常是几十公里外的
   一个村镇，写出来既不准也没意义。

所以改成：**优先高德 Web 服务 API 逆地理编码**（返回省/市/区县，中文、准确），
本地 CSV 只作兜底。没有 Key、超额、断网时自动退回本地库，绝不阻塞扫描。

额度
----
高德个人认证开发者：逆地理编码 5000 次/日（自然日零点重置），QPS 约 3。
本模块按 **约 1km 网格**（小数点后 3 位）缓存到 `geo_cache.json` ——
相册里几千张照片通常只消耗几百次额度，日常扫描几乎不会碰到上限。

配置
----
Key 有三种给法，优先级从高到低：
1. 设置页「拍摄地点」里填的 `geocode.amap_key`（存 web_settings.json）
2. `config.py` 的 `AMAP_KEY`
3. 环境变量 `AMAP_KEY`
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

# 高德逆地理编码：经度在前、纬度在后，小数点后不超过 6 位
_AMAP_URL = "https://restapi.amap.com/v3/geocode/regeo"
_HTTP_TIMEOUT = 8          # 秒
_MIN_INTERVAL = 0.36       # 秒/次 ≈ 2.8 QPS，低于个人开发者 3 QPS 的上限
_EMPTY_TTL_DAYS = 180      # "这里查不到地名"也缓存，免得每次都重查

# 中国大陆的大致经纬度范围。范围外的坐标直接交给本地库（高德海外覆盖很差）。
_CN_LAT = (3.0, 54.0)
_CN_LON = (73.0, 136.0)

_lock = threading.Lock()
_last_call = 0.0
_api_disabled_reason = ""   # 非空 = 本次进程内不再调用 API（key 无效 / 超额 / 网络不通）


def _cache_path() -> Path:
    """缓存文件位置：跟着 photos.db 走，Docker 里就是 /app/data/geo_cache.json。"""
    try:
        import config as cfg
        db = Path(str(getattr(cfg, "DB_PATH", "./photos.db") or "./photos.db"))
    except Exception:
        db = Path("./photos.db")
    if not db.is_absolute():
        db = Path.cwd() / db
    return db.parent / "geo_cache.json"


_CACHE: Optional[dict] = None


def _load_cache() -> dict:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    p = _cache_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        _CACHE = raw if isinstance(raw, dict) else {}
    except Exception:
        _CACHE = {}
    return _CACHE


def _save_cache() -> None:
    if _CACHE is None:
        return
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_CACHE, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:
        print(f"[地名] 缓存写入失败（不影响主流程）：{e}")


def _cell_key(lat: float, lon: float) -> str:
    """约 1km 的网格键（0.001° ≈ 111m 纬度、~95m 经度 @30°N）。"""
    return f"{round(float(lat), 3):.3f},{round(float(lon), 3):.3f}"


_KEY_CACHE: Optional[str] = None


def _resolve_key() -> str:
    """实际去读 Key（设置页 → config.py → 环境变量）。每次都读盘，给状态查询用。"""
    key = ""
    # 1) 设置页（web_settings.json）
    try:
        import web_settings
        s = web_settings.load()
        key = ((s.get("geocode") or {}).get("amap_key") or "").strip()
    except Exception:
        pass
    # 2) config.py
    if not key:
        try:
            import config as cfg
            key = (getattr(cfg, "AMAP_KEY", "") or "").strip()
        except Exception:
            pass
    # 3) 环境变量
    if not key:
        key = (os.environ.get("AMAP_KEY") or "").strip()
    return key


def _amap_key() -> str:
    """Key 只解析一次（每次解析都要读 web_settings.json，几千张照片就是几千次读盘）。

    设置页保存后会拉起新的 --refresh-cities 子进程，所以新 Key 会生效；
    常驻的 server 进程不查地名，缓存的旧值无所谓。
    """
    global _KEY_CACHE
    if _KEY_CACHE is None:
        _KEY_CACHE = _resolve_key()
    return _KEY_CACHE


def _text(v) -> str:
    """高德的空字段有时是 []（列表）而不是 ""，统一成去空白的字符串。"""
    if isinstance(v, str):
        return v.strip()
    return ""


def name_from_regeocode(rc: dict) -> tuple[str, str]:
    """从 regeocode 对象拼出显示名。返回 (地名, 国家)。

    规则：
    - 取「市 + 区县」拼成 "深圳市南山区"；直辖市的 city 是空串，用 province 顶。
    - 国外（country 非空且不是中国）返回 ("", country)，交给本地城市库出中文名。
    """
    if not isinstance(rc, dict):
        return "", ""
    comp = rc.get("addressComponent") or {}
    country = _text(comp.get("country"))
    if country and country != "中国":
        return "", country

    province = _text(comp.get("province"))
    city = _text(comp.get("city"))
    district = _text(comp.get("district"))

    if not city:
        city = province           # 直辖市 / 省直辖县：city 为空
    if city and district and district != city:
        return city + district, country
    if city:
        return city, country
    if district:
        return district, country
    return _text(rc.get("formatted_address")), country


def _amap_lookup(lat: float, lon: float, key: str) -> tuple[Optional[str], str]:
    """返回 (地名, 失败原因)。地名是 "" 且原因为空 = 查到了但没有可用地址。"""
    global _last_call, _api_disabled_reason

    # 距离中国太远的坐标没必要浪费额度
    if not (_CN_LAT[0] <= lat <= _CN_LAT[1] and _CN_LON[0] <= lon <= _CN_LON[1]):
        return None, "outside-cn"

    with _lock:
        wait = _MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()

    qs = urllib.parse.urlencode({
        "key": key,
        "location": f"{lon:.6f},{lat:.6f}",
        "extensions": "base",
        "radius": "1000",
        "output": "JSON",
    })
    req = urllib.request.Request(
        f"{_AMAP_URL}?{qs}",
        headers={"User-Agent": "InkTime/1.0 (photo frame)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return None, f"network:{e}"

    status = str(data.get("status", ""))
    info = str(data.get("info", ""))
    infocode = str(data.get("infocode", ""))
    if status != "1":
        # 10001 key 无效 / 10003 日额度用尽 / 10004 访问过快 / 10009 key 与平台不符
        if infocode in ("10001", "10003", "10004", "10009"):
            with _lock:
                if not _api_disabled_reason:
                    _api_disabled_reason = f"{info or '调用被拒绝'}（{infocode}）"
            print(f"[地名] 高德 API 停用：{info}（infocode={infocode}），本次退回本地城市库")
        return None, f"api:{info or infocode}"

    rc = data.get("regeocode") or {}
    name, country = name_from_regeocode(rc)
    if country and country != "中国":
        return None, "outside-cn"
    return name, ""


def reverse_geocode(lat: Optional[float], lon: Optional[float]) -> str:
    """坐标 → 中文地名（如 "深圳市南山区"）。查不到返回 ""，调用方决定怎么兜底。

    结果按 ~1km 网格缓存；同一次运行里的重复坐标不会重复消耗额度。
    """
    if lat is None or lon is None:
        return ""
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return ""

    key = _amap_key()
    if not key:
        return ""

    ck = _cell_key(lat, lon)
    cache = _load_cache()
    hit = cache.get(ck)
    if isinstance(hit, dict):
        name = hit.get("name")
        if isinstance(name, str):
            if name:
                return name
            # 空结果只在 TTL 内有效（也许高德后来补了数据）
            try:
                at = datetime.fromisoformat(hit.get("at", ""))
                if (datetime.now() - at).days < _EMPTY_TTL_DAYS:
                    return ""
            except Exception:
                return ""

    if _api_disabled_reason:
        return ""

    name, reason = _amap_lookup(lat, lon, key)
    if name is None:
        # 失败（网络/超额/国外）：不写缓存，交给本地库兜底
        return ""

    cache[ck] = {"name": name, "at": datetime.now().isoformat(timespec="seconds")}
    _save_cache()
    return name


def api_status() -> dict:
    """给设置页看的轻量状态（Key 现读，保存后不用重启 server 就能看到变化）。"""
    key = _resolve_key()
    return {
        "configured": bool(key),
        "key_tail": ("***" + key[-4:]) if len(key) >= 4 else "",
        "disabled": _api_disabled_reason,
        "cache_entries": len(_load_cache()),
        "cache_path": str(_cache_path()),
    }
