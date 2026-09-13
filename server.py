#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from pathlib import Path
from flask import Flask, abort, send_file, Response, request, redirect
import mimetypes
import sqlite3
import json
import hmac
import html
import os
import config as cfg
from io import BytesIO
import render_daily_photo as rdp
from PIL import Image
import image_utils
from image_utils import load_image_any, _RAW_EXTS, _TRANSCODE_EXTS, register_heif
register_heif()
import threading
import subprocess
import shlex
import signal
import sys
import time
from datetime import datetime, timedelta
from croniter import croniter
import web_settings
import requests
from urllib.parse import quote

ROOT_DIR = Path(__file__).resolve().parent

# --- config ---
DOWNLOAD_KEY = str(getattr(cfg, "DOWNLOAD_KEY", "") or "").strip()
if not DOWNLOAD_KEY:
    raise SystemExit("config.py 里没有配置 DOWNLOAD_KEY")

DB_PATH = Path(str(getattr(cfg, "DB_PATH", "./photos.db") or "./photos.db")).expanduser()
if not DB_PATH.is_absolute():
    DB_PATH = (ROOT_DIR / DB_PATH).resolve()

IMAGE_DIR = Path(str(getattr(cfg, "IMAGE_DIR", "") or "")).expanduser()
if not IMAGE_DIR.is_absolute():
    IMAGE_DIR = (ROOT_DIR / IMAGE_DIR).resolve()

BIN_OUTPUT_DIR = Path(str(getattr(cfg, "BIN_OUTPUT_DIR", "./output") or "./output")).expanduser()
if not BIN_OUTPUT_DIR.is_absolute():
    BIN_OUTPUT_DIR = (ROOT_DIR / BIN_OUTPUT_DIR).resolve()
BIN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

FLASK_HOST = str(getattr(cfg, "FLASK_HOST", "0.0.0.0") or "0.0.0.0")
FLASK_PORT = int(getattr(cfg, "FLASK_PORT", 8765) or 8765)

# 是否开启照片库 WebUI（跑通后建议关闭，只保留 ESP32 下载接口）
ENABLE_REVIEW_WEBUI = bool(getattr(cfg, "ENABLE_REVIEW_WEBUI", True))

DAILY_PHOTO_QUANTITY = int(getattr(cfg, "DAILY_PHOTO_QUANTITY", 5) or 5)
if DAILY_PHOTO_QUANTITY < 1:
    DAILY_PHOTO_QUANTITY = 1

LOG_DIR = Path(os.environ.get("INKTIME_LOG_DIR", str(ROOT_DIR / "logs"))).expanduser()
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 扫描进度文件路径：analyze 子进程每处理一张就写到这里，/api/scan/progress 读它返回给网页。
PROGRESS_FILE = Path(os.environ.get("INKTIME_PROGRESS_FILE", str(LOG_DIR / "scan_progress.json"))).expanduser()
PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)

# 去重进度用【单独一份】文件：扫描与去重是两个互不相干的进程，共用一个文件的话
# 后写的会把先写的整个覆盖掉，页面上的百分比就会莫名其妙地跳。
DEDUPE_PROGRESS_FILE = Path(os.environ.get(
    "INKTIME_DEDUPE_PROGRESS_FILE", str(LOG_DIR / "dedupe_progress.json"))).expanduser()
DEDUPE_PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)

# ---- Token 感知调度参数（来自 config.py / 设置页 quota 段） ----
# 这些是 import 时从 cfg 读的快照；设置页保存后经 _refresh_quota_globals() 同步（见该函数）。
IDLE_WINDOW_START = str(getattr(cfg, "IDLE_WINDOW_START", "23:00") or "23:00")
IDLE_WINDOW_END = str(getattr(cfg, "IDLE_WINDOW_END", "07:00") or "07:00")
QUOTA_EDGE_MIN = float(getattr(cfg, "QUOTA_EDGE_MIN", 35) or 35)
QUOTA_PER_RUN_BATCH_LIMIT = int(getattr(cfg, "QUOTA_PER_RUN_BATCH_LIMIT", 20) or 20)
QUOTA_FIRE_COOLDOWN_SEC = int(getattr(cfg, "QUOTA_FIRE_COOLDOWN_SEC", 1800) or 1800)
# 烧到剩 N% 就停（应急底线）。旧键 QUOTA_IDLE_PERCENT 保留作兜底。默认 10。
QUOTA_FLOOR_PERCENT = float(getattr(cfg, "QUOTA_FLOOR_PERCENT",
                             getattr(cfg, "QUOTA_IDLE_PERCENT", 10.0)) or 10.0)
# 估算用：每张约烧窗口额度的多少 %（首次跑后看 /api/quotas 的 percent 降幅可校准）
QUOTA_PERCENT_PER_PHOTO = float(getattr(cfg, "QUOTA_PERCENT_PER_PHOTO", 0.5) or 0.5)
QUOTA_SCHEDULE_ENABLED = bool(getattr(cfg, "QUOTA_SCHEDULE_ENABLED", True))


def _refresh_quota_globals() -> None:
    """保存设置页后调用：把 cfg 的 quota 参数重新同步到本模块的模块级变量。

    apply_to_config 改的是 config 模块；但 _should_fire_analyze/_is_in_idle_window 读的是
    这里 import 时的快照。不刷新会导致设置页改了额度参数但调度器仍用旧值。"""
    global IDLE_WINDOW_START, IDLE_WINDOW_END, QUOTA_EDGE_MIN, QUOTA_PER_RUN_BATCH_LIMIT, \
           QUOTA_FIRE_COOLDOWN_SEC, QUOTA_FLOOR_PERCENT, QUOTA_PERCENT_PER_PHOTO, QUOTA_SCHEDULE_ENABLED
    IDLE_WINDOW_START = str(getattr(cfg, "IDLE_WINDOW_START", "23:00") or "23:00")
    IDLE_WINDOW_END = str(getattr(cfg, "IDLE_WINDOW_END", "07:00") or "07:00")
    QUOTA_EDGE_MIN = float(getattr(cfg, "QUOTA_EDGE_MIN", 35) or 35)
    QUOTA_PER_RUN_BATCH_LIMIT = int(getattr(cfg, "QUOTA_PER_RUN_BATCH_LIMIT", 20) or 20)
    QUOTA_FIRE_COOLDOWN_SEC = int(getattr(cfg, "QUOTA_FIRE_COOLDOWN_SEC", 1800) or 1800)
    QUOTA_FLOOR_PERCENT = float(getattr(cfg, "QUOTA_FLOOR_PERCENT", QUOTA_FLOOR_PERCENT) or QUOTA_FLOOR_PERCENT)
    QUOTA_PERCENT_PER_PHOTO = float(getattr(cfg, "QUOTA_PERCENT_PER_PHOTO", QUOTA_PERCENT_PER_PHOTO) or QUOTA_PERCENT_PER_PHOTO)
    QUOTA_SCHEDULE_ENABLED = bool(getattr(cfg, "QUOTA_SCHEDULE_ENABLED", True))

# MiniMax Token Plan 剩余额度接口（必须用 .com，.io 旁路由连不上）
_MINIMAX_REMAINS_URL = "https://api.minimaxi.com/v1/token_plan/remains"
# 配额查询缓存（避免每个 tick 都打接口）
_QUOTA_CACHE: dict = {"data": None, "fetched_at": 0.0}
_QUOTA_CACHE_TTL_SEC = 60.0


def get_minimax_remains() -> dict:
    """拉取 MiniMax token_plan/remains，带缓存。失败用缓存/空结构。"""
    now = time.time()
    ok = _QUOTA_CACHE.get("data") is not None and (now - _QUOTA_CACHE["fetched_at"]) < _QUOTA_CACHE_TTL_SEC
    if ok:
        return _QUOTA_CACHE["data"]

    result = {"ok": False, "error": "未查询", "parsed": None}
    try:
        # 找 minimax 渠道的 key
        key = ""
        for ch in getattr(cfg, "API_CHANNELS", []):
            if ch.get("provider") == "minimax" and str(ch.get("api_key", "")).strip():
                key = str(ch.get("api_key", "")).strip()
                break
        if key:
            r = requests.get(_MINIMAX_REMAINS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=12)
            if r.ok:
                parsed = web_settings.parse_minimax_remains(r.json())
                result = {"ok": True, "parsed": parsed, "raw": r.json()}
                _QUOTA_CACHE["data"] = result
                _QUOTA_CACHE["fetched_at"] = now
                return result
            result = {"ok": False, "error": f"HTTP {r.status_code}", "parsed": None}
        else:
            result = {"ok": False, "error": "未配置 minimax key", "parsed": None}
    except Exception as e:
        result = {"ok": False, "error": str(e), "parsed": None}
    # 用旧缓存兜底
    if _QUOTA_CACHE.get("data"):
        cached = _QUOTA_CACHE["data"]
        cached["error"] = f"(缓存) {result.get('error','')}"
        return cached
    return result


def _is_in_idle_window(now: datetime) -> bool:
    """当前时刻是否处于空闲时段（默认 23:00–07:00）。"""
    try:
        sh, sm = map(int, IDLE_WINDOW_START.split(":"))
        eh, em = map(int, IDLE_WINDOW_END.split(":"))
    except Exception:
        return False
    cur = now.hour * 60 + now.minute
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    if start_m <= end_m:  # 同一天内（跨午夜需分段）
        return start_m <= cur < end_m
    # 跨午夜：如 23:00-07:00
    return cur >= start_m or cur < end_m


def _estimate_batch(percent: float | None) -> int:
    """按当前剩余额度%估算"这次该扫多少张"，烧到剩 QUOTA_FLOOR_PERCENT 就停。

    线性估算：每张约烧 QUOTA_PERCENT_PER_PHOTO%。batch 被 QUOTA_PER_RUN_BATCH_LIMIT 截顶防失控。
    percent=None（查不到额度）时用硬上限保守处理。
    """
    if percent is None:
        return QUOTA_PER_RUN_BATCH_LIMIT
    burnable = max(0.0, percent - QUOTA_FLOOR_PERCENT)
    if burnable <= 0:
        return 0
    batch = int(round(burnable / QUOTA_PERCENT_PER_PHOTO)) if QUOTA_PERCENT_PER_PHOTO > 0 else QUOTA_PER_RUN_BATCH_LIMIT
    return max(1, min(batch, QUOTA_PER_RUN_BATCH_LIMIT))


def _should_fire_analyze(now: datetime) -> tuple[bool, int, str]:
    """Token 感知：是否应拉起 analyze 烧额度。返回 (ok, batch, reason)。

    用户确认策略：每个 5h 窗口末期都检查（非仅深夜）——
    - 空闲时段（深夜）：余量 > 底线(QUOTA_FLOOR_PERCENT%) → 烧
    - 非空闲：距窗口重置 < QUOTA_EDGE_MIN 分钟 且仍有余量 → 烧（窗口快清零，把余量用掉）
    batch 按剩余额度自动估算，烧到剩 QUOTA_FLOOR_PERCENT% 就停。每周窗口仅展示，不参与触发。
    """
    if not QUOTA_SCHEDULE_ENABLED:
        return False, 0, "token 调度未启用"
    q = get_minimax_remains()
    if not q.get("ok") or not q.get("parsed"):
        return False, 0, f"miniMax 额度查询失败({q.get('error','')})"
    parsed = q["parsed"]
    percent = parsed.get("percent")
    remains_time = parsed.get("remains_time")

    if percent is None:
        return False, 0, "未能读取 5h 剩余百分比"

    idle = _is_in_idle_window(now)
    edge_sec = QUOTA_EDGE_MIN * 60
    near_edge = remains_time is not None and remains_time < edge_sec

    # 触发条件：空闲时段 或 窗口将清零（每个窗口末期都检查）
    if not (idle or near_edge):
        return False, 0, f"非空闲且窗口未到边缘(剩 {int(remains_time//60) if remains_time is not None else '?'}min)"

    batch = _estimate_batch(percent)
    if batch <= 0:
        return False, 0, f"余量已到底线({percent:.0f}% ≤ {QUOTA_FLOOR_PERCENT:.0f}%)"

    cause = "空闲时段烧额度" if idle else "窗口将清零"
    return True, batch, f"{cause}(余 {percent:.0f}%, 本次约 {batch} 张)"


# ========== 后台调度器（设置页"定时扫描/渲染"） ==========

# 每个 job 的 cron/开关由 web_settings.load()["schedule"] 提供（设置页可改，即时生效）
_SCHED_TICK_SEC = 20.0  # 调度线程每 20s 醒来检查一次


def _kill_process_group(p: subprocess.Popen, grace: float = 5.0) -> None:
    """终止子进程【及其整个进程组】。

    子进程是用 `sh -c "... | tee -a log"` 起的，Popen 拿到的是 sh 的句柄。
    只 terminate 它会留下真正干活的 Python 子进程继续跑 —— analyze 会接着烧额度，
    render 会和下一次渲染抢写同一批输出目录。所以按进程组发信号。
    这要求 spawn 时设了 start_new_session=True：否则子进程与 server 同组，
    killpg 会把 server 自己一起杀掉。
    """
    if p.poll() is not None:
        return
    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            p.terminate()
    else:
        p.terminate()          # Windows 开发环境没有 killpg
    try:
        p.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        if hasattr(os, "killpg"):
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        p.kill()


class Scheduler:
    """轻量后台调度器：按 cron 表达式拉起 analyze/render 子进程。

    子进程用 subprocess.Popen（非阻塞），避免卡住 Flask；日志写 LOG_DIR。
    """

    def __init__(self):
        self.jobs: list[dict] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._proc: dict[str, subprocess.Popen] = {}   # job 名 -> 运行中的子进程
        self._cooldown_until: dict[str, float] = {}    # job 名 -> 冷却截止 (monotonic)
        self._quota_status: dict = {}                  # 供 /api/status 展示

    def reload_from_settings(self) -> None:
        """从 web_settings.load()["schedule"] 重建 jobs（设置页保存后调用）。"""
        s = web_settings.load().get("schedule", {})
        jobs = [
            {
                "name": "analyze",
                "cron": s.get("analyze_cron", "30 3 * * *"),
                "enabled": bool(s.get("analyze_enabled", True)),
                "cmd": [sys.executable, str(ROOT_DIR / "analyze_photos.py")],
                "log": "analyze.log",
                "next_run": None,
                "last_run": None,
                "quota_gated": True,
            },
            {
                "name": "render",
                "cron": s.get("render_cron", "5 4 * * *"),
                "enabled": bool(s.get("render_enabled", True)),
                "cmd": [sys.executable, str(ROOT_DIR / "render_daily_photo.py")],
                "log": "render.log",
                "next_run": None,
                "last_run": None,
                "quota_gated": False,
            },
            {
                # 去重索引：独立任务。首次全量要读遍 NAS 上每个文件（可能几十分钟），
                # 所以刻意不并进扫描流程 —— 那会跟 token 调度的窗口抢时间。
                # 建立之后的增量很快（只处理新文件/大小变了的）。
                "name": "dedupe",
                "cron": s.get("dedupe_cron", "0 2 * * *"),
                "enabled": bool(s.get("dedupe_enabled", True)),
                "cmd": [sys.executable, str(ROOT_DIR / "analyze_photos.py"), "--dedupe-only"],
                "log": "dedupe.log",
                "next_run": None,
                "last_run": None,
                "quota_gated": False,   # 不调 VLM，不烧额度，无需 token 门控
            },
        ]
        with self._lock:
            self.jobs = jobs
        self._reschedule_all()

    def _reschedule_all(self) -> None:
        now = datetime.now()
        with self._lock:
            for j in self.jobs:
                j["next_run"] = self._next_run(j["cron"], now)

    @staticmethod
    def _next_run(cron: str, base: datetime) -> datetime:
        try:
            return croniter(cron, base).get_next(datetime)
        except Exception:
            return base  # cron 非法时下个检查点再试

    def status(self) -> dict:
        """返回调度状态 + 配额/门控信息（供 /api/status）。"""
        with self._lock:
            jobs = [
                {
                    "name": j["name"],
                    "cron": j["cron"],
                    "enabled": j["enabled"],
                    "next_run": j["next_run"].isoformat() if j["next_run"] else None,
                    "last_run": j["last_run"].isoformat() if j["last_run"] else None,
                    "running": bool(self._proc.get(j["name"]) and self._proc[j["name"]].poll() is None),
                }
                for j in self.jobs
            ]
        return {
            "jobs": jobs,
            "quota": self._quota_status,
            "scan_running": self.is_analyze_running(),
            "render_running": self.is_render_running(),
            "dedupe_running": self.is_dedupe_running(),
        }

    @staticmethod
    def _job_env() -> dict:
        """子进程环境：把两个进度文件路径都注入进去。

        两个都给：扫描读 INKTIME_PROGRESS_FILE，去重读 INKTIME_DEDUPE_PROGRESS_FILE。
        三个拉起子进程的地方（cron、手动扫描、手动任务）都从这里取，免得像
        之前那样漏掉一处，去重的进度就静默地永远不写。
        """
        env = dict(os.environ)
        env["INKTIME_PROGRESS_FILE"] = str(PROGRESS_FILE)
        env["INKTIME_DEDUPE_PROGRESS_FILE"] = str(DEDUPE_PROGRESS_FILE)
        return env

    def _spawn(self, job: dict, now: datetime) -> None:
        """拉起 job 子进程（非阻塞）。analyze 走 token 门控 + 冷却 + batch_limit 注入。"""
        name = job["name"]
        # 已在跑则跳过
        p = self._proc.get(name)
        if p is not None and p.poll() is None:
            print(f"[scheduler] {name} 已在运行，跳过触发")
            return

        cmd = list(job["cmd"])
        env = self._job_env()

        if job.get("quota_gated"):
            # token 门控：返回 (ok, batch, reason)，batch 由剩余额度估算
            ok, batch, reason = _should_fire_analyze(now)
            if not ok:
                # 门控未过：把本 job 下个检查点重排，避免空转
                with self._lock:
                    job["next_run"] = now + timedelta(seconds=_SCHED_TICK_SEC)
                print(f"[scheduler] analyze 门控未过：{reason}")
                return
            # 冷却检查
            cooldown_until = self._cooldown_until.get(name, 0.0)
            if time.monotonic() < cooldown_until:
                print(f"[scheduler] analyze 冷却中，跳过")
                return
            # 注入估算的批量（烧到剩底线）
            env["INKTIME_BATCH_LIMIT"] = str(batch)
            print(f"[scheduler] analyze 触发（{reason}），batch_limit={batch}")

        log_path = LOG_DIR / job["log"]
        # tee: 子进程输出同时进日志文件 + 容器 stdout，这样 docker logs 也能实时看到扫描进度
        shell_cmd = f"{shlex.join(cmd)} 2>&1 | tee -a {shlex.quote(str(log_path))}"
        print(f"[{now:%F %T}] [scheduler] start {name}")
        proc = subprocess.Popen(
            ["sh", "-c", shell_cmd],
            cwd=str(ROOT_DIR),
            env=env,
            start_new_session=True,   # 自成进程组，停止时能整组干掉（见 _kill_process_group）
        )
        with self._lock:
            self._proc[name] = proc
            if job.get("quota_gated"):
                self._cooldown_until[name] = time.monotonic() + QUOTA_FIRE_COOLDOWN_SEC

    # ---------- 手动控制（网页按钮） ----------

    def _get_job(self, name: str) -> dict | None:
        with self._lock:
            for j in self.jobs:
                if j["name"] == name:
                    return j
        return None

    def start_analyze_manual(self, rescan: bool = False) -> tuple[bool, str]:
        """网页点「按额度烧到底线」：立即拉起 analyze（绕过 token 门控）。

        遵守规则：已有一个 analyze 在跑则跳过；batch 由剩余额度自动估算（烧到剩 QUOTA_FLOOR_PERCENT%）。
        查不到额度/已到底线时，退化为 batch_limit 上限（用户主动点按仍可扫）。
        rescan=True 时注入 INKTIME_RESCAN=1，强制重扫已入库照片（改动后刷新评分/方向）。
        """
        job = self._get_job("analyze")
        if job is None:
            return False, "找不到 analyze 任务"
        p = self._proc.get("analyze")
        if p is not None and p.poll() is None:
            return False, "正在扫描中，请稍候"

        # 估算本次批量：按剩余额度烧到剩底线。查不到额度用硬上限。
        q = get_minimax_remains()
        percent = q.get("parsed", {}).get("percent") if q.get("ok") else None
        batch = _estimate_batch(percent)
        if batch <= 0:
            batch = QUOTA_PER_RUN_BATCH_LIMIT  # 已到底线：手动点按仍允许按上限扫
        if percent is not None:
            msg = f"已开始扫描，本次估算 {batch} 张（当前剩余额度 {percent:.0f}%）"
        else:
            msg = f"已开始扫描，本次按上限 {batch} 张"
        if rescan:
            msg = "已开始【重新扫描】，将重新打分已入库照片（" + msg.replace("已开始扫描，", "")

        # 注入估算的批量 + 可选重扫标记
        env = self._job_env()
        env["INKTIME_BATCH_LIMIT"] = str(batch)
        if rescan:
            env["INKTIME_RESCAN"] = "1"
        log_path = LOG_DIR / job["log"]
        now = datetime.now()
        # tee: 手动扫描日志也实时进 docker logs
        shell_cmd = f"{shlex.join(list(job['cmd']))} 2>&1 | tee -a {shlex.quote(str(log_path))}"
        print(f"[{now:%F %T}] [scheduler] 手动开始扫描，batch_limit={batch}(余 {percent if percent is not None else '?'}%)")
        proc = subprocess.Popen(
            ["sh", "-c", shell_cmd],
            cwd=str(ROOT_DIR),
            env=env,
            start_new_session=True,   # 自成进程组，停止时能整组干掉（见 _kill_process_group）
        )
        with self._lock:
            self._proc["analyze"] = proc
            # 下次自动触发推迟一点，避免刚手动跑完又自动跑
            now2 = datetime.now()
            for j in self.jobs:
                if j["name"] == "analyze":
                    j["next_run"] = now2 + timedelta(seconds=_SCHED_TICK_SEC)
        return True, msg

    def stop_analyze_manual(self) -> tuple[bool, str]:
        """网页点「停止扫描」：终止正在跑的 analyze 子进程。"""
        p = self._proc.get("analyze")
        if p is None or p.poll() is not None:
            return False, "当前没有在扫描"
        try:
            _kill_process_group(p)
            print("[scheduler] 手动停止扫描")
            return True, "已停止扫描"
        except Exception as e:
            return False, f"停止失败：{e}"

    def is_analyze_running(self) -> bool:
        p = self._proc.get("analyze")
        return p is not None and p.poll() is None

    # ---------- 手动出图（「换一张」按钮） ----------

    def is_render_running(self) -> bool:
        p = self._proc.get("render")
        return p is not None and p.poll() is None

    def is_dedupe_running(self) -> bool:
        p = self._proc.get("dedupe")
        return p is not None and p.poll() is None

    def start_job_manual(self, name: str, extra_args: list[str] | None = None,
                         msg_ok: str = "已开始") -> tuple[bool, str]:
        """手动拉起某个 job（网页按钮用）。

        用 _proc[name] 槽位做互斥：连点两次不会起两个进程抢写同一批文件。
        不注入 INKTIME_BATCH_LIMIT —— render/dedupe 都不调 VLM、不烧额度
        （只有 analyze 需要额度门控，它有专门的 start_analyze_manual）。
        """
        job = self._get_job(name)
        if job is None:
            return False, f"找不到任务 {name}"
        p = self._proc.get(name)
        if p is not None and p.poll() is None:
            return False, "该任务正在运行，请稍候"

        cmd = list(job["cmd"]) + list(extra_args or [])
        env = self._job_env()
        log_path = LOG_DIR / job["log"]
        now = datetime.now()
        shell_cmd = f"{shlex.join(cmd)} 2>&1 | tee -a {shlex.quote(str(log_path))}"
        print(f"[{now:%F %T}] [scheduler] 手动启动 {name} {' '.join(extra_args or '')}".rstrip())
        proc = subprocess.Popen(
            ["sh", "-c", shell_cmd],
            cwd=str(ROOT_DIR),
            env=env,
            start_new_session=True,
        )
        with self._lock:
            self._proc[name] = proc
            for j in self.jobs:
                if j["name"] == name:
                    j["next_run"] = datetime.now() + timedelta(seconds=_SCHED_TICK_SEC)
        return True, msg_ok

    def start_render_manual(self, extra_args: list[str] | None = None) -> tuple[bool, str]:
        """网页点「换一张」：立刻重选一批照片出图并覆盖 latest.bin。"""
        return self.start_job_manual(
            "render", extra_args, "已开始换一张，稍等几秒刷新即可看到新图")

    def start_dedupe_manual(self) -> tuple[bool, str]:
        """网页点「重建去重索引」：跑一次 analyze_photos.py --dedupe-only。"""
        return self.start_job_manual(
            "dedupe", None, "已开始重建去重索引（首次全量较慢，之后是增量）")

    def stop_render_manual(self) -> tuple[bool, str]:
        p = self._proc.get("render")
        if p is None or p.poll() is not None:
            return False, "当前没有在出图"
        try:
            _kill_process_group(p)
            return True, "已停止出图"
        except Exception as e:
            return False, f"停止失败：{e}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = datetime.now()
                with self._lock:
                    due = [
                        j for j in self.jobs
                        if j["enabled"] and j["next_run"] is not None and j["next_run"] <= now
                    ]
                    for j in due:
                        j["last_run"] = now
                        j["next_run"] = self._next_run(j["cron"], now)
                # 刷新配额状态（供 /api/status 展示），失败不影响调度。存 dict {fire,batch,reason,percent}
                try:
                    _ok, _batch, _reason = _should_fire_analyze(now)
                    _pct = None
                    try:
                        _pct = get_minimax_remains().get("parsed", {}).get("percent")
                    except Exception:
                        pass
                    self._quota_status = {"fire": _ok, "batch": _batch, "reason": _reason, "percent": _pct}
                except Exception as e:
                    self._quota_status = {"fire": False, "batch": 0, "reason": f"查询失败:{e}", "percent": None}
                for j in due:
                    self._spawn(j, now)
            except Exception as e:
                print(f"[scheduler] 调度异常：{e}")
            self._stop.wait(_SCHED_TICK_SEC)


scheduler = Scheduler()


# review 分页：每页 100 张
REVIEW_PAGE_SIZE = 100

# /review 日期筛选的可用 MM-DD 列表缓存（避免每次都扫全库）
_MD_CACHE: dict[str, object] = {"md_list": [], "built_at": 0.0}
_MD_CACHE_TTL_SEC = 300.0  # 5 分钟

def _load_all_md_list() -> list[str]:
    """从全库提取所有存在的 MM-DD（去重、排序）。用于前端“随机一天”。"""
    if not DB_PATH.exists():
        return []

    # 简单 TTL 缓存
    import time
    now = time.time()
    try:
        built_at = float(_MD_CACHE.get("built_at") or 0.0)
    except Exception:
        built_at = 0.0
    if (now - built_at) < _MD_CACHE_TTL_SEC:
        cached = _MD_CACHE.get("md_list")
        if isinstance(cached, list):
            return [str(x) for x in cached]

    conn = _open_db()
    c = conn.cursor()
    rows = c.execute("SELECT exif_json FROM photo_scores").fetchall()
    conn.close()

    s: set[str] = set()
    for (exif_json,) in rows:
        d = extract_date_from_exif(exif_json)
        if d and len(d) >= 10:
            md = d[5:10]
            if len(md) == 5 and md[2] == "-":
                s.add(md)

    md_list = sorted(s)
    _MD_CACHE["md_list"] = md_list
    _MD_CACHE["built_at"] = now
    return md_list

app = Flask(__name__)
def _require_webui_enabled() -> None:
    if not ENABLE_REVIEW_WEBUI:
        abort(404)


def _safe_join(base: Path, rel: str) -> Path:
    """防目录穿越：只允许 base 下的相对路径。

    用 is_relative_to 而非字符串前缀比较 —— 前缀比较会把同级的
    output_evil/ 误判成 output/ 的子路径放行。
    """
    root = base.resolve()
    p = (base / rel).resolve()
    if not p.is_relative_to(root):
        raise ValueError("path traversal blocked")
    return p


def _check_download_key(key: str) -> None:
    """校验 ESP 下载密钥。

    用 hmac.compare_digest 而非 == —— 后者是逐字符短路比较，理论上可从
    响应耗时侧信道推断密钥。这个 key 是守护用户整个照片库的 bearer token。
    """
    if not hmac.compare_digest(str(key), str(DOWNLOAD_KEY)):
        abort(404)


def _open_db() -> sqlite3.Connection:
    """打开 photos.db（复用 analyze_photos.open_db 的 WAL / busy_timeout 设置）。

    延迟 import analyze_photos：它拉 requests 等一批依赖，模块顶层 import 会
    拖慢 server 启动，而网页多数请求并不需要它。import 失败则退回裸连接 ——
    网页只读查询不该因为分析模块出问题而整体不可用。
    """
    try:
        import analyze_photos
        # 显式传本模块的 DB_PATH —— open_db 默认用 analyze_photos 的库路径，
        # 两者生产环境一致，但传参才不会在路径被改写时静默连错库。
        return analyze_photos.open_db(DB_PATH)
    except Exception:
        return sqlite3.connect(DB_PATH, timeout=30.0)


def _read_progress_file(path: Path) -> dict:
    """读一份进度快照。文件不存在、内容坏掉、不是 dict —— 一律返回空 dict。

    进度是增强信息，任何异常都不该让接口 500。
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _send_static_file(p: Path) -> Response:
    if not p.exists() or not p.is_file():
        abort(404)

    if p.suffix.lower() == ".bin":
        return send_file(p, mimetype="application/octet-stream", as_attachment=False)

    mt, _ = mimetypes.guess_type(str(p))
    if mt:
        return send_file(p, mimetype=mt, as_attachment=False)
    return send_file(p, as_attachment=False)


# ---------- 相框设备回执 ----------
# 不改固件：设备每次拉 bin 都要经过 esp_* 路由，顺手记下"谁在什么时候拉了哪个文件"。
# 固件一接入，设置页立刻就有在线状态可看。
_DEVICE_STATE_PATH = LOG_DIR / "device_state.json"
_DEVICE_LOCK = threading.Lock()
_DEVICE_STATE: dict | None = None        # 进程内缓存，避免每次请求都读盘
_DEVICE_LAST_FLUSH = 0.0
_DEVICE_FLUSH_INTERVAL = 10.0            # 最快每 10 秒落一次盘
_DEVICE_RECENT_MAX = 50                  # 环形缓冲长度


def _load_device_state() -> dict:
    global _DEVICE_STATE
    if _DEVICE_STATE is None:
        try:
            _DEVICE_STATE = json.loads(_DEVICE_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            _DEVICE_STATE = {}
        if not isinstance(_DEVICE_STATE, dict):
            _DEVICE_STATE = {}
        _DEVICE_STATE.setdefault("recent", [])
        _DEVICE_STATE.setdefault("by_target", {})
        _DEVICE_STATE.setdefault("pull_count", 0)
    return _DEVICE_STATE


def _write_device_state(st: dict) -> None:
    """原子写（tmp + os.replace）—— 否则并发读会读到半个 JSON。"""
    try:
        _DEVICE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DEVICE_STATE_PATH.with_name(_DEVICE_STATE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _DEVICE_STATE_PATH)
    except Exception as e:
        print(f"[WARN] 写设备状态失败（不影响拉取）: {e}")


def _touch_device(screen: str, direction: str, filename: str) -> None:
    """记录一次设备拉取。

    设备刷新一轮会并发发起多个请求，所以这里有三个讲究：
      · 模块级锁保护读改写，否则并发下丢更新、甚至写出坏 JSON；
      · 状态留在进程内存，最多每 10 秒落一次盘，不然每个请求都要重写文件；
      · 落盘走原子写。
    """
    global _DEVICE_LAST_FLUSH
    now_iso = datetime.now().isoformat(timespec="seconds")
    try:
        ip = request.remote_addr or ""
        ua = (request.headers.get("User-Agent") or "")[:120]
    except Exception:
        ip, ua = "", ""      # 非请求上下文（例如被单测直接调用）
    with _DEVICE_LOCK:
        st = _load_device_state()
        st["last_seen_at"] = now_iso
        st["last_ip"] = ip
        st["last_ua"] = ua
        st["pull_count"] = int(st.get("pull_count") or 0) + 1
        target = f"{screen}/{direction}" if screen else "顶层(兼容路径)"
        st["by_target"][target] = {"last_file": filename, "at": now_iso}
        st["recent"].insert(0, {"at": now_iso, "screen": screen,
                                "direction": direction, "filename": filename})
        del st["recent"][_DEVICE_RECENT_MAX:]
        if time.monotonic() - _DEVICE_LAST_FLUSH >= _DEVICE_FLUSH_INTERVAL:
            _write_device_state(st)
            _DEVICE_LAST_FLUSH = time.monotonic()


def _send_image(p: Path) -> Response:
    """发送图片给浏览器：HEIC/RAW 转码成 JPEG，普通图直接 send_file。

    原因：浏览器不支持 HEIC / DNG 等格式，直接 send_file 会白屏/下载失败。
    """
    if p.suffix.lower() in _TRANSCODE_EXTS:
        try:
            img = load_image_any(p)
            # 控制返回体积，避免 review 页几十张 HEIC 全量解码拖慢
            img.thumbnail((1440, 1440), Image.LANCZOS)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=88)
            buf.seek(0)
            return send_file(buf, mimetype="image/jpeg", as_attachment=False, conditional=True)
        except Exception as e:
            print(f"[WARN] 图片转码失败({p.name}): {e}，落回原文件")
            # 落回原文件（对 JPG/PNG 无影响；坏 HEIC 会让浏览器报错，可接受）
    return _send_static_file(p)


def _make_image_url(path_str: str) -> str:
    """
    把数据库里的本地图片路径转换成 HTTP 可访问的 /images/... 路径。
    要求图片在 IMAGE_DIR 目录下；不在则返回空，避免 file:// 污染与 canvas 跨域。
    """
    try:
        p = Path(path_str).expanduser().resolve()
        rel = p.relative_to(IMAGE_DIR.resolve())
        return "/images/" + str(rel).replace("\\", "/")
    except Exception:
        return ""


# --------------------------
# DB helpers
# --------------------------


def ensure_review_table():
    """确保 photo_scores 表存在（第一次打开网页时自动建表，避免 no such table）。

    复用 analyze_photos.ensure_table 的逻辑；analyze 尚未跑过时，网页也能打开（空状态）。
    """
    try:
        import analyze_photos
        conn = _open_db()
        analyze_photos.ensure_table(conn)
        conn.close()
    except Exception as e:
        print(f"[review] 建表失败（不影响浏览，仅提示）：{e}")


def load_rows(page: int = 1, page_size: int = REVIEW_PAGE_SIZE, md: str = "", sort: str = "memory",
              min_score: float | None = None, hide_unmeaningful: bool = False):
    """分页读取 review 数据。支持按 MM-DD 过滤与排序。返回 (rows, total_count)。

    数据库不存在 / 表不存在 / 空库时，都返回空列表，让网页显示"还没有照片"，而不是报错 500。
    min_score: 只返回 memory_score >= 该值的照片（None 不过滤）。
    hide_unmeaningful: 隐藏 is_meaningful=0 的照片（列不存在时自动跳过，不报错）。
    """
    if page < 1:
        page = 1
    if page_size < 1:
        page_size = REVIEW_PAGE_SIZE

    # 文件不存在 → 先建表，然后走空库路径
    if not DB_PATH.exists():
        ensure_review_table()

    # 表不一定存在（analyze 未跑过）。做一个轻量探测，失败则按空库返回。
    try:
        conn = _open_db()
        # 探测表是否存在
        conn.execute("SELECT 1 FROM photo_scores LIMIT 1")
    except sqlite3.OperationalError:
        # no such table 等 → 按空库处理，报个提示
        print("[review] photo_scores 表尚不存在，按空库返回（运行 analyze 后会有内容）")
        return [], 0
    except Exception as e:
        print(f"[review] 读取数据库失败：{e}")
        return [], 0

    c = conn.cursor()

    # 从 exif_json 里提取 datetime，再拼 MM-DD
    # 期望格式："YYYY:MM:DD HH:MM:SS"（extract_date_from_exif 也按这个假设）
    dt_expr = "json_extract(exif_json, '$.datetime')"
    md_expr = f"(substr({dt_expr}, 6, 2) || '-' || substr({dt_expr}, 9, 2))"

    where_sql = ""
    params: list[object] = []

    # 探测 is_meaningful 列是否存在（旧库可能没有），缺列时跳过 hide_unmeaningful 条件
    has_meaningful_col = any(r[1] == "is_meaningful" for r in c.execute("PRAGMA table_info(photo_scores)").fetchall())

    md = (md or "").strip()
    if md and len(md) == 5 and md[2] == "-":
        where_sql = f"WHERE {dt_expr} IS NOT NULL AND {md_expr} = ?"
        params.append(md)

    # 叠加最低回忆度过滤（主页默认只显示高分）
    if min_score is not None:
        cond = f"COALESCE(memory_score, -1) >= ?"
        where_sql = (where_sql + " AND " + cond) if where_sql else ("WHERE " + cond)
        params.append(min_score)

    # 叠加隐藏无意义（仅当列存在）
    if hide_unmeaningful and has_meaningful_col:
        cond = "COALESCE(is_meaningful, 0) = 1"
        where_sql = (where_sql + " AND " + cond) if where_sql else ("WHERE " + cond)

    # total_count 也要跟随过滤
    if where_sql:
        total_count = c.execute(f"SELECT COUNT(1) FROM photo_scores {where_sql}", params).fetchone()[0]
    else:
        total_count = c.execute("SELECT COUNT(1) FROM photo_scores").fetchone()[0]

    # 排序
    sort = (sort or "memory").strip()
    # ⚠️ 下面的 path 必须写全 photo_scores.path：LEFT JOIN photo_hashes 之后两表都有
    # path 列，裸写 path 会报 "ambiguous column name"
    if sort == "beauty":
        order_sql = ("ORDER BY COALESCE(beauty_score, -1) DESC, "
                     "COALESCE(memory_score, -1) DESC, photo_scores.path")
    elif sort == "time_new":
        # 直接按 datetime 字符串排序（固定格式下可按字典序比较）；NULL 放最后
        order_sql = f"ORDER BY ({dt_expr} IS NULL) ASC, {dt_expr} DESC, photo_scores.path"
    elif sort == "time_old":
        order_sql = f"ORDER BY ({dt_expr} IS NULL) ASC, {dt_expr} ASC, photo_scores.path"
    else:
        # 默认 memory
        order_sql = ("ORDER BY COALESCE(memory_score, -1) DESC, "
                     "COALESCE(beauty_score, -1) DESC, photo_scores.path")

    # 分页偏移量（第 588 行 SQL 里的 OFFSET ? 用）
    offset = (page - 1) * page_size

    # is_meaningful 列可能不存在（旧库），缺列时用 NULL 占位，避免 SELECT 报错
    meaning_col = "is_meaningful" if has_meaningful_col else "NULL AS is_meaningful"

    # 去重标记在独立的 photo_hashes 表里，可能整张表都还没建（从未跑过去重任务）
    try:
        has_hash_table = bool(c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='photo_hashes'").fetchone())
    except Exception:
        has_hash_table = False
    if has_hash_table:
        dup_cols = "ph.dup_of, ph.dup_kind"
        dup_join = "LEFT JOIN photo_hashes ph ON ph.path = photo_scores.path"
    else:
        dup_cols = "NULL AS dup_of, NULL AS dup_kind"
        dup_join = ""

    base_sql = f"""
        SELECT photo_scores.path,
               caption,
               type,
               memory_score,
               beauty_score,
               reason,
               exif_json,
               width,
               height,
               orientation,
               used_at,
               side_caption,
               {meaning_col},
               {dup_cols}
        FROM photo_scores
        {dup_join}
        {where_sql}
        {order_sql}
        LIMIT ? OFFSET ?
    """

    q_params = list(params) + [page_size, offset]
    rows = c.execute(base_sql, q_params).fetchall()

    conn.close()
    return rows, int(total_count)


def load_sim_rows():
    if not DB_PATH.exists():
        raise SystemExit(f"找不到数据库文件: {DB_PATH}")

    conn = _open_db()
    c = conn.cursor()

    rows = c.execute(
        """
        SELECT path,
               caption,
               type,
               memory_score,
               beauty_score,
               reason,
               side_caption,
               exif_json,
               width,
               height,
               orientation,
               used_at,
               exif_gps_lat,
               exif_gps_lon,
               exif_city
        FROM photo_scores
        """
    ).fetchall()

    conn.close()
    return rows


# 新增：只加载指定日期集合的照片，加速 /sim
def load_sim_rows_for_dates(dates: list[str]):
    """只加载指定日期（YYYY-MM-DD）集合内的照片，用于 /sim 加速。"""
    if not dates:
        return []
    if not DB_PATH.exists():
        raise SystemExit(f"找不到数据库文件: {DB_PATH}")

    # 过滤掉不合法日期字符串，避免 SQL 注入（虽然我们用参数化，但也别喂垃圾）
    safe_dates = []
    for d in dates:
        d = (d or "").strip()
        if len(d) == 10 and d[4] == "-" and d[7] == "-":
            safe_dates.append(d)
    if not safe_dates:
        return []

    conn = _open_db()
    c = conn.cursor()

    dt_expr = "json_extract(exif_json, '$.datetime')"
    # exif datetime 形如 YYYY:MM:DD HH:MM:SS，取前 10 位并把 : 替换成 - -> YYYY-MM-DD
    date_expr = f"replace(substr({dt_expr}, 1, 10), ':', '-')"

    placeholders = ",".join(["?"] * len(safe_dates))
    sql = f"""
        SELECT path,
               caption,
               type,
               memory_score,
               beauty_score,
               reason,
               side_caption,
               exif_json,
               width,
               height,
               orientation,
               used_at,
               exif_gps_lat,
               exif_gps_lon,
               exif_city
        FROM photo_scores
        WHERE {dt_expr} IS NOT NULL
          AND {date_expr} IN ({placeholders})
    """

    rows = c.execute(sql, tuple(safe_dates)).fetchall()
    conn.close()
    return rows

def get_photo_meta_by_path(abs_path: str):
    """
    从 DB 找到渲染需要的字段：date/side/lat/lon/city。
    abs_path 必须是数据库里 photo_scores.path 的原值（通常是绝对路径）。
    """
    if not DB_PATH.exists():
        return None

    conn = _open_db()
    c = conn.cursor()
    row = c.execute(
        """
        SELECT path,
               exif_json,
               side_caption,
               memory_score,
               exif_gps_lat,
               exif_gps_lon,
               exif_city
        FROM photo_scores
        WHERE path = ?
        LIMIT 1
        """,
        (abs_path,),
    ).fetchone()
    conn.close()

    if not row:
        return None

    path, exif_json, side_caption, memory_score, gps_lat, gps_lon, exif_city = row
    date_str = extract_date_from_exif(exif_json)
    if not date_str:
        return None

    return {
        "path": str(path),
        "date": date_str,
        "side": side_caption or "",
        "memory": float(memory_score) if memory_score is not None else None,
        "lat": gps_lat,
        "lon": gps_lon,
        "city": exif_city or "",
    }

def summarize_exif(exif_json: str | None) -> str:
    if not exif_json:
        return ""

    try:
        data = json.loads(exif_json)
    except Exception:
        return ""

    dtv = data.get("datetime")
    make = data.get("make")
    model = data.get("model")
    iso = data.get("iso")
    exp = data.get("exposure_time")
    fnum = data.get("f_number")
    fl = data.get("focal_length")
    lat = data.get("gps_lat")
    lon = data.get("gps_lon")

    parts = []
    if dtv:
        parts.append(f"时间: {dtv}")
    if make or model:
        cam = f"{make or ''} {model or ''}".strip()
        if cam:
            parts.append(f"设备: {cam}")
    exp_parts = []
    if iso:
        exp_parts.append(f"ISO {iso}")
    if exp:
        exp_parts.append(f"快门 {exp}")
    if fnum:
        exp_parts.append(f"光圈 {fnum}")
    if fl:
        exp_parts.append(f"焦距 {fl}")
    if exp_parts:
        parts.append(" / ".join(exp_parts))
    if lat is not None and lon is not None:
        try:
            parts.append(f"GPS: {float(lat):.5f}, {float(lon):.5f}")
        except Exception:
            parts.append(f"GPS: {lat}, {lon}")

    return "；".join(str(p) for p in parts if p)


def extract_date_from_exif(exif_json: str | None) -> str:
    if not exif_json:
        return ""
    try:
        data = json.loads(exif_json)
    except Exception:
        return ""
    dtv = data.get("datetime")
    if not dtv:
        return ""
    try:
        date_part = str(dtv).split()[0]  # "2018:03:18"
        parts = date_part.replace(":", "-").split("-")
        if len(parts) >= 3:
            return f"{parts[0]}-{parts[1]}-{parts[2]}"
    except Exception:
        return ""
    return ""


# --------------------------
# HTML builders
# --------------------------

# 全局后台任务状态条（扫描 / 去重共用一份实现）。
#
# 照片库主页有两个版本（空库版与正常版）分别由不同函数返回，这段 JS 原本是各抄
# 一份的 —— 去重之前没进度，两边抄漏了也没人发现。改成单一来源，插入点用
# /*__POLL_TASKS__*/ 标记（见 _build_empty_review_html 与 build_html）。
#
# 用原始字符串：里面的 JS 正则 /[\\/]/ 要原样落到页面上，普通字符串的转义规则
# 会让它少一层反斜杠，Windows 路径就切不出文件名了。
_POLL_TASKS_JS = r"""
// —— 全局后台任务状态条 ——
// 扫描和去重哪个在跑就显示哪个（两个同时跑时优先显示扫描）；每 2 秒轮询一次，
// 从照片库切到别的页再切回来也不会丢。
function _taskBarRender(el, title, percent, detail){
  el.style.display = 'block';
  el.style.background = '#16181d'; el.style.border = '1px solid #2a2e37';
  el.style.borderRadius = '14px'; el.style.padding = '12px 14px'; el.style.margin = '0 auto 16px';
  el.style.maxWidth = '560px';
  el.innerHTML =
    '<div style="font-size:13px;color:#9cffd6;font-weight:600;margin-bottom:6px">' + title + '</div>' +
    '<div style="display:flex;align-items:center;gap:10px">' +
      '<div style="flex:1;height:12px;background:#2a2e37;border-radius:6px;overflow:hidden">' +
        '<div style="height:100%;width:' + Math.min(100, Math.max(0, percent)) + '%;background:linear-gradient(90deg,#3fb58a,#54d6a4);border-radius:6px"></div>' +
      '</div>' +
      '<div style="font-size:12px;color:#8a93a3;width:70px;text-align:right">' + percent + '%</div>' +
    '</div>' +
    '<div style="font-size:12px;color:#8a93a3;margin-top:6px">' + detail + '</div>';
}

function _taskDur(sec){
  var s = Math.round(sec || 0);
  if (s < 60) return s + ' 秒';
  var m = Math.floor(s / 60);
  if (m < 60) return m + ' 分 ' + (s % 60) + ' 秒';
  return Math.floor(m / 60) + ' 小时 ' + (m % 60) + ' 分';
}

function pollGlobalTasks(){
  var bar = document.getElementById('globalTaskBar');
  if (!bar) return;
  fetch('/api/status').then(function(r){ return r.json(); }).then(function(st){
    if (st.scan_running) {
      fetch('/api/scan/progress').then(function(r){ return r.json(); }).then(function(p){
        var pp = (p && p.progress) || {};
        var cur = pp.current ? String(pp.current).split(/[\\/]/).pop() : '';
        var amt = (pp.total > 0)
          ? ('已处理 ' + (pp.done||0) + '/' + pp.total)
          : ('正在枚举文件，已发现 ' + (pp.done||0) + ' 个');
        _taskBarRender(bar, '⏳ 后台任务运行中 · 正在扫描照片', pp.percent || 0, amt + (cur ? ' · ' + cur : ''));
      }).catch(function(){});
    } else if (st.dedupe_running) {
      fetch('/api/dedupe/progress').then(function(r){ return r.json(); }).then(function(p){
        var pp = (p && p.progress) || {};
        // 进程刚拉起、还没写出第一份进度时 pp 是空的：这时别写"处理中"（等于没说），
        // 明说"正在启动" —— 大相册光 import + 列目录就要一会儿。
        var stg = pp.stage ? ('第 ' + pp.stage + '/' + (pp.stages || 4) + ' 步 · ') : '';
        var who = pp.phase || (pp.stage ? '' : '正在启动，请稍候…');
        var amt = (pp.total > 0) ? ((pp.done||0) + '/' + pp.total)
                                 : (pp.done ? ('已发现 ' + pp.done + ' 个文件') : '');
        var eta = pp.eta ? ('　预计还需 ' + _taskDur(pp.eta)) : '';
        _taskBarRender(bar, '🔍 后台任务运行中 · 正在重建去重索引', pp.percent || 0,
                       stg + who + (amt ? '　' + amt : '') + eta);
      }).catch(function(){});
    } else {
      bar.style.display = 'none';
    }
  }).catch(function(){});
  setTimeout(pollGlobalTasks, 2000);
}
"""


def _build_empty_review_html() -> str:
    """空库时的照片库主页：引导去设置页填密钥 / 直接点扫描，而不是 404。"""
    return """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>InkTime 照片库</title>
<style>
:root{--bg:#0b0c10;--panel:#16181d;--card:#1d2027;--text:#e6e8ee;--muted:#8a93a3;--line:#2a2e37;--accent:#8ab4ff;--accent2:#9cffd6;--radius:14px;}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;margin:0;padding:24px;}
.card{max-width:560px;margin:8vh auto;background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:32px;text-align:center;}
h1{font-size:22px;margin:0 0 10px;font-weight:600}
p{color:var(--muted);font-size:14px;line-height:1.7;margin:0 0 20px}
.btns{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}
button{background:var(--accent);color:#0b0c10;border:none;border-radius:8px;padding:12px 22px;font-size:15px;font-weight:600;cursor:pointer}
button.ghost{background:var(--card);color:var(--text);border:1px solid var(--line)}
button:disabled{opacity:.5;cursor:not-allowed}
a{color:var(--accent)}
#scanMsg{display:block;margin-top:16px;font-size:13px;color:var(--accent2)}
</style>
</head>
<body>
<div id="globalTaskBar" style="display:none"></div>
<div class="card">
  <div class="btns">
    <button type="button" class="primary" onclick="startScan()">⏯ 开始扫描</button>
    <button type="button" class="ghost" onclick="location.href='/settings'">⚙ 去设置（填密钥）</button>
  </div>
  <span id="scanMsg"></span>
  <p style="margin-top:20px;margin-bottom:0">扫描完成、打了分之后回到本页就能看到照片卡片。</p>
  <p style="color:var(--muted);font-size:13px;margin-top:12px;margin-bottom:0">显示不了？可能是低于设置的分值，<a href="/review?show_all=1">点这里显示全部</a>。</p>
</div>
<script>
async function startScan(){
  const msg = document.getElementById('scanMsg');
  const btn = document.querySelector('button.primary');
  if(msg) msg.textContent = '正在启动…';
  if(btn){ btn.disabled = true; }
  try{
    const r = await fetch('/api/scan/start', {method:'POST'});
    const j = await r.json();
    if(msg) msg.textContent = (j.ok ? '✅ 已开始扫描（正在后台处理）' : (j.msg || '未开始'));
  }catch(e){ if(msg) msg.textContent = '启动失败: '+e; }
  setTimeout(()=>{ location.reload(); }, 15000);   // 15 秒后自动刷新看进度
}

/*__POLL_TASKS__*/
pollGlobalTasks();
</script>
</body>
</html>""".replace("/*__POLL_TASKS__*/", _POLL_TASKS_JS)


def build_html(rows, page: int, page_size: int, total_count: int):
    items_html = []

    # 当前列表页的完整 URL（含分页/筛选/排序），编码后带给 /sim，这样它"返回"时
    # 能回到同一页同一筛选，而不是永远跳回第一页。
    try:
        back_url = (request.full_path or "/review").rstrip("?")
    except Exception:
        back_url = "/review"
    back_q = quote(back_url, safe="")

    for path, caption, ptype, m_score, b_score, reason, exif_json, width, height, orientation, used_at, side_caption, _meaningful, _dup_of, _dup_kind in rows:
        safe_caption = html.escape(caption or "").replace("\n", "<br>")
        safe_side = html.escape(side_caption or "").replace("\n", "<br>")
        safe_type = html.escape(ptype or "")
        safe_reason = html.escape(reason or "")
        exif_summary = summarize_exif(exif_json)
        safe_exif = html.escape(exif_summary or "")

        date_str = extract_date_from_exif(exif_json)
        safe_date = html.escape(date_str or "")

        md_str = ""
        if date_str and len(date_str) >= 10:
            md_str = date_str[5:10]
        safe_md = html.escape(md_str or "")

        res_str = ""
        if width and height:
            try:
                res_str = f"{int(width)} x {int(height)}"
            except Exception:
                res_str = f"{width} x {height}"
        orient_str = orientation or ""
        used_str = used_at or ""
        # 去重标记：这张被判定为冗余，dup_of 指向保留的那张
        dup_html = ""
        if _dup_of:
            kind_zh = {"exact": "内容相同", "burst": "连拍", "similar": "视觉相似"}.get(
                _dup_kind or "", "重复")
            dup_html = f' · <span style="color:#e08a3c">🔁 {kind_zh}，未送打分</span>'

        img_uri = _make_image_url(str(path))
        if not img_uri:
            continue

        score_html = ""
        if m_score is not None or b_score is not None:
            parts = []
            if m_score is not None:
                parts.append(f"回忆度: {m_score:.1f}")
            if b_score is not None:
                parts.append(f"美观度: {b_score:.1f}")
            score_line = " / ".join(parts)
            score_html = f'<div class="score">{score_line}</div>'

        type_html = f'<div class="type">类型: {safe_type}</div>' if safe_type else ""
        exif_html = f'<div class="exif">{safe_exif}</div>' if safe_exif else ""
        reason_html = f'<div class="reason">理由: {safe_reason}</div>' if safe_reason else ""

        items_html.append(f"""
        <div class="item"
             data-date="{safe_date}"
             data-md="{safe_md}"
             data-memory="{m_score if m_score is not None else ''}"
             data-beauty="{b_score if b_score is not None else ''}">
            <div class="img-wrap">
                <a class="img-link" href="/sim?img={quote(img_uri, safe='')}&back={back_q}" title="打开该照片的模拟器" onclick="window.stop(); rememberScroll();">
                    <img src="{img_uri}" loading="lazy">
                </a>
            </div>
            {f'<div class="side-under">{safe_side}</div>' if safe_side else ''}
            <div class="meta">
                <div class="path">{html.escape(str(path))}</div>
                {type_html}
                {score_html}
                {reason_html}
                {exif_html}
                <div class="extra">
                    {f"拍摄日期: {safe_date}" if safe_date else ""}
                    {(" · 分辨率: " + html.escape(res_str)) if res_str else ""}
                    {(" · 方向: " + html.escape(orient_str)) if orient_str else ""}
                    {(" · 已上屏: " + html.escape(used_str)) if used_str else ""}
                    {dup_html}
                </div>
                <div class="caption">{safe_caption}</div>
            </div>
        </div>
        """)

    items_str = "\n".join(items_html)
    total_pages = (total_count + page_size - 1) // page_size

    # 从请求参数回填（用于显示）
    md_q = (request.args.get("md", "") or "").strip()
    sort_q = (request.args.get("sort", "") or "memory").strip() or "memory"
    md_hint = f" · 筛选日期 {html.escape(md_q)}" if (md_q and len(md_q) == 5) else ""

    html_str = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>InkTime照片数据库</title>
  <style>
    :root{{
      --bg: #0b0c10;
      --panel: rgba(255,255,255,0.06);
      --card: rgba(255,255,255,0.10);
      --card2: rgba(255,255,255,0.08);
      --text: rgba(255,255,255,0.92);
      --muted: rgba(255,255,255,0.62);
      --muted2: rgba(255,255,255,0.48);
      --line: rgba(255,255,255,0.14);
      --accent: #8ab4ff;
      --accent2:#9cffd6;
      --shadow: 0 18px 60px rgba(0,0,0,0.45);
      --shadow2: 0 10px 28px rgba(0,0,0,0.35);
      --radius: 14px;
    }}
    body{{
      margin:0;
      padding:0;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
      background: radial-gradient(1200px 800px at 20% 0%, rgba(138,180,255,0.18), transparent 45%),
                  radial-gradient(900px 700px at 90% 20%, rgba(156,255,214,0.14), transparent 55%),
                  linear-gradient(180deg, #07080b 0%, #0b0c10 40%, #0b0c10 100%);
      color: var(--text);
    }}
    .container{{
      max-width: 1320px;
      margin: 26px auto 60px;
      padding: 0 18px;
    }}
    h1{{
      font-size: 22px;
      margin: 0 0 8px;
      letter-spacing: 0.2px;
    }}
    .subtitle{{
      font-size: 13px;
      color: var(--muted);
      margin: 0 0 14px;
      line-height: 1.35;
    }}

    .controls{{
      display:flex;
      flex-wrap:wrap;
      gap: 10px;
      align-items:center;
      margin: 12px 0 14px;
      font-size: 13px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 10px 12px;
      box-shadow: var(--shadow2);
      backdrop-filter: blur(10px);
    }}
    .controls label{{
      display:inline-flex;
      align-items:center;
      gap: 8px;
      color: var(--muted);
      white-space: nowrap;
    }}
    .controls select{{
      padding: 7px 10px;
      font-size: 13px;
      color: var(--text);
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(255,255,255,0.16);
      border-radius: 10px;
      outline: none;
    }}
    .controls select:focus{{
      border-color: rgba(138,180,255,0.7);
      box-shadow: 0 0 0 3px rgba(138,180,255,0.16);
    }}
    .controls button{{
      padding: 7px 12px;
      font-size: 13px;
      cursor: pointer;
      color: var(--text);
      background: rgba(255,255,255,0.10);
      border: 1px solid rgba(255,255,255,0.16);
      border-radius: 10px;
      transition: transform .08s ease, background .15s ease, border-color .15s ease, opacity .15s ease;
    }}
    .controls button:hover{{
      background: rgba(255,255,255,0.14);
      border-color: rgba(255,255,255,0.26);
    }}
    .controls button:active{{
      transform: translateY(1px);
    }}
    .controls button:disabled{{
      opacity: 0.45;
      cursor: not-allowed;
    }}
    .controls.pager{{
      background: rgba(255,255,255,0.05);
    }}

    .status{{
      font-size: 12px;
      color: var(--muted);
      margin: 8px 0 12px;
    }}

    .grid{{
      display:grid;
      grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
      gap: 16px;
    }}
    .item{{
      background: linear-gradient(180deg, var(--card) 0%, var(--card2) 100%);
      border: 1px solid rgba(255,255,255,0.14);
      border-radius: var(--radius);
      overflow: hidden;
      box-shadow: var(--shadow2);
      display:flex;
      flex-direction:column;
      transition: transform .12s ease, border-color .15s ease, box-shadow .15s ease;
    }}
    .item:hover{{
      transform: translateY(-2px);
      border-color: rgba(138,180,255,0.38);
      box-shadow: var(--shadow);
    }}

    .img-wrap{{
      width:100%;
      background: rgba(0,0,0,0.55);
      display:flex;
      align-items:center;
      justify-content:center;
      max-height: 260px;
      overflow:hidden;
    }}
    .img-wrap img{{
      width:100%;
      height:auto;
      display:block;
      object-fit: cover;
      filter: saturate(1.04) contrast(1.02);
    }}
    .img-link{{ display:block; width:100%; }}
    .img-link:link, .img-link:visited{{ text-decoration:none; }}

    .side-under{{
      padding: 10px 12px 0;
      font-size: 12px;
      color: var(--text);
      line-height: 1.45;
      word-break: break-word;
      opacity: 0.92;
    }}

    .meta{{
      padding: 10px 12px 12px;
      font-size: 13px;
      color: var(--text);
    }}
    .path{{
      font-size: 11px;
      color: var(--muted2);
      margin-bottom: 6px;
      word-break: break-all;
    }}
    .type{{
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 4px;
    }}
    .score{{
      font-size: 13px;
      font-weight: 650;
      margin-bottom: 6px;
      color: var(--accent2);
    }}
    .reason{{
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 6px;
      line-height: 1.45;
    }}
    .exif{{
      font-size: 11px;
      color: var(--muted2);
      margin-bottom: 8px;
      line-height: 1.45;
    }}
    .extra{{
      font-size: 11px;
      color: var(--muted2);
      margin-bottom: 8px;
      line-height: 1.45;
    }}
    .caption{{
      margin-top: 6px;
      font-size: 13px;
      line-height: 1.55;
      color: var(--text);
    }}

    @media (max-width: 560px){{
      .container{{ padding: 0 14px; }}
      .grid{{ grid-template-columns: 1fr; }}
      .controls{{ gap: 8px; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <h1>InkTime照片数据库</h1>
    <div class="subtitle">
      数据库：{html.escape(str(DB_PATH))}{md_hint} · 当前页 {page} · 本页 {len(rows)} 张 · 总计 {total_count} 张（每页 {page_size} 张）
    </div>

    <div id="globalTaskBar" style="display:none"></div>

    <div class="controls">
      <label>
        月份：
        <select id="monthFilter">
          <option value="">全部</option>
          <option value="01">1 月</option><option value="02">2 月</option><option value="03">3 月</option>
          <option value="04">4 月</option><option value="05">5 月</option><option value="06">6 月</option>
          <option value="07">7 月</option><option value="08">8 月</option><option value="09">9 月</option>
          <option value="10">10 月</option><option value="11">11 月</option><option value="12">12 月</option>
        </select>
      </label>
      <label>
        日期：
        <select id="dayFilter">
          <option value="">全部</option>
          {''.join([f'<option value="{i:02d}">{i} 日</option>' for i in range(1, 32)])}
        </select>
      </label>
      <label>
        排序：
        <select id="sortBy">
          <option value="memory">按回忆度</option>
          <option value="beauty">按美观度</option>
          <option value="time_new">按时间（新→旧）</option>
          <option value="time_old">按时间（旧→新）</option>
        </select>
      </label>
      <button type="button" id="randomDateBtn">随机一天</button>
      <button type="button" id="homeBtn">回到首页</button>
      <button type="button" onclick="location.href='/settings'">设置</button>
      <label style="display:inline-flex;align-items:center;gap:6px"><input type="checkbox" id="showAllToggle" style="width:auto">显示全部（含低分/无意义）</label>
    </div>

    <div class="controls pager" style="justify-content: space-between;">
      <div>
        <button type="button" id="prevPageBtn">上一页</button>
        <button type="button" id="nextPageBtn">下一页</button>
      </div>
      <div class="subtitle" style="margin:0;">第 <span id="pageNum">{page}</span> 页 / 共 <span id="pageTotal">{total_pages}</span> 页</div>
    </div>

    <div class="status" id="statusLine"></div>

    <div class="grid">
      {items_str}
    </div>

    <div class="controls pager" style="justify-content: space-between; margin-top: 18px;">
      <div>
        <button type="button" id="prevPageBtnBottom">上一页</button>
        <button type="button" id="nextPageBtnBottom">下一页</button>
      </div>
      <div class="subtitle" style="margin:0;">第 <span>{page}</span> 页 / 共 <span>{total_pages}</span> 页</div>
    </div>
  </div>

  <script>
    document.addEventListener('DOMContentLoaded', function () {{
      const monthSelect = document.getElementById('monthFilter');
      const daySelect = document.getElementById('dayFilter');
      const sortSelect = document.getElementById('sortBy');
      const statusLine = document.getElementById('statusLine');
      const randomBtn = document.getElementById('randomDateBtn');
      const homeBtn = document.getElementById('homeBtn');

      const currentPage = {page};
      const totalPages = {total_pages};
      const prevBtn = document.getElementById('prevPageBtn');
      const nextBtn = document.getElementById('nextPageBtn');
      const prevBtnBottom = document.getElementById('prevPageBtnBottom');
      const nextBtnBottom = document.getElementById('nextPageBtnBottom');

      // 任何跳转前先中断当前页面的图片/资源加载，避免请求排队导致“点击无响应”
      function navigateTo(urlStr) {{
        try {{
          window.stop();
        }} catch (e) {{
          // ignore
        }}
        window.location.href = urlStr;
      }}

      function getParams() {{
        const url = new URL(window.location.href);
        const md = (url.searchParams.get('md') || '').trim();
        const sort = (url.searchParams.get('sort') || '').trim() || 'memory';
        const page = parseInt(url.searchParams.get('page') || '1', 10) || 1;
        const show_all = (url.searchParams.get('show_all') || '') === '1';
        return {{ url, md, sort, page, show_all }};
      }}

      function setSelectsFromUrl() {{
        const p = getParams();
        // sort
        if (sortSelect) sortSelect.value = p.sort;
        // md -> month/day
        if (p.md && p.md.length === 5 && p.md.indexOf('-') === 2) {{
          const parts = p.md.split('-');
          if (parts.length === 2) {{
            if (monthSelect) monthSelect.value = parts[0];
            if (daySelect) daySelect.value = parts[1];
          }}
          if (statusLine) statusLine.textContent = '当前筛选：' + p.md + '（全库）';
        }} else {{
          if (monthSelect) monthSelect.value = '';
          if (daySelect) daySelect.value = '';
          if (statusLine) statusLine.textContent = '';
        }}
        // "显示全部" checkbox 状态回填
        const showAllToggle = document.getElementById('showAllToggle');
        if (showAllToggle) showAllToggle.checked = p.show_all;
      }}

      function buildReviewUrl(md, sort, page) {{
        const url = new URL(window.location.href);
        url.pathname = '/review';
        if (md && md.length === 5 && md.indexOf('-') === 2) url.searchParams.set('md', md);
        else url.searchParams.delete('md');
        if (sort) url.searchParams.set('sort', sort);
        else url.searchParams.delete('sort');
        url.searchParams.set('page', String(page || 1));
        // 透传"显示全部"开关（读当前 URL 的 show_all，翻页/筛选不丢失）
        const curShowAll = (new URL(window.location.href)).searchParams.get('show_all');
        if (curShowAll === '1') url.searchParams.set('show_all', '1');
        else url.searchParams.delete('show_all');
        return url.toString();
      }}

      function goPage(p) {{
        const params = getParams();
        navigateTo(buildReviewUrl(params.md, params.sort, p));
      }}

      function goHome() {{
        const params = getParams();
        navigateTo(buildReviewUrl('', params.sort || 'memory', 1));
      }}

      async function pickRandomDate() {{
        // 从后端拿“真实存在的日期集合”，前端随机一个，然后让后端按 md 过滤
        try {{
          // 先停止当前页面的图片加载，释放连接
          try {{ window.stop(); }} catch (e) {{}}
          const resp = await fetch('/api/md_list');
          if (!resp.ok) throw new Error('HTTP ' + resp.status);
          const data = await resp.json();
          const arr = Array.isArray(data) ? data : (Array.isArray(data.md_list) ? data.md_list : []);
          if (!arr.length) {{
            if (statusLine) statusLine.textContent = '全库没有任何可用日期（exif datetime 缺失）。';
            return;
          }}
          const idx = Math.floor(Math.random() * arr.length);
          const md = String(arr[idx] || '').trim();
          const params = getParams();
          navigateTo(buildReviewUrl(md, params.sort || 'memory', 1));
        }} catch (e) {{
          if (statusLine) statusLine.textContent = '随机失败：' + e;
        }}
      }}

      function onMonthDayChange() {{
        const mVal = (monthSelect && monthSelect.value) ? monthSelect.value : '';
        const dVal = (daySelect && daySelect.value) ? daySelect.value : '';
        const sortBy = (sortSelect && sortSelect.value) ? sortSelect.value : 'memory';

        if (!mVal && !dVal) {{
          navigateTo(buildReviewUrl('', sortBy, 1));
          return;
        }}
        if (mVal && dVal) {{
          const md = mVal + '-' + dVal;
          navigateTo(buildReviewUrl(md, sortBy, 1));
          return;
        }}
        // 只选了一个，不跳转，避免生成无意义的 md
      }}

      function onSortChange() {{
        const params = getParams();
        const sortBy = (sortSelect && sortSelect.value) ? sortSelect.value : 'memory';
        navigateTo(buildReviewUrl(params.md, sortBy, 1));
      }}

      // 分页按钮
      if (prevBtn) {{
        prevBtn.disabled = currentPage <= 1;
        prevBtn.addEventListener('click', () => goPage(Math.max(1, currentPage - 1)));
      }}
      if (nextBtn) {{
        nextBtn.disabled = currentPage >= totalPages;
        nextBtn.addEventListener('click', () => goPage(Math.min(totalPages, currentPage + 1)));
      }}
      if (prevBtnBottom) {{
        prevBtnBottom.disabled = currentPage <= 1;
        prevBtnBottom.addEventListener('click', () => goPage(Math.max(1, currentPage - 1)));
      }}
      if (nextBtnBottom) {{
        nextBtnBottom.disabled = currentPage >= totalPages;
        nextBtnBottom.addEventListener('click', () => goPage(Math.min(totalPages, currentPage + 1)));
      }}

      if (monthSelect) monthSelect.addEventListener('change', onMonthDayChange);
      if (daySelect) daySelect.addEventListener('change', onMonthDayChange);
      if (sortSelect) sortSelect.addEventListener('change', onSortChange);
      if (randomBtn) randomBtn.addEventListener('click', pickRandomDate);
      if (homeBtn) homeBtn.addEventListener('click', goHome);

      // "显示全部"开关切换：勾选/取消后带 show_all 重新加载本页
      const showAllToggle = document.getElementById('showAllToggle');
      if (showAllToggle) {{
        showAllToggle.addEventListener('change', () => {{
          const params = getParams();
          const url = new URL(window.location.href);
          url.pathname = '/review';
          if (showAllToggle.checked) url.searchParams.set('show_all', '1');
          else url.searchParams.delete('show_all');
          // 保留当前 md/sort/page
          if (params.md) url.searchParams.set('md', params.md);
          if (params.sort && params.sort !== 'memory') url.searchParams.set('sort', params.sort);
          url.searchParams.set('page', String(params.page || 1));
          navigateTo(url.toString());
        }});
      }}

      // 兜底：用户在图片疯狂加载时点击任何链接/按钮，先 stop()，避免导航请求排队
      document.addEventListener('click', function (ev) {{
        const t = ev.target;
        if (!t) return;
        const a = t.closest ? t.closest('a') : null;
        const btn = t.closest ? t.closest('button') : null;
        // 只要是链接或按钮点击，就先中断当前加载
        if (a || btn) {{
          try {{ window.stop(); }} catch (e) {{}}
        }}
      }}, true);

      setSelectsFromUrl();
    }});

    // —— 全局后台任务状态条：无论在哪页都显示正在跑的任务（切页不丢）——
    pollGlobalTasks();
  </script>

  <script>
{_POLL_TASKS_JS}

    // 列表滚动位置记忆：点进模拟器再返回时，回到原来的位置。
    // 只按「路径+查询串」匹配，所以翻页/换筛选后不会错误地跳到别处。
    function rememberScroll(){{
      try{{
        sessionStorage.setItem('inktime_scroll', JSON.stringify({{
          url: location.pathname + location.search,
          y: Math.round(window.scrollY)
        }}));
      }}catch(e){{}}
    }}
    (function restoreScroll(){{
      try{{
        const raw = sessionStorage.getItem('inktime_scroll');
        if(!raw) return;
        const d = JSON.parse(raw);
        if(!d || d.url !== location.pathname + location.search || !d.y) return;
        const jump = () => window.scrollTo(0, d.y);
        jump();                                   // 先试一次
        window.addEventListener('load', jump);    // 图片加载完、高度稳定后再对齐
        setTimeout(jump, 400);
      }}catch(e){{}}
    }})();
  </script>
</body>
</html>
"""
    return html_str


def build_simulator_html(sim_rows, selected_img: str = "", back_url: str = "/review"):
    # 空数据时不要做任何无意义的循环，避免前端 JS 大对象
    if not sim_rows:
        sim_rows = []
    items = []

    # 屏幕参数（多分辨率，跟随 cfg.SCREEN / 设置页）
    _screen = dict(getattr(cfg, "SCREEN", {}) or {})
    CANVAS_W = int(_screen.get("width", 480))
    CANVAS_H = int(_screen.get("height", 800))

    # 屏幕下拉：详情页切换分辨率用（列出已启用的屏）
    _opts = []
    for _s in (getattr(cfg, "SCREENS", None) or []):
        if not isinstance(_s, dict) or not _s.get("enabled", True):
            continue
        _nm = str(_s.get("name", ""))
        _w, _h = int(_s.get("width", 0)), int(_s.get("height", 0))
        if _nm and _w > 0 and _h > 0:
            _opts.append(f'<option value="{html.escape(_nm)}">{html.escape(_nm)} · {_w}×{_h}</option>')
    screen_opts = "".join(_opts)

    def _parse_tags(ptype_val) -> list[str]:
        """把 DB 的 type 字段解析成 tag 数组。
        兼容三种常见存储：
        - JSON 数组：   ["人物","日常"]
        - 伪数组文本：  [人物, 日常] / [人物，日常]
        - 普通字符串：  人物,日常 / 人物
        注意：这里是容错解析，目的是不让 /sim 因坏数据 500。
        """
        if ptype_val is None:
            return []
        s = str(ptype_val).strip()
        if not s:
            return []

        # 1) 先尝试严格 JSON
        if s.startswith("[") and s.endswith("]"):
            try:
                arr = json.loads(s)
                if isinstance(arr, list):
                    out = []
                    for x in arr:
                        t = str(x).strip()
                        if t:
                            out.append(t)
                    return out
            except Exception:
                # JSON 不合法：继续走容错
                pass

        # 2) 容错：去掉最外层 [] 以及引号，然后按逗号/中文逗号切
        if s.startswith("[") and s.endswith("]"):
            s = s[1:-1].strip()

        # 去掉可能出现的引号
        s = s.replace('"', '').replace("'", "")

        parts = [p.strip() for p in s.replace('，', ',').split(',')]
        out = [p for p in parts if p]
        return out
    for (
        path,
        caption,
        ptype,
        memory_score,
        beauty_score,
        reason,
        side_caption,
        exif_json,
        width,
        height,
        orientation,
        used_at,
        gps_lat,
        gps_lon,
        exif_city,
    ) in sim_rows:
        date_str = extract_date_from_exif(exif_json)
        if not date_str:
            continue
        img_uri = _make_image_url(str(path))
        if not img_uri:
            continue

        # tags: 保证为数组，优先解析 JSON/容错
        type_value = _parse_tags(ptype)

        items.append({
            "path": img_uri,
            "date": date_str,
            "memory": float(memory_score) if memory_score is not None else None,
            "beauty": float(beauty_score) if beauty_score is not None else None,
            "city": exif_city or "",
            "lat": gps_lat,
            "lon": gps_lon,
            "side": side_caption or "",
            "caption": caption or "",
            "type": type_value,
            "reason": reason or "",
            "exif_json": exif_json or "",
            "exif_summary": summarize_exif(exif_json) if exif_json else "",
            "width": width if width is not None else "",
            "height": height if height is not None else "",
            "orientation": orientation or "",
            "used_at": used_at or "",
        })

    data_json = json.dumps(items, ensure_ascii=False).replace("</", "<\\/") if items else "[]"
    selected_json = json.dumps(selected_img or "", ensure_ascii=False).replace("</", "<\\/")

    html_str = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>墨水屏渲染效果预览</title>
  <style>
    :root {{
      --bg: #0b0c10;
      --panel: rgba(255,255,255,0.06);
      --line: rgba(255,255,255,0.14);
      --text: rgba(255,255,255,0.92);
      --muted: rgba(255,255,255,0.62);
      --muted2: rgba(255,255,255,0.48);
      --accent: #8ab4ff;
      --accent2: #9cffd6;
      --shadow: 0 18px 60px rgba(0,0,0,0.45);
      --shadow2: 0 10px 28px rgba(0,0,0,0.35);
      --radius: 14px;
    }}
    body {{
      margin:0; padding:0;
      font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;
      background: radial-gradient(1200px 800px at 20% 0%, rgba(138,180,255,0.18), transparent 45%),
                  radial-gradient(900px 700px at 90% 20%, rgba(156,255,214,0.14), transparent 55%),
                  linear-gradient(180deg, #07080b 0%, #0b0c10 40%, #0b0c10 100%);
      color: var(--text);
    }}
    .container {{
      max-width: 1120px;
      margin: 22px auto 42px;
      padding: 0 16px;
    }}
    a.back {{
      display:inline-block;
      margin-bottom: 10px;
      color: var(--accent);
      text-decoration: none;
    }}
    h1 {{
      font-size: 22px;
      margin: 0 0 8px;
      letter-spacing: 0.2px;
    }}
    .subtitle {{
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 14px;
      line-height: 1.45;
    }}
    .controls {{
      display:flex;
      align-items:center;
      gap: 10px;
      margin-bottom: 14px;
      font-size: 13px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 10px 12px;
      box-shadow: var(--shadow2);
      backdrop-filter: blur(10px);
    }}
    .controls button {{
      padding: 7px 12px;
      font-size: 13px;
      cursor: pointer;
      color: var(--text);
      background: rgba(255,255,255,0.10);
      border: 1px solid rgba(255,255,255,0.16);
      border-radius: 10px;
      transition: transform .08s ease, background .15s ease, border-color .15s ease, opacity .15s ease;
    }}
    .controls button:hover {{
      background: rgba(255,255,255,0.14);
      border-color: rgba(255,255,255,0.26);
    }}
    .controls button:active {{
      transform: translateY(1px);
    }}

    .status {{
      font-size: 12px;
      color: var(--muted);
      margin: 6px 0 10px;
      min-height: 16px;
    }}

    .preview-wrap {{
      display:flex;
      flex-wrap:wrap;
      gap: 20px;
      align-items: flex-start;
    }}
    .canvas-box {{
      /* 画布容器固定一个宽度：横图 800 宽也等比缩进同一栏，于是
         ① 横竖切换时布局不跳动；② 信息区永远在右侧，不会被挤到下一行。 */
      flex: 0 0 auto;
      width: min(440px, 100%);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 12px;
      box-shadow: var(--shadow2);
      backdrop-filter: blur(10px);
    }}
    .canvas-box h2 {{
      font-size: 13px;
      margin: 0 0 8px;
      color: rgba(255,255,255,0.78);
    }}
    #previewCanvas {{
      display:block;
      width:100%;            /* 在容器内等比缩放：横图 800 宽也缩到同一栏宽 */
      height:auto;
      background:#fff;
      border: 1px solid rgba(255,255,255,0.18);
      border-radius: 10px;
    }}
    .sel-wrap {{
      display:inline-flex;
      align-items:center;
      gap:7px;
      font-size:13px;
      color:var(--muted);
      margin-left:auto;      /* 推到控制栏右端，和左边的按钮拉开层次 */
    }}
    .sel-wrap select {{
      background: rgba(255,255,255,0.06);
      color: var(--text);
      border: 1px solid var(--line);
      border-radius: 9px;
      padding: 7px 10px;
      font-size: 13px;
      cursor: pointer;
    }}
    .sel-wrap select:focus {{ outline:none; border-color: rgba(255,255,255,0.34); }}

    .meta-box {{
      flex: 1;
      min-width: 320px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 16px 18px;
      box-shadow: var(--shadow2);
      backdrop-filter: blur(10px);
      font-size: 16px;
      line-height: 1.75;
    }}
    .meta-title {{
      font-size: 13px;
      color: rgba(255,255,255,0.78);
      margin: 0 0 10px;
    }}
    .kpi {{
      display:grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-bottom: 12px;
      padding-bottom: 10px;
      border-bottom: 1px solid rgba(255,255,255,0.10);
    }}
    .kpi .cell {{
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 12px;
      padding: 10px;
    }}
    .kpi .label {{
      font-size: 11px;
      color: var(--muted2);
      margin-bottom: 4px;
    }}
    .kpi .value {{
      font-size: 16px;
      font-weight: 700;
      color: var(--text);
      line-height: 1.2;
      word-break: break-word;
    }}
    .kpi .value.accent {{
      color: var(--accent2);
    }}

    .field {{
      display:flex;
      gap: 10px;
      margin-bottom: 8px;
      line-height: 1.45;
      font-size: 12px;
    }}
    .field .label {{
      width: 92px;
      flex: 0 0 92px;
      color: var(--muted2);
    }}
    .field .value {{
      flex: 1;
      color: var(--text);
      word-break: break-word;
    }}
    .mono {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      font-size: 11px;
      color: rgba(255,255,255,0.80);
      white-space: pre-wrap;
      word-break: break-word;
      background: rgba(0,0,0,0.22);
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 12px;
      padding: 10px;
    }}

    .section {{
      padding-top: 10px;
      margin-top: 10px;
      border-top: 1px solid rgba(255,255,255,0.10);
    }}
    .section:first-of-type {{
      padding-top: 0;
      margin-top: 0;
      border-top: none;
    }}
    .section-title {{
      display:flex;
      align-items:center;
      justify-content: space-between;
      gap: 10px;
      font-size: 12px;
      color: rgba(255,255,255,0.78);
      margin: 0 0 8px;
      letter-spacing: .2px;
    }}

    .chips {{
      display:flex;
      flex-wrap:wrap;
      gap: 8px;
      margin: 12px 0 14px;
    }}
    .chip {{
      display:inline-flex;
      align-items:center;
      gap: 6px;
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 12px;
      line-height: 1;
      border: 1px solid rgba(255,255,255,0.18);
      background: rgba(255,255,255,0.07);
      color: rgba(255,255,255,0.92);
      user-select: none;
    }}
    .chip-dot {{
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: rgba(255,255,255,0.7);
      flex: 0 0 8px;
    }}

    .big-text {{
      font-size: 13px;
      line-height: 1.55;
      color: rgba(255,255,255,0.92);
      padding: 10px 12px;
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 12px;
      word-break: break-word;
      white-space: pre-wrap;
    }}

    details.fold {{
      background: rgba(255,255,255,0.04);
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 12px;
      padding: 10px 12px;
      margin-top: 18px;
      font-size: 14px;
    }}
    details.fold > summary {{
      cursor: pointer;
      list-style: none;
      outline: none;
      color: rgba(255,255,255,0.86);
      font-size: 12px;
      display:flex;
      align-items:center;
      justify-content: space-between;
      gap: 10px;
    }}
    details.fold > summary::-webkit-details-marker {{ display: none; }}
    .fold-hint {{
      color: rgba(255,255,255,0.55);
      font-size: 11px;
    }}

    .kv-grid {{
      display:grid;
      grid-template-columns: 92px 1fr;
      gap: 8px 10px;
      margin-top: 10px;
      font-size: 12px;
      line-height: 1.45;
    }}
    .kv-k {{
      color: rgba(255,255,255,0.52);
    }}
    .kv-v {{
      color: rgba(255,255,255,0.92);
      word-break: break-word;
    }}

    .hero-text {{
      font-size: 26px;
      line-height: 1.7;
      font-weight: 650;
      margin-bottom: 18px;
      color: rgba(255,255,255,0.98);
      word-break: break-word;
      white-space: pre-wrap;
    }}

    .sub-text {{
      font-size: 17px;
      line-height: 1.8;
      color: rgba(255,255,255,0.90);
      margin: 14px 0 18px;
      word-break: break-word;
      white-space: pre-wrap;
    }}

    .score-bars {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
      margin: 18px 0 20px;
    }}

    .score-row {{
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 14px;
      color: rgba(255,255,255,0.75);
    }}

    .score-track {{
      position: relative;
      flex: 1;
      height: 10px;
      background: rgba(255,255,255,0.12);
      border-radius: 999px;
      overflow: hidden;
    }}

    .score-fill {{
      position: absolute;
      left: 0; top: 0; bottom: 0;
      width: 0%;
      border-radius: 999px;
    }}

    .score-fill.memory {{ background: linear-gradient(90deg, #6fd6ff, #9cffd6); }}
    .score-fill.beauty {{ background: linear-gradient(90deg, #ffd36f, #ff9f6f); }}

    .score-num {{
      width: 44px;
      text-align: right;
      font-variant-numeric: tabular-nums;
      color: rgba(255,255,255,0.9);
    }}

    #fieldReason {{
      font-size: 16px;
      line-height: 1.8;
      margin-top: 6px;
    }}

    @media (max-width: 560px) {{
      .kpi {{ grid-template-columns: 1fr; }}
      .meta-box {{ min-width: 0; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <a class="back" href="{html.escape(back_url)}">← 返回 Review</a>
    <a class="back" href="/settings" style="margin-left:14px">⚙ 设置</a>
    <h1>墨水屏渲染效果预览</h1>
    <div class="subtitle">
      画布：<span id="canvasSize">{CANVAS_W} × {CANVAS_H}</span>&nbsp;&nbsp;
      <span style="display:inline-flex; gap:6px; vertical-align:middle;">
        <span style="width:10px;height:10px;box-sizing:border-box;border-radius:50%;background:#000;border:1px solid rgba(255,255,255,0.70);"></span>
        <span style="width:10px;height:10px;box-sizing:border-box;border-radius:50%;background:#fff;border:1px solid rgba(255,255,255,0.45);"></span>
        <span style="width:10px;height:10px;box-sizing:border-box;border-radius:50%;background:#c80000;border:1px solid rgba(255,255,255,0.18);"></span>
        <span style="width:10px;height:10px;box-sizing:border-box;border-radius:50%;background:#e0b400;border:1px solid rgba(255,255,255,0.18);"></span>
      </span>
    </div>

    <div class="controls">
      <button type="button" id="rerollBtn">同一天换一张</button>
      <label class="sel-wrap">屏幕
        <select id="screenSel">{screen_opts}</select>
      </label>
    </div>

    <div class="status" id="statusLine"></div>

    <div class="preview-wrap">
      <div class="canvas-box">
        <canvas id="previewCanvas" width="{CANVAS_W}" height="{CANVAS_H}"></canvas>
      </div>

      <div class="meta-box">
        <div class="hero-text" id="kpiSide"></div>

        <div class="chips" id="fieldType"></div>

        <div class="sub-text" id="fieldCaption"></div>

        <div class="score-bars">
          <div class="score-row">
            <div>回忆度</div>
            <div class="score-track">
              <div class="score-fill memory" id="barMemory"></div>
            </div>
            <div class="score-num" id="numMemory"></div>
          </div>
          <div class="score-row">
            <div>美观度</div>
            <div class="score-track">
              <div class="score-fill beauty" id="barBeauty"></div>
            </div>
            <div class="score-num" id="numBeauty"></div>
          </div>
        </div>

        <div class="sub-text" id="fieldReason"></div>

        <details class="fold">
          <summary>
            <span>更多信息</span>
            <span class="fold-hint">EXIF / 路径 / 调试</span>
          </summary>

          <div class="kv-grid">
            <div class="kv-k">日期</div><div class="kv-v" id="kpiDate"></div>
            <div class="kv-k">地点</div><div class="kv-v" id="kpiLocation"></div>
            <div class="kv-k">图片URL</div><div class="kv-v" id="fieldPath"></div>
            <div class="kv-k">原始路径</div><div class="kv-v" id="fieldOrigPath"></div>
            <div class="kv-k">分辨率</div><div class="kv-v" id="fieldRes"></div>
            <div class="kv-k">方向</div><div class="kv-v" id="fieldOrientation"></div>
            <div class="kv-k">已上屏</div><div class="kv-v" id="fieldUsedAt"></div>
            <div class="kv-k">EXIF摘要</div><div class="kv-v" id="fieldExifSummary"></div>
          </div>

          <details class="fold" style="margin-top:10px;">
            <summary>
              <span>EXIF JSON</span>
              <span class="fold-hint">调试</span>
            </summary>
            <div class="mono" id="fieldExifJson"></div>
          </details>
        </details>
      </div>
    </div>
  </div>

  <script>
    const PHOTOS = {data_json};
    const SELECTED_IMG = {selected_json};

    const byDate = new Map();
    for (const p of PHOTOS) {{
      if (!p.date) continue;
      if (!byDate.has(p.date)) byDate.set(p.date, []);
      byDate.get(p.date).push(p);
    }}
    for (const [d, arr] of byDate.entries()) {{
      arr.sort((a, b) => ((b.memory ?? -1) - (a.memory ?? -1)));
    }}

    const canvas = document.getElementById('previewCanvas');
    const ctx = canvas.getContext('2d');
    const statusLine = document.getElementById('statusLine');
    const screenSel = document.getElementById('screenSel');   // 分辨率切换下拉

    const kpiDate = document.getElementById('kpiDate');
    const kpiLocation = document.getElementById('kpiLocation');
    const kpiSide = document.getElementById('kpiSide');

    const fieldPath = document.getElementById('fieldPath');
    const fieldOrigPath = document.getElementById('fieldOrigPath');
    const fieldType = document.getElementById('fieldType');
    const fieldCaption = document.getElementById('fieldCaption');
    const fieldReason = document.getElementById('fieldReason');
    const fieldRes = document.getElementById('fieldRes');
    const fieldOrientation = document.getElementById('fieldOrientation');
    const fieldUsedAt = document.getElementById('fieldUsedAt');
    const fieldExifSummary = document.getElementById('fieldExifSummary');
    const fieldExifJson = document.getElementById('fieldExifJson');

    // 评分条
    const barMemory = document.getElementById('barMemory');
    const barBeauty = document.getElementById('barBeauty');
    const numMemory = document.getElementById('numMemory');
    const numBeauty = document.getElementById('numBeauty');

    let currentDate = null;
    let currentPhoto = null;

    function formatLocation(lat, lon, city) {{
      const c = (city || '').trim();
      if (c.length > 0) return c;
      if (lat == null || lon == null) return '';
      try {{
        return Number(lat).toFixed(5) + ', ' + Number(lon).toFixed(5);
      }} catch (e) {{
        return String(lat) + ', ' + String(lon);
      }}
    }}

    function formatDateDisplay(dateStr) {{
      if (!dateStr) return '';
      const parts = dateStr.split('-');
      if (parts.length < 3) return dateStr;
      const y = parts[0];
      const m = String(parseInt(parts[1], 10));
      const d = String(parseInt(parts[2], 10));
      return y + '.' + m + '.' + d;
    }}

    function escapeHtml(s) {{
      return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }}

    function hashToHue(str) {{
      // 简单稳定 hash -> 0..359
      let h = 0;
      const s = String(str || '');
      for (let i = 0; i < s.length; i++) {{
        h = (h * 31 + s.charCodeAt(i)) >>> 0;
      }}
      return h % 360;
    }}

    function renderTags(tags) {{
      if (!Array.isArray(tags) || tags.length === 0) return '';
      let htmlOut = '';
      for (const t of tags) {{
        if (!t) continue;
        const hue = hashToHue(t);
        const bg = 'hsla(' + hue + ', 90%, 55%, 0.14)';
        const bd = 'hsla(' + hue + ', 90%, 55%, 0.30)';
        const dot = 'hsl(' + hue + ', 90%, 62%)';
        htmlOut += '<span class="chip" style="background:' + bg + '; border-color:' + bd + ';">'
          + '<span class="chip-dot" style="background:' + dot + ';"></span>'
          + escapeHtml(t)
          + '</span>';
      }}
      return htmlOut;
    }}

    function safeText(v) {{
      if (v === null || v === undefined) return '';
      return String(v);
    }}

    function wrapText(ctx, text, x, y, maxWidth, lineHeight, maxLines) {{
      if (!text) return;
      const words = text.split(/\\s+/);
      let line = '';
      let lineCount = 0;
      for (let n = 0; n < words.length; n++) {{
        const testLine = line ? (line + ' ' + words[n]) : words[n];
        const metrics = ctx.measureText(testLine);
        if (metrics.width > maxWidth && n > 0) {{
          ctx.fillText(line, x, y);
          line = words[n];
          y += lineHeight;
          lineCount++;
          if (lineCount >= maxLines) break;
        }} else {{
          line = testLine;
        }}
      }}
      if (line && lineCount < maxLines) ctx.fillText(line, x, y);
    }}

    function applyFourColorDither() {{
      const w = canvas.width, h = canvas.height;
      let imgData;
      try {{
        imgData = ctx.getImageData(0, 0, w, h);
      }} catch (e) {{
        statusLine.textContent = '无法从画布读取像素（跨域或图片未走 /images）：' + e;
        return;
      }}
      const data = imgData.data;

      const palette = [
        {{ r: 0, g: 0, b: 0 }},
        {{ r: 255, g: 255, b: 255 }},
        {{ r: 200, g: 0, b: 0 }},
        {{ r: 220, g: 180, b: 0 }}
      ];

      const errR = new Float32Array(w);
      const errG = new Float32Array(w);
      const errB = new Float32Array(w);
      const nextErrR = new Float32Array(w);
      const nextErrG = new Float32Array(w);
      const nextErrB = new Float32Array(w);

      function nearestColor(r, g, b) {{
        let bestIndex = 0;
        let bestDist = Infinity;
        for (let i = 0; i < palette.length; i++) {{
          const pr = palette[i].r, pg = palette[i].g, pb = palette[i].b;
          const dr = r - pr, dg = g - pg, db = b - pb;
          const dist = dr*dr + dg*dg + db*db;
          if (dist < bestDist) {{ bestDist = dist; bestIndex = i; }}
        }}
        return palette[bestIndex];
      }}

      for (let y = 0; y < h; y++) {{
        for (let x = 0; x < w; x++) {{
          const idx = (y * w + x) * 4;

          let r = data[idx] + errR[x];
          let g = data[idx + 1] + errG[x];
          let b = data[idx + 2] + errB[x];

          r = r < 0 ? 0 : (r > 255 ? 255 : r);
          g = g < 0 ? 0 : (g > 255 ? 255 : g);
          b = b < 0 ? 0 : (b > 255 ? 255 : b);

          const nc = nearestColor(r, g, b);

          data[idx] = nc.r;
          data[idx + 1] = nc.g;
          data[idx + 2] = nc.b;

          const er = r - nc.r, eg = g - nc.g, eb = b - nc.b;

          if (x + 1 < w) {{
            errR[x + 1] += er * (7 / 16);
            errG[x + 1] += eg * (7 / 16);
            errB[x + 1] += eb * (7 / 16);
          }}
          if (y + 1 < h) {{
            if (x > 0) {{
              nextErrR[x - 1] += er * (3 / 16);
              nextErrG[x - 1] += eg * (3 / 16);
              nextErrB[x - 1] += eb * (3 / 16);
            }}
            nextErrR[x] += er * (5 / 16);
            nextErrG[x] += eg * (5 / 16);
            nextErrB[x] += eb * (5 / 16);
            if (x + 1 < w) {{
              nextErrR[x + 1] += er * (1 / 16);
              nextErrG[x + 1] += eg * (1 / 16);
              nextErrB[x + 1] += eb * (1 / 16);
            }}
          }}
        }}

        if (y + 1 < h) {{
          for (let i = 0; i < w; i++) {{
            errR[i] = nextErrR[i]; errG[i] = nextErrG[i]; errB[i] = nextErrB[i];
            nextErrR[i] = 0; nextErrG[i] = 0; nextErrB[i] = 0;
          }}
        }}
      }}

      ctx.putImageData(imgData, 0, 0);
    }}

    function updateMeta(photo) {{
      if (!photo) {{
        kpiDate.textContent = '';
        kpiLocation.textContent = '';
        kpiSide.textContent = '';

        fieldPath.textContent = '';
        fieldOrigPath.textContent = '';
        fieldType.innerHTML = '';
        fieldCaption.textContent = '';
        fieldReason.textContent = '';
        fieldRes.textContent = '';
        fieldOrientation.textContent = '';
        fieldUsedAt.textContent = '';
        fieldExifSummary.textContent = '';
        fieldExifJson.textContent = '';
        // 清空评分条
        barMemory.style.width = '0%';
        barBeauty.style.width = '0%';
        numMemory.textContent = '';
        numBeauty.textContent = '';
        return;
      }}

      // 填充 meta-box 新结构
      kpiSide.textContent = photo.side ? '「' + safeText(photo.side) + '」' : '';
      fieldType.innerHTML = renderTags(photo.type);
      fieldCaption.textContent = safeText(photo.caption);

      // 评分条
      const m = photo.memory != null ? Math.max(0, Math.min(100, photo.memory)) : 0;
      const b = photo.beauty != null ? Math.max(0, Math.min(100, photo.beauty)) : 0;
      barMemory.style.width = m + '%';
      barBeauty.style.width = b + '%';
      numMemory.textContent = m ? m.toFixed(1) : '';
      numBeauty.textContent = b ? b.toFixed(1) : '';

      fieldReason.textContent = photo.reason ? '评分理由：' + safeText(photo.reason) : '';

      // 更多信息区
      const loc = formatLocation(photo.lat, photo.lon, photo.city);
      kpiDate.textContent = safeText(photo.date);
      kpiLocation.textContent = safeText(loc);
      fieldPath.textContent = safeText(photo.path);
      fieldOrigPath.textContent = safeText(photo.orig_path || '');
      const res = (safeText(photo.width) || safeText(photo.height)) ? (safeText(photo.width) + ' x ' + safeText(photo.height)) : '';
      fieldRes.textContent = res;
      fieldOrientation.textContent = safeText(photo.orientation);
      fieldUsedAt.textContent = safeText(photo.used_at);
      fieldExifSummary.textContent = safeText(photo.exif_summary);
      fieldExifJson.textContent = safeText(photo.exif_json);
    }}

    function drawPreview(photo) {{
      if (!photo) {{
        statusLine.textContent = '未指定照片。请从 /review 点击某张照片进入模拟器。';
        return;
      }}

      statusLine.textContent = ''; // 正常情况不显示废话

      canvas.width = {CANVAS_W};
      canvas.height = {CANVAS_H};

      ctx.fillStyle = '#FFFFFF';
      ctx.fillRect(0, 0, canvas.width, canvas.height);

      const img = new Image();
      img.onload = function() {{
          // 按图片自然尺寸设 canvas（横照 800×480 / 竖照 480×800，跟随方向不再被拉伸竖框）
          canvas.width = img.naturalWidth || img.width;
          canvas.height = img.naturalHeight || img.height;
          ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
          // 画布尺寸标注跟着实际渲染结果走（换屏幕、横竖切换都会变）
          const sz = document.getElementById('canvasSize');
          if (sz) sz.textContent = canvas.width + ' × ' + canvas.height;
        }};
      img.onerror = function() {{
        statusLine.textContent = '图片加载失败：' + photo.path;
      }};
      const scr = screenSel ? screenSel.value : '';
      img.src = '/sim_render?img=' + encodeURIComponent(photo.path)
              + (scr ? '&screen=' + encodeURIComponent(scr) : '');
    }}

    function pickPhotoFromDate(date) {{
      const arr = byDate.get(date) || [];
      if (!arr.length) return null;

      const THRESHOLD = {float(getattr(cfg, "MEMORY_THRESHOLD", 70.0) or 70.0)};
      const candidates = arr.filter(p => p.memory != null && p.memory > THRESHOLD);
      if (candidates.length > 0) {{
        const idx = Math.floor(Math.random() * candidates.length);
        return {{ photo: candidates[idx], dateUsed: date }};
      }}

      // 兜底：当天随便挑
      const idx = Math.floor(Math.random() * arr.length);
      return {{ photo: arr[idx], dateUsed: date, fallbackNoThreshold: true }};
    }}

    function getPreviousDateStr(dateStr) {{
      if (!dateStr) return null;
      const parts = dateStr.split('-');
      if (parts.length < 3) return null;
      const y = parseInt(parts[0], 10);
      const m = parseInt(parts[1], 10);
      const d = parseInt(parts[2], 10);
      if (!y || !m || !d) return null;
      const dt = new Date(y, m - 1, d);
      dt.setDate(dt.getDate() - 1);
      const yy = dt.getFullYear();
      const mm = String(dt.getMonth() + 1).padStart(2, '0');
      const dd = String(dt.getDate()).padStart(2, '0');
      return yy + '-' + mm + '-' + dd;
    }}

    function pickPhotoWithLookback(baseDate) {{
      if (!baseDate) return null;
      let date = baseDate;
      const MAX_LOOKBACK = 30;

      for (let i = 0; i < MAX_LOOKBACK; i++) {{
        const picked = pickPhotoFromDate(date);
        if (picked && picked.photo) return picked;
        const prev = getPreviousDateStr(date);
        if (!prev) break;
        date = prev;
      }}

      // 最终兜底：目标日期没找到 map，啥也不干
      return null;
    }}

    function findSelectedPhoto() {{
      if (!SELECTED_IMG) return null;
      for (const p of PHOTOS) {{
        if (p.path === SELECTED_IMG) return p;
      }}
      return null;
    }}

    function onRerollSameDay() {{
      if (!currentDate) {{
        statusLine.textContent = '请从 /review 点击某张照片进入模拟器。';
        return;
      }}

      const pick = pickPhotoWithLookback(currentDate);
      if (!pick || !pick.photo) {{
        statusLine.textContent = '该日期及向前 30 天内没有可用照片。';
        return;
      }}

      // 如果刚好又抽到自己，尝试再抽几次
      let tries = 0;
      let chosen = pick;
      while (tries < 6 && chosen && chosen.photo && currentPhoto && chosen.photo.path === currentPhoto.path) {{
        const again = pickPhotoWithLookback(currentDate);
        if (!again || !again.photo) break;
        chosen = again;
        tries++;
      }}

      currentPhoto = chosen.photo;
      updateMeta(currentPhoto);
      drawPreview(currentPhoto);
    }}

    document.getElementById('rerollBtn').addEventListener('click', onRerollSameDay);

    // 换屏幕：重画当前这张（方向由照片自身决定，所以只有画布尺寸变）
    if (screenSel) {{
      screenSel.addEventListener('change', () => {{
        if (currentPhoto) drawPreview(currentPhoto);
      }});
    }}

    // 默认进入：如果从 review 点进来，则显示该照片；否则提示用户从 review 进入
    const initPhoto = findSelectedPhoto();
    if (!initPhoto) {{
      updateMeta(null);
      drawPreview(null);
    }} else {{
      currentDate = initPhoto.date;
      currentPhoto = initPhoto;
      updateMeta(currentPhoto);
      drawPreview(currentPhoto);
    }}
  </script>
</body>
</html>
"""
    return html_str


# --------------------------
# Routes
# --------------------------

@app.get("/settings")
def settings_page():
    _require_webui_enabled()
    return Response(build_settings_html(), mimetype="text/html; charset=utf-8")


@app.get("/api/settings")
def api_get_settings():
    _require_webui_enabled()
    return Response(json.dumps(web_settings.load(), ensure_ascii=False), mimetype="application/json")


def _fill_missing_screen_fields(payload: dict) -> dict:
    """补齐 screens 里缺失的 palette/bin_format（前端不提交这两项）。

    从当前配置的对应屏（按 name 匹配）补齐；找不到则给默认六色 + 1byte。
    """
    screens = payload.get("screens")
    if not isinstance(screens, list):
        return payload

    known = {}
    for sc in getattr(cfg, "SCREENS", []) or []:
        if isinstance(sc, dict) and sc.get("name"):
            known[str(sc["name"])] = sc
    for sc in screens:
        if not isinstance(sc, dict):
            continue
        if not sc.get("palette"):
            ref = known.get(str(sc.get("name", "")))
            if ref and ref.get("palette"):
                sc["palette"] = [list(c) for c in ref["palette"]]
            else:
                sc["palette"] = [[0, 0, 0], [255, 255, 255], [200, 0, 0], [220, 180, 0]]
        if not sc.get("bin_format"):
            ref = known.get(str(sc.get("name", "")))
            sc["bin_format"] = ref.get("bin_format", "1byte_per_px") if ref else "1byte_per_px"
    payload["screens"] = screens
    return payload


@app.post("/api/settings")
def api_save_settings():
    _require_webui_enabled()
    try:
        payload = request.get_json(force=True)
    except Exception:
        return Response(json.dumps({"ok": False, "error": "无效的 JSON 请求体"}), mimetype="application/json")
    if not isinstance(payload, dict):
        return Response(json.dumps({"ok": False, "error": "payload 必须是对象"}), mimetype="application/json")

    # 保 palette / bin_format：前端提交的 screens 只有 name/width/height/text_area_height/enabled，
    # 若缺 palette/bin_format 则从当前 config 的对应屏补齐，避免切屏后退化四色/错误打包。
    payload = _fill_missing_screen_fields(payload)

    web_settings.save(payload)
    try:
        web_settings.apply_to_config(cfg)
    except Exception as e:
        print(f"[settings] apply_to_config 失败：{e}")
    try:
        _refresh_quota_globals()  # 同步 QUOTA_* 模块级变量，否则调度器读旧值
    except Exception as e:
        print(f"[settings] 刷新额度参数失败：{e}")
    try:
        scheduler.reload_from_settings()
    except Exception as e:
        print(f"[settings] scheduler reload 失败：{e}")
    return Response(json.dumps({"ok": True}), mimetype="application/json")


@app.get("/api/status")
def api_status():
    _require_webui_enabled()
    try:
        st = scheduler.status()
        return Response(json.dumps(st, ensure_ascii=False), mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), mimetype="application/json")


@app.post("/api/scan/start")
def api_scan_start():
    _require_webui_enabled()
    req = request.get_json(silent=True) or {}
    rescan = bool(req.get("rescan"))
    ok, msg = scheduler.start_analyze_manual(rescan=rescan)
    return Response(json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False), mimetype="application/json")


@app.post("/api/scan/stop")
def api_scan_stop():
    _require_webui_enabled()
    ok, msg = scheduler.stop_analyze_manual()
    return Response(json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False), mimetype="application/json")


@app.post("/api/next")
def api_next():
    """网页点「换一张」：立刻重选一批照片出图，覆盖 latest.bin。

    渲染进程走 --next：优先排除最近已出过图的照片。不烧 VLM 额度（只读库+出图）。
    """
    _require_webui_enabled()
    ok, msg = scheduler.start_render_manual(extra_args=["--next"])
    return Response(json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False), mimetype="application/json")


@app.post("/api/render/stop")
def api_render_stop():
    _require_webui_enabled()
    ok, msg = scheduler.stop_render_manual()
    return Response(json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False), mimetype="application/json")


@app.post("/api/dedupe/start")
def api_dedupe_start():
    """网页点「重建去重索引」：跑一次 analyze_photos.py --dedupe-only。

    不烧 VLM 额度（只算哈希、不调模型），所以不走 token 门控。
    """
    _require_webui_enabled()
    ok, msg = scheduler.start_dedupe_manual()
    return Response(json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False), mimetype="application/json")


@app.get("/api/device")
def api_device():
    """相框设备的最后上线 / 拉取记录。

    数据完全来自 esp 下载路由的被动记录 —— 固件一个字不用改。当前还没有实体
    设备，所以这里会是空的；等固件接入，设置页立刻就有在线状态可看。
    """
    _require_webui_enabled()
    with _DEVICE_LOCK:
        st = dict(_load_device_state())
        st["recent"] = list(st.get("recent") or [])[:20]
    return Response(json.dumps(st, ensure_ascii=False), mimetype="application/json")


@app.get("/api/scan/progress")
def api_scan_progress():
    """返回扫描实时进度（analyze 写到 PROGRESS_FILE 的 JSON）。未在扫描则返回 running=false。"""
    _require_webui_enabled()
    running = scheduler.is_analyze_running()
    data: dict = {"running": bool(running)}
    snap = _read_progress_file(PROGRESS_FILE)
    if snap:
        data["progress"] = snap
    return Response(json.dumps(data, ensure_ascii=False), mimetype="application/json")


@app.get("/api/dedupe/progress")
def api_dedupe_progress():
    """去重进度 + 上一次的结果。

    running 以【进程状态】为准，文件里的 status 只说明上一次跑到哪：
    进程没了、文件却还停在 status="running"，那就是中途被中断或崩了 ——
    如实标成 interrupted，而不是傻等一个永远不会更新的百分比。
    跑完的那份快照会一直留着（analyze 结束时不删），页面据此显示上次的统计。
    """
    _require_webui_enabled()
    running = scheduler.is_dedupe_running()
    data: dict = {"running": bool(running)}
    snap = _read_progress_file(DEDUPE_PROGRESS_FILE)
    if snap:
        if not running and snap.get("status") == "running":
            snap["interrupted"] = True
        data["progress"] = snap
    return Response(json.dumps(data, ensure_ascii=False), mimetype="application/json")


@app.get("/api/quotas")
def api_quotas():
    _require_webui_enabled()
    result = {}

    # MiniMax Token Plan 剩余额度（5h 窗口，真实值）
    mm = get_minimax_remains()
    if mm.get("ok") and mm.get("parsed"):
        pp = mm["parsed"]
        result["minimax"] = {
            "ok": True,
            "data": {
                "remaining_5h": pp.get("remaining_5h"),
                "total_5h": pp.get("total_5h"),
                "percent": round(pp.get("percent"), 1) if pp.get("percent") is not None else None,
                "weekly_percent": round(pp.get("weekly_percent"), 1) if pp.get("weekly_percent") is not None else None,
                "remains_time_sec": pp.get("remains_time"),
                "weekly_remaining": pp.get("weekly_remaining"),
                "weekly_total": pp.get("weekly_total"),
            },
        }
    else:
        result["minimax"] = {"ok": False, "error": mm.get("error", "查询失败")}

    # 其它渠道：有官方余额接口的真查，否则提示控制台
    for ch in getattr(cfg, "API_CHANNELS", []):
        provider = ch.get("provider") or web_settings.guess_provider(ch.get("model_name", ""))
        if provider == "minimax":
            continue
        meta = web_settings.PROVIDER_META.get(provider, {})
        api_url = meta.get("quota_api")
        key = ch.get("api_key", "")
        if api_url and key:
            try:
                r = requests.get(api_url, headers={"Authorization": f"Bearer {key}"}, timeout=15)
                result[provider] = {"ok": r.ok, "data": r.json() if r.ok else {"error": f"HTTP {r.status_code}"}}
            except Exception as e:
                result[provider] = {"ok": False, "error": str(e)}
        else:
            result[provider] = {"ok": None, "note": meta.get("quota_note", "无官方余额接口")}
    return Response(json.dumps(result, ensure_ascii=False), mimetype="application/json")


@app.get("/")
def index():
    if ENABLE_REVIEW_WEBUI:
        return redirect("/review")
    return Response("InkTime server running. WebUI disabled.", mimetype="text/plain; charset=utf-8")


def build_settings_html() -> str:
    """设置页 HTML（暗色主题，与 review/sim 页一致）。普通字符串，动态数据由 JS fetch 填充。"""
    return """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>InkTime 设置</title>
<style>
:root{--bg:#0b0c10;--panel:#16181d;--card:#1d2027;--text:#e6e8ee;--muted:#8a93a3;--line:#2a2e37;--accent:#8ab4ff;--accent2:#9cffd6;--radius:14px;}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;margin:0;padding:24px;}
.container{max-width:920px;margin:0 auto;}
h1{font-size:22px;margin:0 0 6px;font-weight:600}
.subtitle{color:var(--muted);font-size:13px;margin-bottom:18px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:20px;padding:12px;background:var(--panel);border-radius:var(--radius);}
.controls button,input[type=text],input[type=password],select{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px 12px;font-size:14px;}
.controls button{cursor:pointer}
.controls button:hover{background:var(--line)}
.controls button.primary{background:var(--accent);color:#0b0c10;font-weight:600;border:none}
details.fold{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:14px;margin-bottom:14px;}
details.fold summary{cursor:pointer;font-weight:600;font-size:15px;outline:none;margin-bottom:10px}
.kv{display:grid;grid-template-columns:180px 1fr auto;gap:10px;align-items:center;margin-bottom:10px;font-size:14px}
.kv label{color:var(--muted)}
.kv input[type=text],.kv input[type=password]{width:100%}
/* cron 常用档位按钮组 */
.cron-chips{display:flex;flex-wrap:wrap;gap:6px;margin:-4px 0 12px 190px}
.cron-chips .chip{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:16px;padding:5px 12px;font-size:12px;cursor:pointer}
.cron-chips .chip:hover{background:var(--line)}
.cron-chips .chip.active{background:var(--accent);color:#0b0c10;font-weight:600;border-color:var(--accent)}
.hint{color:var(--muted);font-size:12px;margin-bottom:14px}
#quotaBox,.statusline{font-family:ui-monospace,Consolas,monospace;font-size:13px;line-height:1.6;background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:12px;}
.ok{color:var(--accent2)} .warn{color:#ffd479} .err{color:#ff7b7b}
/* 额度进度条 */
.quota-card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:10px;font-family:system-ui,sans-serif;}
.quota-title{font-weight:600;font-size:14px;margin-bottom:10px}
.quota-row{display:flex;align-items:center;gap:10px;margin:8px 0}
.q-label{width:120px;font-size:12px;color:var(--muted);flex-shrink:0}
.q-pct{width:60px;text-align:right;font-weight:600;font-size:13px;flex-shrink:0}
.q-note{font-size:12px;color:var(--muted);margin-top:6px}
.bar{flex:1;height:12px;background:var(--line);border-radius:6px;overflow:hidden}
.bar .fill{height:100%;border-radius:6px;transition:width .3s}
.bar-fill{height:100%;border-radius:6px;background:linear-gradient(90deg,#3fb58a,#54d6a4);transition:width .3s}
.bar .fill.success{background:linear-gradient(90deg,#3fb58a,#54d6a4)}
.bar .fill.warn{background:linear-gradient(90deg,#e0a83c,#f0c060)}
.bar .fill.err{background:linear-gradient(90deg,#d65757,#f07b7b)}
</style>
</head>
<body>
<div class="container">
<h1>InkTime 设置</h1>
<div class="subtitle">修改后点「保存设置」，模型密钥 / 自动任务立即生效（不用重启）。<a href="/review">← 返回照片库</a></div>

<div class="controls">
  <button type="button" class="primary" onclick="saveSettings()">保存设置</button>
  <button type="button" onclick="refreshQuota()">查看额度余额</button>
  <button type="button" onclick="refreshStatus()">查看自动任务</button>
  <span class="subtitle" id="saveMsg" style="margin:0"></span>
</div>

<details class="fold" open>
<summary>智能模型密钥（MiniMax / 智谱 / DeepSeek）</summary>
<div class="hint">给照片打分、写文案就靠这些大模型。填了哪家的密钥，就会用哪家；某家失败或限流会自动切到下一家，不会卡住。密钥只在本地保存，不会上传。</div>
<div class="kv"><label>MiniMax（主力）</label><input type="password" id="minimax_api_key" placeholder="sk-..."><label style="display:flex;gap:6px"><input type="checkbox" id="minimax_enabled">启用</label></div>
<div class="kv"><label>智谱 GLM（备选）</label><input type="password" id="zhipu_api_key" placeholder="..."><label style="display:flex;gap:6px"><input type="checkbox" id="zhipu_enabled">启用</label></div>
<div class="kv"><label>DeepSeek（备选）</label><input type="password" id="deepseek_api_key" placeholder="sk-..."><label style="display:flex;gap:6px"><input type="checkbox" id="deepseek_enabled">启用</label></div>
</details>

<details class="fold" open>
<summary>自动任务（新增照片 / 每日出图 / 去重索引）</summary>
<div class="hint">下面两件事会自动跑：<b>①扫描新照片</b>＝把相册里没打过分的新照片送去打分、写文案；<b>②每日出图</b>＝每天挑一张「历史上的今天」渲染成墨水屏能显示的图片。填「分 时 日 月 周」五个数字，看不懂没关系，选右边的常用档位会自动帮你填。</div>
<div class="kv"><label>扫描新照片</label><input type="text" id="analyze_cron" placeholder="30 3 * * *" style="grid-column:1"><label style="display:flex;gap:6px"><input type="checkbox" id="analyze_enabled">开启</label></div>
<div class="cron-chips" id="analyze_chips"></div>
<div class="kv"><label>每日出图</label><input type="text" id="render_cron" placeholder="5 4 * * *" style="grid-column:1"><label style="display:flex;gap:6px"><input type="checkbox" id="render_enabled">开启</label></div>
<div class="cron-chips" id="render_chips"></div>
<div class="kv"><label>去重索引</label><input type="text" id="dedupe_cron" placeholder="0 2 * * *" style="grid-column:1"><label style="display:flex;gap:6px"><input type="checkbox" id="dedupe_enabled">开启</label></div>
<div class="cron-chips" id="dedupe_chips"></div>
<div class="hint">⭐ 每个任务下方会实时显示成一句人话：<span id="analyze_hint" class="ok"></span></div>
<div class="hint">⭐ 每日出图同样：<span id="render_hint" class="ok"></span></div>
<div class="controls" style="margin:10px 0;padding:10px;background:var(--card);border:1px solid var(--line);border-radius:10px;">
  <button type="button" id="scanStartBtn" class="primary" onclick="startScan()">▶ 按额度烧到底线</button>
  <button type="button" id="scanStopBtn" onclick="stopScan()">■ 停止扫描</button>
  <button type="button" id="rescanBtn" onclick="rescanAll()">🔄 重新扫描已有照片</button>
  <span class="subtitle" id="scanMsg" style="margin:0"></span>
</div>
<div id="scanProgressBox" style="display:none;margin:8px 0;padding:10px;background:var(--card);border:1px solid var(--line);border-radius:10px;">
  <div class="quota-row"><span class="q-label">扫描进度</span><div class="bar"><div class="bar-fill" id="scanProgressFill" style="width:0%"></div></div><span class="q-pct" id="scanProgressPct">0%</span></div>
  <div class="q-note" id="scanProgressNote">正在扫描…</div>
</div>
<div class="hint" style="margin-bottom:4px">「按额度烧到底线」= 立刻把没打过分的新照片送去打分，按剩余额度自动算出该扫多少张，把额度用到底线（不受空闲时段限制），进度条实时显示。</div>
<div class="statusline" id="statusBox">尚未查看调度状态</div>
</details>

<details class="fold">
<summary>相框设备（最后上线 / 换一张）</summary>
<div class="hint">数据来自相框每次拉取图片时的被动记录，固件不需要任何改动。还没有设备接入时这里会是空的。</div>
<div class="kv"><label>最后上线</label><span id="dev_last_seen" style="grid-column:2/-1">—</span></div>
<div class="kv"><label>最后拉取</label><span id="dev_last_file" style="grid-column:2/-1">—</span></div>
<div class="kv"><label>设备地址</label><span id="dev_last_ip" style="grid-column:2/-1">—</span></div>
<div class="kv"><label>累计拉取</label><span id="dev_pull_count" style="grid-column:2/-1">—</span></div>
<div class="kv"><label>操作</label><span style="grid-column:2/-1;display:flex;gap:8px;flex-wrap:wrap">
<button type="button" onclick="doNext()">🔀 换一张</button>
<button type="button" onclick="refreshDevice()">刷新状态</button>
</span></div>
<div class="q-note" id="dev_note">「换一张」会立刻重选一批照片出图并覆盖 latest.bin（优先排除最近已出过图的照片），相框下次轮询就能看到新图。</div>
</details>

<details class="fold">
<summary>屏幕参数（有几个墨水屏就勾几个）</summary>
<div class="hint">勾选的屏幕会各自生成对应的图片（尺寸/分辨率不同）。第一个勾选的是主屏。调色板（颜色）与图片打包格式在配置文件里定义，这里只读。</div>
<div id="screensBox"></div>
<button type="button" onclick="addScreen()">+ 新增一块屏幕</button>
</details>

<details class="fold">
<summary>评分与选片</summary>
<div class="kv"><label>回忆度达标分</label><input type="text" id="memory_threshold"></div>
<div class="hint">▲ 回忆度超过这个分的照片，才够格被选为"每日一图"。</div>
<div class="kv"><label>每天出几张图</label><input type="text" id="daily_qty"></div>
<div class="kv"><label>没意义照片阈值</label><input type="text" id="unmeaningful_threshold"></div>
<div class="hint">▲ 低于这个分的（比如截图、收据、杂物）会被标记为"没意义"，不配文案、不生成成品，省流量。</div>
<div class="kv"><label>上传分辨率（长边px）</label><input type="range" id="vlm_max_long_edge" min="512" max="2048" step="128" value="1024"><span class="q-note" id="vlm_res_note">1024px</span></div>
<div class="hint">▲ 发给大模型看图的清晰度。越大评分越准但越费流量；512 省流量，1024 更清晰（默认）。</div>
<div class="kv"><label>主页显示最低分</label><input type="text" id="home_min_score" placeholder="60"></div>
<div class="hint">▲ 低于这个回忆度的照片默认不在主页显示。勾"显示低分"才能看到它们。</div>
<div class="kv"><label>主页隐藏无意义</label><label style="display:flex;gap:6px"><input type="checkbox" id="home_hide_unmeaningful">隐藏</label></div>
<div class="hint">▲ 勾选后，被 AI 判定为"无意义"（截图/收据/杂物）的照片也不在主页显示。</div>
<div class="kv"><label>扫描跳过重复照片</label><label style="display:flex;gap:6px"><input type="checkbox" id="cfg_dedupe_enabled">跳过</label></div>
<div class="hint">▲ 开启后，被去重索引判定为重复的照片不再送去打分（省额度）。索引由上面「自动任务 → 去重索引」定时重建，也可以在下面手动重建。</div>
<div class="kv"><label>视觉相似判定</label><label style="display:flex;gap:6px"><input type="checkbox" id="cfg_dedupe_similar">启用</label></div>
<div class="hint">▲ 用感知哈希确认两张图是否真的像。关掉会退化成"拍摄时间接近就算重复"——那会误杀连拍里构图不同的照片。</div>
<div class="kv"><label>相似度阈值</label><input type="text" id="cfg_dedupe_hamming" placeholder="6"></div>
<div class="hint">▲ 汉明距离 0~64，越小越严格。默认 6 比较稳；超过 8 开始容易把不同的照片判成重复。</div>
<div class="kv"><label>连拍时间间隔（秒）</label><input type="text" id="cfg_dedupe_burst_gap" placeholder="3"></div>
<div class="kv"><label>选片排除天数</label><input type="text" id="cfg_recent_days" placeholder="30"></div>
<div class="hint">▲ 每天出图时跳过最近这么多天内已经上过屏的照片，避免连着几天推同一批。</div>
<div class="kv"><label>去重索引</label><span style="grid-column:2/-1"><button type="button" onclick="rebuildDedupe()">🔍 立即重建去重索引</button></span></div>
<div class="q-note" id="dedupe_note" style="display:none"></div>
<div class="q-note" style="opacity:.7">首次全量重建要读遍相册里的每个文件，大相册可能要几十分钟；之后是增量，很快。</div>
<div id="dedupeProgressBox" style="display:none;margin:10px 0;padding:10px;background:var(--card);border:1px solid var(--line);border-radius:10px;">
  <div class="quota-row"><span class="q-label" id="dedupeStage">准备中…</span><div class="bar"><div class="bar-fill" id="dedupeFill" style="width:0%"></div></div><span class="q-pct" id="dedupePct">0%</span></div>
  <div class="q-note" id="dedupeDetail"></div>
</div>
</details>

<details class="fold">
<summary>额度自动利用（把快用完的额度用在半夜）</summary>
<div class="hint">MiniMax 的套餐额度是「5 小时一清、用不完就浪费」。开启后，会在你基本不用手机的深夜时段自动多扫一些照片，把快要清零的额度用掉，不浪费钱。白天不会打扰你。</div>
<div class="kv"><label>自动利用</label><label style="display:flex;gap:6px"><input type="checkbox" id="quota_enabled">开启</label></div>
<div class="kv"><label>深夜时段：从</label><input type="text" id="quota_idle_start" placeholder="23:00"></div>
<div class="kv"><label>到</label><input type="text" id="quota_idle_end" placeholder="07:00"></div>
<div class="hint">▲ 深夜空闲时段起止（默认 23:00 → 07:00）。这段时间会尽量多扫照片把额度用掉。</div>
<div class="kv"><label>窗口剩多少分钟开始烧</label><input type="text" id="quota_edge_min" placeholder="35"><span class="q-note">分钟</span></div>
<div class="hint">▲ 非深夜时，距 5h 窗口清零还剩这么多分钟就自动开始烧额度（用完的额度会被系统清零，不烧就浪费）。</div>
<div class="kv"><label>烧到剩多少%就停</label><input type="text" id="quota_floor_percent" placeholder="10"><span class="q-note">%</span></div>
<div class="hint">▲ 应急底线：额度烧到剩这么多 % 就停下，留着备用（建议 10）。</div>
<div class="kv"><label>每次最多扫几张</label><input type="text" id="quota_batch_limit" placeholder="20"><span class="q-note">张</span></div>
<div class="hint">▲ 单次自动扫描的上限，防止一次花太多。额度多时会自动多扫几轮。</div>
<div class="kv"><label>每张约烧多少%额度</label><input type="text" id="quota_photo_burn" placeholder="0.5"><span class="q-note">%</span></div>
<div class="hint">▲ 估算用：每张照片大约消耗窗口额度的多少%。用来算"这次该扫几张"。首次跑后看余额降幅可微调。</div>
<div class="kv"><label>两次之间隔多久</label><input type="text" id="quota_cooldown" placeholder="1800"><span class="q-note">秒</span></div>
</details>

<div class="controls">
  <span class="subtitle" style="margin:0">💰 模型额度余额</span>
</div>
<div id="quotaBox">加载额度中…</div>

<script>
async function getJSON(url, opts){
  const r = await fetch(url, opts);
  if(!r.ok) throw new Error('HTTP '+r.status);
  return r.json();
}
function setVal(id, v){
  const el = document.getElementById(id);
  if(el) el.value = (v===null||v===undefined)?'':v;
}
function setChk(id, v){ const el=document.getElementById(id); if(el) el.checked=!!v; }

// —— cron 常用档位（按钮组，点选即填；含每天/每周/每月） ——
const CRON_PRESETS = [
  {cron:'30 3 * * *', label:'每天凌晨 03:30'},
  {cron:'0 3 * * *',  label:'每天凌晨 03:00'},
  {cron:'0 6 * * *',  label:'每天清晨 06:00'},
  {cron:'0 9 * * *',  label:'每天早上 09:00'},
  {cron:'0 12 * * *', label:'每天中午 12:00'},
  {cron:'0 4 * * 1',  label:'每周一凌晨 04:00'},
  {cron:'0 4 * * 0',  label:'每周日凌晨 04:00'},
  {cron:'0 4 1 * *',  label:'每月 1 号凌晨 04:00'},
  {cron:'0 4 1,15 * *', label:'每月 1、15 号凌晨 04:00'},
];
function cronToChinese(cronStr){
  const c = (cronStr||'').trim();
  if(!c) return '（未设置）';
  // 先查档位表（精确命中直接返回人话）
  for(const p of CRON_PRESETS){ if(p.cron === c) return p.label; }
  const p = c.split(' ');
  if(p.length < 5) return '『'+c+'』（请检查填写）';
  const [min, hour, dom, mon, dow] = p;
  let h = '';
  if(dow && dow !== '*') {
    const wk = {0:'周日',1:'周一',2:'周二',3:'周三',4:'周四',5:'周五',6:'周六'};
    const w = wk[+dow.replace('*/','')] || dow;
    h = '每' + w;
  } else if(dom && dom !== '*') {
    // 每月特定日（含逗号多日期）；hour/min 拼进去
    const hm = (hour==='*') ? ('第 '+min+' 分') : (hour+' 点 '+min+' 分');
    const days = dom.split(',').map(d=>d.trim()).join('、');
    h = '每个月 ' + days + ' 号 ' + hm;
  } else if(hour === '*/6' && min === '0') {
    h = '每天每隔 6 小时';
  } else if(hour === '*/2' && min === '0') {
    h = '每天每隔 2 小时';
  } else if(hour === '*/4' && min === '0') {
    h = '每天每隔 4 小时';
  } else {
    if(hour==='*') h = '每个小时的第 ' + min + ' 分';
    else h = '每天 ' + hour + ' 点 ' + min + ' 分';
  }
  return h;
}
function refreshCronHint(prefix){
  const cronInput = document.getElementById(prefix+'_cron');
  const hintEl = document.getElementById(prefix+'_hint');
  if(cronInput && hintEl) hintEl.textContent = cronToChinese(cronInput.value);
}
// 把常用档位渲染成可点选的按钮组（点一下自动填 cron，更直观）
function renderCronChips(prefix){
  const box = document.getElementById(prefix+'_chips');
  if(!box) return;
  box.innerHTML = '';
  for(const p of CRON_PRESETS){
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'chip';
    b.dataset.cron = p.cron;
    b.textContent = p.label;
    b.onclick = () => {
      const cronInput = document.getElementById(prefix+'_cron');
      if(cronInput){ cronInput.value = p.cron; }
      // 高亮当前选中
      box.querySelectorAll('button').forEach(x=>x.classList.remove('active'));
      b.classList.add('active');
      refreshCronHint(prefix);
    };
    box.appendChild(b);
  }
  // 根据现有 cron 值高亮匹配档位
  const cronInput = document.getElementById(prefix+'_cron');
  if(cronInput && cronInput.value){
    box.querySelectorAll('button').forEach(b=>{
      b.classList.toggle('active', b.dataset.cron === cronInput.value);
    });
  }
}

async function initSettings(){
  try{
    const s = await getJSON('/api/settings');
    const m = s.model || {};
    setVal('minimax_api_key', m.minimax_api_key); setChk('minimax_enabled', m.minimax_enabled);
    setVal('zhipu_api_key', m.zhipu_api_key);     setChk('zhipu_enabled', m.zhipu_enabled);
    setVal('deepseek_api_key', m.deepseek_api_key); setChk('deepseek_enabled', m.deepseek_enabled);
    const sc = s.schedule || {};
    setVal('analyze_cron', sc.analyze_cron); setChk('analyze_enabled', sc.analyze_enabled);
    setVal('render_cron', sc.render_cron);   setChk('render_enabled', sc.render_enabled);
    setVal('dedupe_cron', sc.dedupe_cron);   setChk('dedupe_enabled', sc.dedupe_enabled);
    renderCronChips('analyze'); renderCronChips('render'); renderCronChips('dedupe');
    refreshCronHint('analyze'); refreshCronHint('render'); refreshCronHint('dedupe');
    ['analyze_cron','render_cron','dedupe_cron'].forEach(id => {
      const el = document.getElementById(id);
      if(el) el.addEventListener('input', () => refreshCronHint(id.replace('_cron','')));
    });
    // 屏幕列表（多屏并存，含启用开关）
    window.__screens = s.screens || [];
    renderScreens();
    // 配额/Token 调度
    const q = s.quota || {};
    setVal('quota_idle_start', q.idle_start); setVal('quota_idle_end', q.idle_end);
    setVal('quota_edge_min', q.edge_min); setVal('quota_batch_limit', q.batch_limit);
    setVal('quota_cooldown', q.cooldown_sec);
    setVal('quota_floor_percent', (q.floor_percent!==undefined?q.floor_percent:q.idle_percent) || 10);
    setVal('quota_photo_burn', q.photo_burn_percent || 0.5);
    setChk('quota_enabled', q.enabled);
    const c = s.config || {};
    setVal('memory_threshold', c.MEMORY_THRESHOLD); setVal('daily_qty', c.DAILY_PHOTO_QUANTITY);
    setVal('unmeaningful_threshold', c.UNMEANINGFUL_THRESHOLD);
    setVal('vlm_max_long_edge', c.VLM_MAX_LONG_EDGE || 1024);
    setVal('home_min_score', c.HOME_MIN_SCORE || 60); setChk('home_hide_unmeaningful', c.HOME_HIDE_UNMEANINGFUL);
    setChk('cfg_dedupe_enabled', c.DEDUPE_ENABLED !== false);
    setChk('cfg_dedupe_similar', c.DEDUPE_SIMILAR_ENABLED !== false);
    setVal('cfg_dedupe_hamming', c.DEDUPE_HAMMING_MAX ?? 6);
    setVal('cfg_dedupe_burst_gap', c.DEDUPE_BURST_GAP_SEC ?? 3);
    setVal('cfg_recent_days', c.RECENT_EXCLUDE_DAYS ?? 30);
    if(document.getElementById('vlm_res_note')) document.getElementById('vlm_res_note').textContent = (c.VLM_MAX_LONG_EDGE||1024)+'px';
    // 上传分辨率滑块拖动时同步显示值
    const vlmEl = document.getElementById('vlm_max_long_edge');
    if(vlmEl){
      vlmEl.addEventListener('input', () => {
        if(document.getElementById('vlm_res_note')) document.getElementById('vlm_res_note').textContent = vlmEl.value + 'px';
      });
    }
    // 进页面自动加载：额度余额 + 自动任务状态 + 相框在线状态
    refreshQuota();
    refreshStatus();
    refreshDevice();
  }catch(e){ document.getElementById('saveMsg').textContent='加载设置失败: '+e; }
}

function renderScreens(){
  const box = document.getElementById('screensBox');
  if(!box) return;
  const list = window.__screens || [];
  box.innerHTML = '';
  list.forEach((sc, i) => {
    const div = document.createElement('div');
    div.className = 'kv';
    const en = sc.enabled ? 'checked' : '';
    div.innerHTML =
      `<label style="display:flex;gap:6px;grid-column:auto"><input type="checkbox" data-idx="${i}" data-field="enabled" ${en}>启用</label>`+
      `<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">`+
        `<input type="text" data-idx="${i}" data-field="name" value="${sc.name||''}" title="屏名" style="width:110px">`+
        `<input type="text" data-idx="${i}" data-field="width" value="${sc.width||''}" title="宽" style="width:70px">x`+
        `<input type="text" data-idx="${i}" data-field="height" value="${sc.height||''}" title="高" style="width:70px">`+
        `<input type="text" data-idx="${i}" data-field="text_area_height" value="${sc.text_area_height||''}" title="文字区高" style="width:70px">`+
        `<button type="button" onclick="removeScreen(${i})">删</button>`+
      `</div>`;
    box.appendChild(div);
  });
  // 绑定 input 同步回 window.__screens
  box.querySelectorAll('input').forEach(inp => {
    inp.addEventListener('input', () => {
      const i = +inp.dataset.idx, f = inp.dataset.field, v = inp.value;
      if(f === 'enabled') window.__screens[i][f] = inp.checked;
      else window.__screens[i][f] = f === 'width'||f==='height'||f==='text_area_height' ? (parseInt(v)||0) : v;
    });
  });
}
function addScreen(){
  window.__screens = window.__screens || [];
  window.__screens.push({name:'new_screen', width:480, height:800, text_area_height:100, enabled:false,
    palette:[[0,0,0],[255,255,255],[200,0,0],[220,180,0]]});
  renderScreens();
}
function removeScreen(i){
  window.__screens.splice(i,1);
  renderScreens();
}

async function saveSettings(){
  const payload = {
    model: {
      minimax_api_key: document.getElementById('minimax_api_key').value,
      minimax_enabled: document.getElementById('minimax_enabled').checked,
      zhipu_api_key: document.getElementById('zhipu_api_key').value,
      zhipu_enabled: document.getElementById('zhipu_enabled').checked,
      deepseek_api_key: document.getElementById('deepseek_api_key').value,
      deepseek_enabled: document.getElementById('deepseek_enabled').checked,
    },
    schedule: {
      analyze_cron: document.getElementById('analyze_cron').value,
      analyze_enabled: document.getElementById('analyze_enabled').checked,
      render_cron: document.getElementById('render_cron').value,
      render_enabled: document.getElementById('render_enabled').checked,
      dedupe_cron: document.getElementById('dedupe_cron').value,
      dedupe_enabled: document.getElementById('dedupe_enabled').checked,
    },
    screens: window.__screens || [],
    quota: {
      idle_start: document.getElementById('quota_idle_start').value,
      idle_end: document.getElementById('quota_idle_end').value,
      edge_min: parseInt(document.getElementById('quota_edge_min').value||'35'),
      batch_limit: parseInt(document.getElementById('quota_batch_limit').value||'20'),
      cooldown_sec: parseInt(document.getElementById('quota_cooldown').value||'1800'),
      floor_percent: parseFloat(document.getElementById('quota_floor_percent').value||'10'),
      photo_burn_percent: parseFloat(document.getElementById('quota_photo_burn').value||'0.5'),
      enabled: document.getElementById('quota_enabled').checked,
    },
    config: {
      MEMORY_THRESHOLD: parseFloat(document.getElementById('memory_threshold').value||'75'),
      DAILY_PHOTO_QUANTITY: parseInt(document.getElementById('daily_qty').value||'5'),
      UNMEANINGFUL_THRESHOLD: parseFloat(document.getElementById('unmeaningful_threshold').value||'40'),
      VLM_MAX_LONG_EDGE: parseInt(document.getElementById('vlm_max_long_edge').value||'1024'),
      HOME_MIN_SCORE: parseFloat(document.getElementById('home_min_score').value||'60'),
      HOME_HIDE_UNMEANINGFUL: document.getElementById('home_hide_unmeaningful').checked,
      DEDUPE_ENABLED: document.getElementById('cfg_dedupe_enabled').checked,
      DEDUPE_SIMILAR_ENABLED: document.getElementById('cfg_dedupe_similar').checked,
      DEDUPE_HAMMING_MAX: parseInt(document.getElementById('cfg_dedupe_hamming').value||'6'),
      DEDUPE_BURST_GAP_SEC: parseInt(document.getElementById('cfg_dedupe_burst_gap').value||'3'),
      RECENT_EXCLUDE_DAYS: parseInt(document.getElementById('cfg_recent_days').value||'30'),
    }
  };
  try{
    const r = await getJSON('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    document.getElementById('saveMsg').textContent = r.ok ? '已保存 ✓' : '保存失败: '+(r.error||'');
  }catch(e){ document.getElementById('saveMsg').textContent='保存失败: '+e; }
}

async function refreshQuota(){
  const box = document.getElementById('quotaBox');
  box.innerHTML = '查询中…';
  try{
    const q = await getJSON('/api/quotas');
    let html = '';
    // MiniMax TokenPlan：进度条 + 百分比
    const mm = q['minimax'];
    if(mm && mm.ok && mm.data){
      const d = mm.data;
      const bar = (p)=>{
        p = Math.max(0, Math.min(100, p));
        const color = p>50?'success':(p>15?'warn':'err');
        return '<div class="bar"><div class="fill '+color+'" style="width:'+p+'%"></div></div>';
      };
      const p5 = (d.percent==null)?0:d.percent;
      const pw = (d.weekly_percent==null)?0:d.weekly_percent;
      const rem = (d.remains_time_sec==null)?'?':(Math.round(d.remains_time_sec/60)+' 分钟');
      html += '<div class="quota-card">';
      html += '<div class="quota-title">💰 MiniMax 额度</div>';
      html += '<div class="quota-row"><span class="q-label">本 5 小时窗口</span>'+bar(p5)+'<span class="q-pct">'+p5.toFixed(1)+'%</span></div>';
      html += '<div class="q-note">窗口重置还有 '+rem+'</div>';
      html += '<div class="quota-row"><span class="q-label">本周</span>'+bar(pw)+'<span class="q-pct">'+pw.toFixed(1)+'%</span></div>';
      html += '</div>';
    } else {
      html += '<div class="err">MiniMax 额度查询失败：'+((mm&&mm.error)||'')+'</div>';
    }
    // 其它渠道
    for(const [k,v] of Object.entries(q)){
      if(k==='minimax') continue;
      if(v.note){ html += '<div class="q-note">'+k+'：'+v.note+'</div>'; }
      else if(v.ok && v.data){ html += '<div class="q-note">'+k+'：'+JSON.stringify(v.data.balance_infos||v.data)+'</div>'; }
      else { html += '<div class="q-note">'+k+'：查询失败 '+(v.error||'')+'</div>'; }
    }
    box.innerHTML = html;
  }catch(e){ box.innerHTML = '<div class="err">查询失败: '+e+'</div>'; }
}

// —— 相框设备状态（数据来自设备拉取时的被动记录，固件无需改动）——
function agoText(iso){
  try{
    const t = new Date(iso).getTime();
    if(isNaN(t)) return '';
    const s = Math.max(0, (Date.now()-t)/1000);
    if(s < 60) return '刚刚';
    if(s < 3600) return Math.floor(s/60)+' 分钟前';
    if(s < 86400) return Math.floor(s/3600)+' 小时前';
    return Math.floor(s/86400)+' 天前';
  }catch(e){ return ''; }
}
async function refreshDevice(){
  const seenEl = document.getElementById('dev_last_seen');
  if(!seenEl) return;
  try{
    const d = await getJSON('/api/device');
    const seen = d.last_seen_at || '';
    seenEl.textContent = seen ? (seen.replace('T',' ') + '（' + agoText(seen) + '）') : '暂无设备接入';
    const bt = d.by_target || {};
    const k = Object.keys(bt)[0];
    document.getElementById('dev_last_file').textContent = k ? (k + ' → ' + bt[k].last_file) : '—';
    document.getElementById('dev_last_ip').textContent = d.last_ip || '—';
    document.getElementById('dev_pull_count').textContent = (d.pull_count||0) + ' 次';
  }catch(e){
    seenEl.textContent = '读取失败：' + e;
  }
}
async function doNext(){
  const note = document.getElementById('dev_note');
  note.textContent = '正在换一张…';
  try{
    const r = await getJSON('/api/next', {method:'POST'});
    note.textContent = r.msg || (r.ok ? '已开始' : '失败');
    if(r.ok) setTimeout(refreshDevice, 5000);
  }catch(e){
    note.textContent = '换一张失败：' + e;
  }
}
// 去重索引：点按钮后每 2 秒轮询进度，跑完显示统计。
// 进度文件在跑完后【不删】，所以隔天再打开这一页，仍能看到上次重建的结果。
let _dedupeTimer = null;
const DEDUPE_STAGE_ZH = {1:'读取拍摄信息', 2:'比对文件内容', 3:'划定候选范围', 4:'比对感知哈希'};

function fmtDur(sec){
  const s = Math.round(sec || 0);
  if(s < 60) return s + ' 秒';
  const m = Math.floor(s / 60);
  if(m < 60) return m + ' 分 ' + (s % 60) + ' 秒';
  return Math.floor(m / 60) + ' 小时 ' + (m % 60) + ' 分';
}

// 状态行默认藏着（没话说时不留空行）；一旦有内容就显出来
function _dedupeSay(text){
  const note = document.getElementById('dedupe_note');
  if(!note) return;
  note.textContent = text;
  note.style.display = text ? 'block' : 'none';
}

async function refreshDedupeProgress(){
  const box = document.getElementById('dedupeProgressBox');
  const note = document.getElementById('dedupe_note');
  if(!box || !note) return;
  let r;
  try{ r = await getJSON('/api/dedupe/progress'); }catch(e){ return; }
  const p = (r && r.progress) || null;

  if(r && r.running && p){
    box.style.display = 'block';
    // 优先用后端给的 phase：第 1 步其实含"枚举目录"和"读拍摄信息"两小段，
    // 写死成阶段名的话，明明还在数文件却显示"读取拍摄信息"，是误导。
    const name = p.phase || DEDUPE_STAGE_ZH[p.stage] || '处理中';
    document.getElementById('dedupeStage').textContent =
      '第 ' + (p.stage || 1) + '/' + (p.stages || 4) + ' 步 · ' + name;
    document.getElementById('dedupePct').textContent = (p.percent || 0) + '%';
    document.getElementById('dedupeFill').style.width = Math.min(100, Math.max(0, p.percent || 0)) + '%';
    let d = (p.total > 0) ? ((p.done || 0) + ' / ' + p.total)
                          : (p.done > 0 ? ('已发现 ' + p.done + ' 个文件') : '');
    if(p.elapsed) d += (d ? '　' : '') + '已用时 ' + fmtDur(p.elapsed);
    if(p.eta) d += '　预计还需 ' + fmtDur(p.eta);
    document.getElementById('dedupeDetail').textContent = d;
    if(!_dedupeTimer) _dedupeTimer = setInterval(refreshDedupeProgress, 2000);
    return;
  }

  box.style.display = 'none';
  if(_dedupeTimer){ clearInterval(_dedupeTimer); _dedupeTimer = null; }

  if(p && p.status === 'done'){
    const s = p.stats || {};
    const when = p.finished_at ? new Date(p.finished_at).toLocaleString('zh-CN') : '';
    _dedupeSay('✅ 上次重建' + (when ? '（' + when + '）' : '') + '：共索引 ' + (s.indexed || 0)
      + ' 张、用时 ' + fmtDur(p.elapsed) + '；标记重复 ' + (s.duplicates || 0)
      + ' 张（内容相同 ' + (s.exact || 0) + '、连拍 ' + (s.burst || 0)
      + '、视觉相似 ' + (s.similar || 0) + '）');
  } else if(p && p.interrupted){
    _dedupeSay('⚠ 上次重建没有跑完（进程已退出，详见 logs/dedupe.log），索引可能不完整，建议重跑一次。');
  } else {
    _dedupeSay('');
  }
}

async function rebuildDedupe(){
  _dedupeSay('正在启动去重索引…');
  try{
    const r = await getJSON('/api/dedupe/start', {method:'POST'});
    _dedupeSay(r.msg || (r.ok ? '已开始' : '失败'));
    if(r.ok){
      // 先显示进度框，再等子进程写出第一份进度（它要先列一遍目录）
      const box = document.getElementById('dedupeProgressBox');
      if(box) box.style.display = 'block';
      setTimeout(refreshDedupeProgress, 800);
    }
  }catch(e){
    _dedupeSay('启动失败：' + e);
  }
}

async function refreshStatus(){
  const box = document.getElementById('statusBox');
  try{
    const st = await getJSON('/api/status');
    let lines = [];
    for(const j of st.jobs){
      const nm = (j.name==='analyze')?'扫描新照片':(j.name==='render'?'每日出图':j.name);
      lines.push(nm+': '+(j.enabled?'🟢 已开启':'⚪ 已停用')+' 　'+cronToChinese(j.cron)+'　下次：'+(j.next_run?new Date(j.next_run).toLocaleString('zh-CN'):'-')+(j.running?'　⏳ 运行中':''));
    }
    if(st.quota){
      const g = st.quota;
      const pct = (g.percent>0? (Math.round(g.percent)+'%') : '—');
      const gv = g.fire
        ? '⏰ 应该烧额度（剩余 '+pct+'，本次约 '+g.batch+' 张）'
        : '暂不烧（剩余 '+pct+' '+(g.reason||'')+'）';
      lines.push('额度利用：'+gv);
    }
    box.innerHTML = lines.join('<br>');
    // 根据是否在扫描，切换开始/停止按钮可用性
    const running = !!st.scan_running;
    const startBtn = document.getElementById('scanStartBtn');
    const stopBtn = document.getElementById('scanStopBtn');
    if(startBtn){ startBtn.disabled = running; startBtn.textContent = running ? '⏳ 扫描中…' : '▶ 按额度烧到底线'; }
    if(stopBtn){ stopBtn.disabled = !running; }
    refreshScanProgress(running);
  }catch(e){ box.textContent = '查询失败: '+e; }
}

// 扫描进度：轮询 /api/scan/progress，更新进度条与文案
let _scanProgressTimer = null;
async function refreshScanProgress(running){
  const box = document.getElementById('scanProgressBox');
  const fill = document.getElementById('scanProgressFill');
  const pct = document.getElementById('scanProgressPct');
  const note = document.getElementById('scanProgressNote');
  if(!running){
    if(box) box.style.display = 'none';
    if(_scanProgressTimer){ clearInterval(_scanProgressTimer); _scanProgressTimer = null; }
    return;
  }
  if(box) box.style.display = 'block';
  try{
    const r = await getJSON('/api/scan/progress');
    const p = (r && r.progress) || null;
    if(p){
      if(fill) fill.style.width = Math.min(100, Math.max(0, p.percent||0)) + '%';
      if(pct) pct.textContent = (p.percent||0) + '%';
      if(note){
        // 列目录阶段还没有可用于算百分比的总数（total=0），这时报"已发现 N 个"
        // 比"已处理 500/0"更说得通
        const amt = (p.total > 0)
          ? ('已处理 ' + (p.done||0) + '/' + p.total)
          : ('正在枚举文件，已发现 ' + (p.done||0) + ' 个');
        note.textContent = amt + (p.current ? '　正在：'+basename(p.current) : '');
      }
    }
  }catch(e){ /* 轮询失败静默 */ }
  // 每 1.5 秒刷一次进度
  if(!_scanProgressTimer){
    _scanProgressTimer = setInterval(()=>{ refreshScanProgress(true); }, 1500);
  }
}
function basename(p){
  if(!p) return '';
  return String(p).split(/[\\/]/).pop();
}

async function startScan(){
  const msg = document.getElementById('scanMsg');
  if(msg) msg.textContent = '正在启动…';
  try{
    const r = await getJSON('/api/scan/start', {method:'POST'});
    if(msg) msg.textContent = r.ok ? '已开始 ✓' : (r.msg||'未开始');
    refreshStatus();
  }catch(e){ if(msg) msg.textContent = '启动失败: '+e; }
}

// 重新扫描：把目录里所有照片（含已入库）重新打分（改动后刷新评分/方向）
async function rescanAll(){
  const msg = document.getElementById('scanMsg');
  if(!confirm('将把相册里所有照片重新打分（含已打分的），耗时/耗额度≈一次完整扫描。确定重新扫描？')) return;
  if(msg) msg.textContent = '正在启动重新扫描…';
  try{
    const r = await getJSON('/api/scan/start', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({rescan:true})});
    if(msg) msg.textContent = r.ok ? ('✅ '+(r.msg||'已重新扫描')) : (r.msg||'未开始');
    refreshStatus();
  }catch(e){ if(msg) msg.textContent = '重新扫描失败: '+e; }
}

async function stopScan(){
  const msg = document.getElementById('scanMsg');
  try{
    const r = await getJSON('/api/scan/stop', {method:'POST'});
    if(msg) msg.textContent = r.ok ? '已停止 ✓' : (r.msg||'');
    refreshStatus();
  }catch(e){ if(msg) msg.textContent = '停止失败: '+e; }
}

initSettings();
// 去重进度独立于扫描，不在 initSettings 里：进页面就查一次，
// 正在跑就接上轮询，跑完了就把上次结果摆出来。
refreshDedupeProgress();
</script>
</div>
</body>
</html>"""


@app.get("/review")
def review():
    _require_webui_enabled()
    try:
        page = int(request.args.get("page", "1"))
    except Exception:
        page = 1

    md = (request.args.get('md', '') or '').strip()
    sort = (request.args.get('sort', '') or 'memory').strip() or 'memory'
    show_all = (request.args.get('show_all', '') or '').strip() == '1'

    # 默认隐藏低分/无意义；勾"显示全部"(show_all=1) 则不过滤
    if show_all:
        min_score, hide_unmeaningful = None, False
    else:
        min_score = float(getattr(cfg, "HOME_MIN_SCORE", 60.0) or 60.0)
        hide_unmeaningful = bool(getattr(cfg, "HOME_HIDE_UNMEANINGFUL", True))

    rows, total_count = load_rows(page=page, page_size=REVIEW_PAGE_SIZE, md=md, sort=sort,
                                  min_score=min_score, hide_unmeaningful=hide_unmeaningful)
    if not rows:
        return Response(_build_empty_review_html(), mimetype="text/html; charset=utf-8")

    html_str = build_html(rows, page=page, page_size=REVIEW_PAGE_SIZE, total_count=total_count)
    return Response(html_str, mimetype="text/html; charset=utf-8")


# API endpoint for md list
@app.get('/api/md_list')
def api_md_list():
    _require_webui_enabled()
    md_list = _load_all_md_list()
    return Response(json.dumps(md_list, ensure_ascii=False), mimetype='application/json; charset=utf-8')


@app.get("/sim")
def sim():
    _require_webui_enabled()
    selected_img = request.args.get("img", "")

    # 默认不再全库加载，避免 /sim 页面巨大 JSON 导致浏览器转圈
    sim_rows = []

    # 仅当从 /review 点进来且参数合法时，按“该日期 + 向前 30 天”加载
    if selected_img and isinstance(selected_img, str) and selected_img.startswith("/images/"):
        subpath = selected_img[len("/images/"):]
        try:
            p = _safe_join(IMAGE_DIR, subpath)
        except Exception:
            p = None

        if p is not None and p.exists() and p.is_file():
            meta = get_photo_meta_by_path(str(p))
            base_date = meta.get("date") if meta else ""

            if base_date:
                try:
                    from datetime import datetime, timedelta
                    dt0 = datetime.strptime(base_date, "%Y-%m-%d")
                    dates = [(dt0 - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(0, 31)]
                except Exception:
                    dates = [base_date]

                sim_rows = load_sim_rows_for_dates(dates)

    # 返回链接用 /review 带上来的来源页（含分页/筛选/排序），这样能回到原来那一页。
    # 必须校验前缀，否则 back= 参数会变成开放重定向。
    back_url = (request.args.get("back") or "/review").strip()
    if not back_url.startswith("/review"):
        back_url = "/review"

    html_str = build_simulator_html(sim_rows, selected_img=selected_img, back_url=back_url)
    return Response(html_str, mimetype="text/html; charset=utf-8")


@app.get("/images/<path:subpath>")
def images(subpath: str):
    _require_webui_enabled()
    try:
        p = _safe_join(IMAGE_DIR, subpath)
    except Exception:
        abort(400)
    return _send_image(p)

@app.get("/sim_render")
def sim_render():
    _require_webui_enabled()

    img_uri = request.args.get("img", "")
    if not img_uri or not img_uri.startswith("/images/"):
        abort(400)

    subpath = img_uri[len("/images/"):]
    try:
        p = _safe_join(IMAGE_DIR, subpath)
    except Exception:
        abort(400)

    if not p.exists() or not p.is_file():
        abort(404)

    meta = get_photo_meta_by_path(str(p))
    if meta is None:
        # 兜底：DB 没命中就渲染纯图（不建议长期这样）
        meta = {
            "path": str(p),
            "date": "",
            "side": "",
            "memory": None,
            "lat": None,
            "lon": None,
            "city": "",
        }

    # 详情页的分辨率切换：按屏幕名挑一块屏，未指定或名字不认识就回退默认屏。
    # 这里不限 enabled —— 预览的用途就是提前看效果，不该因为没启用就看不到。
    screen_name = (request.args.get("screen") or "").strip()
    screen = None
    if screen_name:
        for sc in (getattr(cfg, "SCREENS", None) or []):
            if isinstance(sc, dict) and str(sc.get("name")) == screen_name:
                screen = sc
                break

    try:
        img, _ = rdp.render_image(meta, screen)   # v4 返回 (canvas, orientation)
        img_dithered = rdp.apply_four_color_dither(img)

        bio = BytesIO()
        img_dithered.save(bio, format="PNG")
        bio.seek(0)
        return send_file(bio, mimetype="image/png", as_attachment=False)
    except Exception:
        abort(500)

@app.get("/static/inktime/<key>/photo_<int:idx>.bin")
def esp_photo(key: str, idx: int):
    _check_download_key(key)
    if idx < 0 or idx >= DAILY_PHOTO_QUANTITY:
        abort(404)
    p = BIN_OUTPUT_DIR / f"photo_{idx}.bin"
    _touch_device("", "", f"photo_{idx}.bin")   # 顶层兼容路径（旧固件），同样记一笔
    return _send_static_file(p)


@app.get("/static/inktime/<key>/latest.bin")
def esp_latest(key: str):
    _check_download_key(key)
    p = BIN_OUTPUT_DIR / "latest.bin"
    _touch_device("", "", "latest.bin")
    return _send_static_file(p)


@app.get("/static/inktime/<key>/preview.png")
def esp_preview(key: str):
    _check_download_key(key)
    p = BIN_OUTPUT_DIR / "preview.png"
    _touch_device("", "", "preview.png")
    return _send_static_file(p)


# ---------- 多屏 + 方向下载路由（v4 方向自适应） ----------
def _screen_direction_path(screen: str, direction: str, filename: str) -> Path:
    """校验 screen/direction 白名单并返回 <BIN_OUTPUT_DIR>/<screen>/<direction>/<filename>。

    仅允许启用屏名 + landscape/portrait；_safe_join 防目录穿越。
    """
    if direction not in ("landscape", "portrait"):
        abort(404)
    valid_names = {str(sc.get("name")) for sc in getattr(cfg, "SCREENS", []) if sc.get("enabled", True)}
    if screen not in valid_names:
        valid_names.add(str(getattr(cfg, "SCREEN", {}).get("name", "")) if isinstance(getattr(cfg, "SCREEN", None), dict) else "")
    if screen not in valid_names:
        abort(404)
    try:
        p = _safe_join(BIN_OUTPUT_DIR / screen / direction, filename)
    except ValueError:
        abort(400)
    # 所有"屏/方向"形式的下载都经过这里，顺手记一笔设备上线（不改固件）
    _touch_device(screen, direction, filename)
    return p


@app.get("/static/inktime/<key>/<screen>/<direction>/latest.bin")
def esp_latest_dir(key: str, screen: str, direction: str):
    _check_download_key(key)
    p = _screen_direction_path(screen, direction, "latest.bin")
    return _send_static_file(p)


@app.get("/static/inktime/<key>/<screen>/<direction>/photo_<int:idx>.bin")
def esp_photo_dir(key: str, screen: str, direction: str, idx: int):
    _check_download_key(key)
    p = _screen_direction_path(screen, direction, f"photo_{idx}.bin")
    return _send_static_file(p)


@app.get("/static/inktime/<key>/<screen>/<direction>/preview.png")
def esp_preview_dir(key: str, screen: str, direction: str):
    _check_download_key(key)
    p = _screen_direction_path(screen, direction, "preview.png")
    return _send_static_file(p)


@app.get("/static/inktime/<key>/<screen>/<direction>/manifest.json")
def esp_manifest_dir(key: str, screen: str, direction: str):
    _check_download_key(key)
    p = _screen_direction_path(screen, direction, "manifest.json")
    return _send_static_file(p)


@app.get("/files/")
@app.get("/files/<path:subpath>")
def browse(subpath: str = ""):
    _require_webui_enabled()
    try:
        p = _safe_join(BIN_OUTPUT_DIR, subpath)
    except Exception:
        abort(400)

    if p.is_file():
        return _send_static_file(p)

    if not p.exists() or not p.is_dir():
        abort(404)

    items = []
    for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        name = child.name + ("/" if child.is_dir() else "")
        rel = child.relative_to(BIN_OUTPUT_DIR)
        href = "/files/" + str(rel).replace("\\", "/")
        items.append(f'<li><a href="{html.escape(href)}">{html.escape(name)}</a></li>')

    up = ""
    if p != BIN_OUTPUT_DIR:
        parent_rel = p.parent.relative_to(BIN_OUTPUT_DIR)
        up_href = "/files/" + str(parent_rel).replace("\\", "/")
        up = f'<a href="{html.escape(up_href)}">⬅ 返回上级</a><br><br>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>InkTime Files</title>
<style>
body {{ font-family: -apple-system,BlinkMacSystemFont,system-ui,sans-serif; padding: 24px; }}
ul {{ line-height: 1.8; }}
code {{ background:#f2f2f2; padding:2px 6px; border-radius:4px; }}
</style>
</head>
<body>
<h3>输出目录浏览</h3>
<p>当前：<code>{html.escape(str(p.relative_to(BIN_OUTPUT_DIR) if p != BIN_OUTPUT_DIR else "."))}</code></p>
{up}
<ul>
{''.join(items)}
</ul>
</body>
</html>
"""


if __name__ == "__main__":
    mimetypes.add_type("application/octet-stream", ".bin")
    print(f"[InkTime] DB: {DB_PATH}")
    print(f"[InkTime] IMAGE_DIR: {IMAGE_DIR}")
    print(f"[InkTime] OUT: {BIN_OUTPUT_DIR}")
    print(f"[InkTime] key: {DOWNLOAD_KEY}")
    print(f"[InkTime] listen: {FLASK_HOST}:{FLASK_PORT}")
    print(f"[InkTime] open: http://127.0.0.1:{FLASK_PORT}/  (本机)")

    # 首次打开自动建表（photos.db + photo_scores），避免 /review 报 no such table
    try:
        ensure_review_table()
        print(f"[InkTime] 数据库表已就绪（photos.db）")
    except Exception as e:
        print(f"[InkTime] 数据库初始化失败（继续运行）：{e}")

    # 应用 Web 设置 + 启动后台调度器（定时扫描/渲染）
    try:
        web_settings.apply_to_config(cfg)
        _refresh_quota_globals()  # 启动时也把 QUOTA_* 同步成设置页保存的最新值
    except Exception as e:
        print(f"[InkTime] 应用 web_settings 失败（继续运行）：{e}")
    try:
        scheduler.reload_from_settings()
        scheduler.start()
        print(f"[InkTime] 后台调度器已启动（定时分析/渲染）")
    except Exception as e:
        print(f"[InkTime] 调度器启动失败（不影响服务）：{e}")

    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False)