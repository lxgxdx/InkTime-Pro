#pragma once

#include <Arduino.h>

// 存在 NVS（Preferences，命名空间 "inktime"）里的用户配置。
// 出厂时全空 → valid=false → 直接进 AP 配网，不需要用户按键。
struct FrameConfig {
  String  wifiSsid;
  String  wifiPass;
  String  hostPort;        // "192.168.88.68:8765"，可带也可不带 http://
  String  downloadKey;     // 与服务器 config.py 的 DOWNLOAD_KEY 一致
  String  panel;           // 屏幕型号 = 服务器 SCREENS 里的 name（"GDEP073E01" / "7.09_E6"）
  int32_t tzOffsetHours;   // 时区，默认 +8
  uint8_t refreshHour;     // 每天几点拉图，0..23
  uint16_t chargingRedrawMin;  // 充电时每隔多少分钟重绘一次电量；0 = 不重绘
  bool    valid;           // wifiSsid 非空即视为已配置
};

void configLoad(FrameConfig& c);
void configSave(const FrameConfig& c);
void configClear();          // 恢复出厂：清空整个命名空间

// 上次对时成功的时间戳。没有 NTP 时靠它推算下一个唤醒点 ——
// 深睡是定时的，算错就会睡过头或醒来太频繁。
void configSaveLastEpoch(uint32_t epoch);
bool configLoadLastEpoch(uint32_t& out);
