# -*- coding: utf-8 -*-
"""Web 设置持久化层。

在 config.py（Docker 里可能只读）之上叠加一个可写的 web_settings.json 覆盖层，
让设置页能改 API key / 定时扫描 / 屏幕参数 / 评分阈值，且不碰 config.py。

- load(): 读 json（不存在用 DEFAULT），与 DEFAULT 深合并（宁可缺省不丢字段）
- save(): 原子写（tmp 同目录 → os.replace）
- apply_to_config(cfg): 按 provider 覆盖 cfg.API_CHANNELS 各渠道的 api_key/enabled，
                        setattr 其它 config 项，更新 cfg.SCREEN
- rebuild_channels(analyze_mod): 用覆盖后的 cfg.API_CHANNELS 重建 analyze 模块的渠道
                                 + 重置 index/cooldown（数组长度变过）
"""
from __future__ import annotations

import json
import os
import copy
from pathlib import Path

# 设置文件位置：可用环境变量覆盖，默认放当前目录（Docker 里映射到 data 卷）
SETTINGS_PATH = Path(os.environ.get("INKTIME_SETTINGS_PATH", "web_settings.json"))

# 各提供商默认模型 / 额度端点等信息（供设置页展示）
PROVIDER_META = {
    "minimax": {
        "label": "MiniMax",
        "model": "MiniMax-M3",
        "quota_note": "请到 MiniMax 控制台查看余额",
    },
    "zhipu": {
        "label": "智谱 GLM",
        "model": "glm-4.6v",
        "quota_note": "请到智谱开放平台控制台查看余额",
    },
    "deepseek": {
        "label": "DeepSeek",
        "model": "deepseek-v4-flash-vision-exp",
        "quota_api": "https://api.deepseek.com/user/balance",
    },
}


def _default() -> dict:
    """默认设置（与 config.py 对齐的初始值）。"""
    return {
        "model": {
            "minimax_api_key": "",
            "minimax_enabled": True,
            "zhipu_api_key": "",
            "zhipu_enabled": True,
            "deepseek_api_key": "",
            "deepseek_enabled": True,
            "channel_order": ["minimax", "zhipu", "deepseek"],
        },
        "schedule": {
            "analyze_cron": "30 3 * * *",
            "analyze_enabled": True,
            "render_cron": "5 4 * * *",
            "render_enabled": True,
        },
        "screens": [
            {
                "name": "GDEP073E01",
                "width": 480,
                "height": 800,
                "text_area_height": 100,
                "palette": [
                    [0, 0, 0],
                    [255, 255, 255],
                    [200, 0, 0],
                    [220, 180, 0],
                    [0, 0, 255],
                    [0, 150, 0],
                ],
                "bin_format": "1byte_per_px",
                "enabled": True,
            },
            {
                "name": "7.09_E6",
                "width": 1200,
                "height": 1600,
                "text_area_height": 200,
                "palette": [
                    [0, 0, 0],
                    [255, 255, 255],
                    [200, 0, 0],
                    [220, 180, 0],
                    [0, 0, 255],
                    [0, 150, 0],
                ],
                "bin_format": "1byte_per_px",
                "enabled": False,
            },
        ],
        "config": {
            "MEMORY_THRESHOLD": 75.0,
            "DAILY_PHOTO_QUANTITY": 5,
            "UNMEANINGFUL_THRESHOLD": 40.0,
            "VLM_MAX_LONG_EDGE": 1024,
            "HOME_MIN_SCORE": 60.0,
            "HOME_HIDE_UNMEANINGFUL": True,
        },
        "quota": {
            "idle_start": "23:00",
            "idle_end": "07:00",
            "edge_min": 35,
            "batch_limit": 20,
            "cooldown_sec": 1800,
            "floor_percent": 10.0,
            "photo_burn_percent": 0.5,
            "enabled": True,
        },
    }


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并 override 到 base（返回新 dict，不改 base）。"""
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def load() -> dict:
    """读取设置，不存在则回退 DEFAULT。"""
    if not SETTINGS_PATH.exists():
        return _default()
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        # 文件损坏时用默认，不崩
        return _default()
    if not isinstance(raw, dict):
        return _default()
    return _deep_merge(_default(), raw)


def save(payload: dict) -> None:
    """原子写设置到磁盘（避免写一半损坏）。"""
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_PATH)


def active_screen(screens=None) -> dict:
    """返回第一个 enabled 的屏（默认屏），无则回退第一个或空 dict。"""
    if screens is None:
        screens = _default().get("screens", [])
    if not screens:
        return {}
    return next((x for x in screens if x.get("enabled", True)), screens[0])


def parse_minimax_remains(raw: dict) -> dict:
    """解析 MiniMax /v1/token_plan/remains 响应。

    真实结构（实测 2026-09-07）：
        {"model_remains": [ {model_name:"general", current_interval_remaining_percent,
                             remains_time(ms), current_interval_total_count, ...}, ... ],
         "base_resp": {status_code, status_msg}}
    关键：
        - 取 model_name=="general"（文本/视觉模型）条目，找不到则取第一个有 percent 的条目。
        - 最可靠信号是 current_interval_remaining_percent（剩余百分比，直接可用）；
          计数 total/usage 在该订阅下不可靠（常返回 0），不用于换算剩余。
        - remains_time 是**毫秒**，需 /1000 转成秒。

    Returns:
        {
            "remaining_5h": float|None,   # total - usage（可用于估算；不可靠时 None）
            "total_5h": float|None,
            "percent": float|None,         # 剩余百分比 0~100（主信号）
            "remains_time": int|None,      # 距窗口重置秒数
            "start_time": int|None,        # 毫秒时间戳
            "end_time": int|None,
            "weekly_percent": float|None,  # 每周剩余百分比（仅展示）
            "weekly_remaining": float|None,
            "weekly_total": float|None,
            "raw": dict,
        }
    """
    if not isinstance(raw, dict):
        return {"remaining_5h": None, "total_5h": None, "percent": None,
                "remains_time": None, "start_time": None, "end_time": None,
                "weekly_percent": None, "weekly_remaining": None, "weekly_total": None, "raw": {}}

    def _num(v):
        try:
            return float(v)
        except Exception:
            return None

    def _int(v):
        try:
            return int(v)
        except Exception:
            return None

    # 取 model_remains 里的 general 条目（文本/视觉），兜底第一个有 percent 的
    entries = raw.get("model_remains") if isinstance(raw.get("model_remains"), list) else []
    entry = None
    for e in entries:
        if isinstance(e, dict) and e.get("model_name") == "general":
            entry = e
            break
    if entry is None:
        for e in entries:
            if isinstance(e, dict) and e.get("current_interval_remaining_percent") is not None:
                entry = e
                break

    if entry is None:
        return {"remaining_5h": None, "total_5h": None, "percent": None,
                "remains_time": _int(raw.get("remains_time")),
                "start_time": None, "end_time": None,
                "weekly_percent": None, "weekly_remaining": None, "weekly_total": None,
                "raw": raw}

    total_5h = _num(entry.get("current_interval_total_count"))
    usage_5h = _num(entry.get("current_interval_usage_count"))
    percent = _num(entry.get("current_interval_remaining_percent"))
    remains_time_ms = _int(entry.get("remains_time"))
    # remains_time 单位是毫秒；若看起来明显是秒量级（> 1e6 才像毫秒），保守处理
    remains_time = remains_time_ms / 1000.0 if remains_time_ms is not None else None

    weekly_percent = _num(entry.get("current_weekly_remaining_percent"))
    weekly_total = _num(entry.get("current_weekly_total_count"))
    weekly_usage = _num(entry.get("current_weekly_usage_count"))

    remaining_5h = None
    if total_5h is not None and usage_5h is not None and total_5h > 0:
        remaining_5h = total_5h - usage_5h
    weekly_remaining = None
    if weekly_total is not None and weekly_usage is not None and weekly_total > 0:
        weekly_remaining = weekly_total - weekly_usage

    return {
        "remaining_5h": remaining_5h,
        "total_5h": total_5h,
        "percent": percent,
        "remains_time": remains_time,
        "start_time": _int(entry.get("start_time")),
        "end_time": _int(entry.get("end_time")),
        "weekly_percent": weekly_percent,
        "weekly_remaining": weekly_remaining,
        "weekly_total": weekly_total,
        "raw": raw,
    }


def guess_provider(model_name: str) -> str:
    """由 model_name 猜 provider，兜底返回 'other'。"""
    m = (model_name or "").lower()
    if "minimax" in m:
        return "minimax"
    if "glm" in m:
        return "zhipu"
    if "deepseek" in m:
        return "deepseek"
    return "other"


def apply_to_config(cfg) -> None:
    """把设置叠加到 config 模块（改 API_CHANNELS 的 key/enabled + 其它 config 项 + SCREEN）。

    注意：config.API_CHANNELS 的 dict 元素是共享引用，analyze 模块 list() 拷贝里
    引用的就是这些 dict，改 dict 属性会对 analyze 生效（不用换列表）。

    重要：仅当 web_settings.json **已存在**（用户从设置页保存过）时才覆盖 config.py 的默认值。
    否则（首次运行无设置文件）保持 config.py 里的原始配置（含真实 API key），避免被默认空值覆盖。
    """
    if not SETTINGS_PATH.exists():
        return
    s = load()

    # 1) 覆盖 API_CHANNELS 各家 key/enabled
    channels = getattr(cfg, "API_CHANNELS", None)
    if channels:
        m = s.get("model", {})
        for ch in channels:
            provider = ch.get("provider") or guess_provider(ch.get("model_name", ""))
            ch["api_key"] = m.get(f"{provider}_api_key", ch.get("api_key", ""))
            ch["enabled"] = bool(m.get(f"{provider}_enabled", ch.get("enabled", True)))

    # 2) 覆盖其它 config 标量
    for k, v in s.get("config", {}).items():
        try:
            setattr(cfg, k, v)
        except Exception:
            pass

    # 3) 覆盖屏幕参数（支持 SCREENS 列表，保留未在设置页暴露的 palette/bin_format）
    screens = s.get("screens")
    if screens and isinstance(screens, list) and screens:
        cfg.SCREENS = screens
        first_enabled = next((x for x in screens if x.get("enabled", True)), screens[0])
        cfg.SCREEN = first_enabled
    elif screens and isinstance(screens, dict):
        cfg.SCREEN = screens

    # 4) 覆盖 Token 调度参数（设置页键名 → config.py 属性名）
    quota = s.get("quota", {})
    if quota and isinstance(quota, dict):
        _map = {
            "idle_start": "IDLE_WINDOW_START",
            "idle_end": "IDLE_WINDOW_END",
            "edge_min": "QUOTA_EDGE_MIN",
            "batch_limit": "QUOTA_PER_RUN_BATCH_LIMIT",
            "cooldown_sec": "QUOTA_FIRE_COOLDOWN_SEC",
            "floor_percent": "QUOTA_FLOOR_PERCENT",
            "photo_burn_percent": "QUOTA_PERCENT_PER_PHOTO",
            "idle_percent": "QUOTA_FLOOR_PERCENT",  # 旧键名兜底：本来语义是"余量>N%才动"，现作烧到剩N%底线
            "enabled": "QUOTA_SCHEDULE_ENABLED",
        }
        for k, v in quota.items():
            attr = _map.get(k)
            if attr:
                setattr(cfg, attr, v)


def rebuild_channels(analyze_mod) -> None:
    """analyze 模块专用：把 config.API_CHANNELS 覆盖后的结果重建到 analyze_mod，
    并重置渠道轮询状态（index/cooldown/inflight），避免数组长度错位。"""
    import config as cfg
    channels = getattr(cfg, "API_CHANNELS", []) or []
    analyze_mod.API_CHANNELS = list(channels)
    n = len(channels)
    analyze_mod._channel_index = 0
    analyze_mod._channel_cooldown_until = [0.0] * n
    analyze_mod._channel_inflight = [0] * n
