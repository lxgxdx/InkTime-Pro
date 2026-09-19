#include "config_store.h"

#include <Preferences.h>

#include "board.h"

namespace {
constexpr char NS[] = "inktime";
constexpr char KEY_LAST_EPOCH[] = "last_epoch";
Preferences prefs;

// ── 为什么每个键都要先 isKey() 判一下再读 ──────────────────────────────
// Preferences 的 getXxx(key, default) 在键不存在时会往串口吐一行
//   [E][Preferences.cpp:483] getString(): nvs_get_str len fail: ssid NOT_FOUND
// 值本身是会被 default 兜住的、功能完全正常，但首次上电的日志里顶着几行
// [E] 级别的红字，谁看了都以为板子坏了。
//
// （第一版只在开头用读写方式打开命名空间，结果从 1 行红字变成 4 行 ——
//   命名空间是建出来了，但里面的键还是缺的，逐个键继续报。所以得逐键判。）
// isKey() 走的是 nvs_get_type，取不到只是返回 false，不写日志。
// ────────────────────────────────────────────────────────────────────
// FNV-1a。只用来比对"两次保存的是不是同一个值"，不用于任何安全用途。
uint32_t passFingerprint(const String& s) {
  uint32_t h = 2166136261u;
  for (size_t i = 0; i < s.length(); ++i) {
    h ^= (uint8_t)s[i];
    h *= 16777619u;
  }
  return h;
}

String getStr(const char* key, const char* def) {
  return prefs.isKey(key) ? prefs.getString(key, def) : String(def);
}
int32_t getI32(const char* key, int32_t def) {
  return prefs.isKey(key) ? (int32_t)prefs.getInt(key, def) : def;
}
uint32_t getU32(const char* key, uint32_t def) {
  return prefs.isKey(key) ? (uint32_t)prefs.getULong(key, def) : def;
}
uint16_t getU16(const char* key, uint16_t def) {
  return prefs.isKey(key) ? (uint16_t)prefs.getUShort(key, def) : def;
}
uint8_t getU8(const char* key, uint8_t def) {
  return prefs.isKey(key) ? (uint8_t)prefs.getUChar(key, def) : def;
}
}  // namespace

void configLoad(FrameConfig& c) {
  // 用读写方式打开（而不是只读）：首次上电时 NVS 里还没有这个命名空间，
  // 只读打开会失败，ESP-IDF 就往串口吐一行
  //   [E][Preferences.cpp:50] begin(): nvs_open failed: NOT_FOUND
  // 那是【预期行为】、配置会被下面的默认值兜住，但一个 [E] 级别的红字摆在
  // 首次上电日志里，谁看了都以为板子坏了。读写打开会顺手把命名空间建出来，
  // 就干净了。代价可以忽略 —— 每次开机只读一次配置。
  prefs.begin(NS, /*readOnly=*/false);   // 读写打开：首次上电顺手建出命名空间
  c.wifiSsid      = getStr("ssid", "");
  c.wifiPass      = getStr("pass", "");
  c.hostPort      = getStr("hostport", "");
  c.downloadKey   = getStr("dlkey", DEFAULT_DOWNLOAD_KEY);
  c.tzOffsetHours = getI32("tz", 8);
  c.refreshHour   = getU8("hour", 8);
  c.chargingRedrawMin = getU16("chgmin", 20);
  prefs.end();

  if (c.tzOffsetHours < -12 || c.tzOffsetHours > 14) c.tzOffsetHours = 8;
  if (c.refreshHour > 23) c.refreshHour = 8;
  if (c.chargingRedrawMin > 240) c.chargingRedrawMin = 240;
  if (c.downloadKey.length() == 0) c.downloadKey = DEFAULT_DOWNLOAD_KEY;

  c.valid = (c.wifiSsid.length() > 0);

  // 密码只报"有没有"和"指纹"，不打印原文。
  //
  // 指纹（长度 + FNV-1a）是为了回答一个具体问题：**这次保存到底生效了没有？**
  // 排查 WiFi 连不上时，如果每次都只看"已设置"，就分不清"密码改了但还是错的"
  // 和"压根没存进去、还在用旧的"。有了指纹，两次开机的值一比就知道了。
  Serial.printf("[配置] ssid=%s pass=%s hostport=%s tz=%d hour=%d key=%s valid=%d\n",
                c.wifiSsid.c_str(),
                c.wifiPass.length() ? String("(len=" + String(c.wifiPass.length()) +
                                             ",fp=" + String(passFingerprint(c.wifiPass), HEX) + ")").c_str()
                                    : "(空!)",
                c.hostPort.c_str(), (int)c.tzOffsetHours, (int)c.refreshHour,
                c.downloadKey.c_str(), (int)c.valid);
}

void configSave(const FrameConfig& c) {
  prefs.begin(NS, /*readOnly=*/false);
  prefs.putString("ssid", c.wifiSsid);
  prefs.putString("pass", c.wifiPass);
  prefs.putString("hostport", c.hostPort);
  prefs.putString("dlkey", c.downloadKey);
  prefs.putInt("tz", c.tzOffsetHours);
  prefs.putUChar("hour", c.refreshHour);
  prefs.putUShort("chgmin", c.chargingRedrawMin);
  prefs.end();
  Serial.println("[配置] 已保存");
}

void configClear() {
  prefs.begin(NS, /*readOnly=*/false);
  prefs.clear();
  prefs.end();
  Serial.println("[配置] 已清空（恢复出厂）");
}

void configSaveLastEpoch(uint32_t epoch) {
  if (epoch == 0) return;
  prefs.begin(NS, /*readOnly=*/false);
  prefs.putULong(KEY_LAST_EPOCH, epoch);
  prefs.end();
}

bool configLoadLastEpoch(uint32_t& out) {
  prefs.begin(NS, /*readOnly=*/true);
  const uint32_t v = getU32(KEY_LAST_EPOCH, 0);
  prefs.end();
  if (v == 0) return false;
  out = v;
  return true;
}
