#pragma once

#include "config_store.h"

// 启动 AP 配网页（SSID = InkTime-XXXX，密码 12345678，浏览器开 192.168.4.1）。
//
// 进入后是个死循环，只有两种情况会出来：
//   · 用户点了保存 → 写 NVS 并重启，本函数【不返回】；
//   · 超时（只有手动触发时才有超时）→ 返回，调用方继续走正常流程。
//
// 出厂未配置（cur.valid == false）时【不设超时】—— 否则晾它几分钟就再也配不上了，
// 只能拔电池重来。
//
// 顺带同步一次时间（AP 模式下连不上 NTP，配置页上的时间是拿不到有效时钟的）
void netPortalRun(const FrameConfig& cur, unsigned long timeoutMs);

// ── 局域网配网页 ──────────────────────────────────────────────────────
// WiFi 已经连上时，用 80 端口在 STA 接口上提供同一个配置表单 ——
// 手机/电脑在同一个 WiFi 下直接打开 http://<板子IP>/ 就能改配置，不用连热点。
//
// 和 AP 配网页共用同一个 WebServer 对象，所以两者不能同时开。
// 用法：清醒窗口开始时 lanConfigBegin()，循环里频繁 lanConfigTick()，
//       进深睡前 lanConfigStop()。
void lanConfigBegin(const FrameConfig& cur);
void lanConfigTick();
void lanConfigStop();
