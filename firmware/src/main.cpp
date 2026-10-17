// InkTime 相框固件 —— ESP32-S3-WROOM-1-N8R8 + GDEP073E01（7.3" 800×480 六色）
//
// 一次开机（或一次定时唤醒）的流程：
//   判上电按住的键 → 连 WiFi → 对时 → 读 manifest → 拉图 → 合成电量图标 → 上屏
//   → 清醒窗口（按键可用）/ 充电模式（按间隔重绘电量）→ 深睡到下一个刷新点
//
// 几条来自硬件、不能靠软件绕开的约束：
//   · 按键在 IO45/47/48，【不是 RTC GPIO】→ 深睡时叫不醒，只在清醒窗口内有效
//   · v2 起屏供电由 IO12 经高边 P-MOSFET 控制：高=升压开，低/悬空=屏彻底断电。
//     复位后 GPIO 高阻 + 板上 R19 下拉 = 【上电默认断电】，所以 setup 第一件事
//     就是 boardPanelPowerOn()，而且必须在任何一次 epdBegin() 之前。
//     （v1 是把 MT3608 的 EN 直接挂电池上，屏永久带电，从来没管过屏供电。）
//   · IO45 是 strapping 脚，任何 ESP.restart() 之前必须先 releaseStrappingPins()
//
// 串口走原生 USB（板子只引了 IO19/20），platformio.ini 里开了 ARDUINO_USB_CDC_ON_BOOT。

#include <Arduino.h>
#include <WiFi.h>
#include <time.h>

#include "esp_heap_caps.h"
#include "esp_sleep.h"
#include "esp_system.h"   // esp_reset_reason()
#include "esp_wifi.h"

#include "battery.h"
#include "board.h"
#include "config_store.h"
#include "daily_fetch.h"
#include "epd709.h"
#include "epd_display.h"
#include "led.h"
#include "net_portal.h"
#include "overlay.h"
#include "selftest.h"

namespace {

// ======================= 时长常量 =======================
// 配网模式的保底超时。设 30 分钟而不是 10 分钟 —— 配网本身要手机操作、还要
// 跟手机的网络切换较劲（它看这个热点没网会自己切走），10 分钟经常不够用，
// 超时一次就得拔插 USB 重来。
constexpr uint32_t AP_PORTAL_TIMEOUT_MS   = 30UL * 60UL * 1000UL;
constexpr uint32_t WIFI_CONNECT_TIMEOUT_MS = 20UL * 1000UL;
constexpr uint32_t AWAKE_WINDOW_MS        = 10UL * 60UL * 1000UL;  // 刷完图后保持清醒多久
constexpr uint32_t NO_IMAGE_RETRY_MIN     = 30;    // 没拿到图时，多久后重试
constexpr uint32_t NEXT_BATCH_WAIT_MS     = 90UL * 1000UL;         // 等服务器重渲染的上限

// ======================= 运行期状态 =======================
FrameConfig   g_cfg;
DailyManifest g_man;
uint8_t*      g_photo = nullptr;   // 服务器原图，只读 —— 重绘时从这里复原
uint8_t*      g_work  = nullptr;   // 工作副本，电量图标画在这上面
int           g_idx = 0;           // 当前显示的是该方向里的第几张

size_t canvasBytes() {
  if (!g_man.ok || g_man.canvasW <= 0 || g_man.canvasH <= 0) return 0;
  return (size_t)g_man.canvasW * (size_t)g_man.canvasH;
}

// ======================= 启动按键判定 =======================
// 按键对地、无外部上拉 → 要用内部上拉读：空闲高、按下低
bool heldAtBoot(int pin) {
  pinMode(pin, INPUT_PULLUP);
  delay(5);   // 等上拉拉稳
  return digitalRead(pin) == LOW;
}

// ======================= WiFi / 时间 =======================
// 断开原因码的中文提示。
//
// WiFi.status() 只说"连不上"（=6），真正的原因在这个码里。最常见的两个方向完全
// 相反：15/205/202 = 密码不对；201 = 扫不到这个热点（名字错/5G）。
// 不报这个码的话，两种情况的日志长得一模一样。
const char* wifiReasonHint(int r) {
  switch (r) {
    case 2:   return "  ← 认证过期（密码错，或路由器拒绝）";
    case 15:  return "  ← 四次握手超时，几乎总是【密码不对】";
    case 202: return "  ← 认证失败，【密码不对】";
    case 203: return "  ← 关联失败（路由器可能开了 MAC 过滤 / 客户端数满）";
    case 204: return "  ← 握手超时";
    case 205: return "  ← 四次握手失败，【密码不对】";
    case 201: return "  ← 扫不到这个热点（名字写错，或填成了 5G）";
    default:  return "";
  }
}

// 注意用 arduino_event_t* 这个重载：Arduino ESP32 2.x 里 WiFi.onEvent 有两套回调，
// 只收 arduino_event_id_t 的那个【拿不到事件数据】——它只告诉你"发生了个事件"，
// 拿不到 reason。要读原因码必须走指针这版。
void onWiFiEvent(arduino_event_t* event) {
  if (event->event_id != ARDUINO_EVENT_WIFI_STA_DISCONNECTED) return;
  const int r = (int)event->event_info.wifi_sta_disconnected.reason;
  Serial.printf("[WiFi] 断开，原因码 %d%s\n", r, wifiReasonHint(r));
}

// 加密方式的中文名。ESP32 连不上 WPA3 是常见坑，所以单独标出来。
const char* authName(wifi_auth_mode_t m) {
  switch (m) {
    case WIFI_AUTH_OPEN:            return "开放（无密码）";
    case WIFI_AUTH_WEP:             return "WEP";
    case WIFI_AUTH_WPA_PSK:         return "WPA";
    case WIFI_AUTH_WPA2_PSK:        return "WPA2";
    case WIFI_AUTH_WPA_WPA2_PSK:    return "WPA/WPA2 混合";
    case WIFI_AUTH_WPA3_PSK:        return "WPA3 ← 很多 ESP32 连不上这个";
    case WIFI_AUTH_WPA2_WPA3_PSK:   return "WPA2/WPA3 混合";
    default:                        return "其它";
  }
}

bool connectWiFi(const FrameConfig& cfg, uint32_t timeoutMs) {
  if (cfg.wifiSsid.length() == 0) return false;

  WiFi.persistent(false);
  WiFi.onEvent(onWiFiEvent);   // 要在 begin 之前注册，否则收不到断开事件
  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_STA);
  // ⚠️ 关掉 modem sleep。开着时射频只在 DTIM 醒来，实测会周期性
  //    [WiFi] 断开，原因码 200（beacon 超时）—— 1.9MB 的大图下载正好卡在这上面。
  //    代价：醒着时功耗高一点；深睡时 WiFi 本来就关掉，不影响。
  WiFi.setSleep(false);
  WiFi.begin(cfg.wifiSsid.c_str(), cfg.wifiPass.c_str());

  const uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < timeoutMs) {
    delay(200);
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("[WiFi] 连接失败（status=%d）\n", (int)WiFi.status());

    // 光说"连不上"没法排查，扫一遍周边热点把原因分开：
    //   · 扫不到这个 SSID → 名字写错，或者是 5GHz（ESP32 只认 2.4G）
    //   · 扫得到但连不上 → 密码不对，或者路由器开了 WPA3/PMF
    //
    // ⚠️ 必须先把射频腾出来再扫：刚刚那个失败的连接尝试还占着它，立刻扫会返回
    //    -2（WIFI_SCAN_FAILED）。第一版没处理这个，于是把"扫描失败"报成了
    //    "没有这个热点"—— 结论正好反了，比不报还糟。
    WiFi.disconnect(true, true);
    delay(300);
    int n = WiFi.scanNetworks(/*async=*/false, /*show_hidden=*/true);
    if (n < 0) { delay(1200); n = WiFi.scanNetworks(false, true); }

    if (n < 0) {
      Serial.printf("[WiFi] 扫描失败（返回 %d，负数都是错误码），这次判断不了热点在不在。\n", n);
      Serial.println("[WiFi] → 射频可能还被占着，重试一次或重启板子；别据此以为热点不存在");
    } else {
      bool found = false;
      for (int i = 0; i < n; ++i) {
        if (WiFi.SSID(i) != cfg.wifiSsid) continue;
        found = true;
        Serial.printf("[WiFi] 热点「%s」在，信号 %d dBm，加密 %s\n", cfg.wifiSsid.c_str(),
                      (int)WiFi.RSSI(i), authName(WiFi.encryptionType(i)));
        break;
      }
      if (!found) {
        Serial.printf("[WiFi] 扫到 %d 个热点，里面没有「%s」。扫到的是：\n", n,
                      cfg.wifiSsid.c_str());
        for (int i = 0; i < n && i < 12; ++i) {
          Serial.printf("[WiFi]    %4d dBm  %s\n", (int)WiFi.RSSI(i), WiFi.SSID(i).c_str());
        }
        Serial.println("[WiFi] → 名字写错了，或者填成了 5G 那个 SSID（ESP32 只支持 2.4GHz）");
      } else {
        Serial.println("[WiFi] → 热点在但连不上，八成是密码不对；"
                       "若加密方式是 WPA3，把路由器改成 WPA2 或 WPA2/WPA3 混合模式");
      }
    }
    WiFi.scanDelete();
    return false;
  }
  Serial.printf("[WiFi] 已连接 %s，信号 %d dBm\n", WiFi.localIP().toString().c_str(),
                (int)WiFi.RSSI());
  return true;
}

bool syncTime(const FrameConfig& cfg) {
  configTime((long)cfg.tzOffsetHours * 3600, 0, "ntp.aliyun.com", "pool.ntp.org",
             "time.nist.gov");
  struct tm t {};
  for (int i = 0; i < 30; ++i) {
    if (getLocalTime(&t, 200)) {
      const time_t now = time(nullptr);
      if (now > 1600000000) {
        configSaveLastEpoch((uint32_t)now);
        Serial.printf("[时间] %04d-%02d-%02d %02d:%02d:%02d\n", t.tm_year + 1900,
                      t.tm_mon + 1, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec);
        return true;
      }
    }
    delay(500);
  }
  Serial.println("[时间] 对时失败，将用上次记录的时间估算唤醒点");
  return false;
}

// 到下一个刷新点还有几分钟。时钟不可用时返回兜底值。
uint32_t minutesToNextRefresh(const FrameConfig& cfg) {
  time_t now = time(nullptr);
  if (now < 1600000000) {                       // RTC 时间没建立（换过电池之类）
    uint32_t last = 0;
    if (!configLoadLastEpoch(last)) return 1440;
    now = (time_t)last;
    Serial.println("[睡眠] 当前时钟无效，用上次对时的时间估算");
  }

  struct tm t {};
  localtime_r(&now, &t);
  const int curMin = t.tm_hour * 60 + t.tm_min;
  const int target = (int)cfg.refreshHour * 60;
  int delta = (curMin < target) ? (target - curMin) : (24 * 60 - (curMin - target));
  if (delta < 1) delta = 24 * 60;

  Serial.printf("[睡眠] 现在 %02d:%02d，目标 %02d:00，还需 %d 分钟\n", t.tm_hour, t.tm_min,
                (int)cfg.refreshHour, delta);
  return (uint32_t)delta;
}

// 按当前电量/充电状态点灯。用在清醒窗口和充电模式：
//   已充满 → 绿常亮    充电中 → 红常亮    低电（<20%）→ 红常亮    其余 → 全灭
// 读不到电量计（valid=false）就当正常处理，不误报低电。
void ledShowBattery(const BatteryState& st) {
  if (st.full)                                        ledSet(false, true);
  else if (st.charging)                               ledSet(true, false);
  else if (st.valid && st.percent < BATT_LOW_PERCENT) ledSet(true, false);
  else                                                ledAllOff();
}

// 把当前状态报给服务器（设置页会显示）。失败无所谓，服务器不靠它供图。
void reportStatus() {
  const BatteryState st = batteryRead();
  reportDeviceStatus(g_cfg, st.valid ? st.percent : -1, st.voltage, st.charging, st.full,
                     g_man.ok ? g_man.orientation.c_str() : "");
}

// ======================= 取图 → 合成 → 上屏 =======================
bool showIndex(int idx) {
  const size_t n = canvasBytes();
  if (n == 0 || !g_photo) return false;

  if (!fetchPhotoBin(g_cfg, g_man, idx, g_photo, n)) return false;

  // 要画电量图标时才需要第二块缓冲；不需要就直接把原图推上屏。
  //
  // 这不是小气那 384KB —— 服务器还没给 battery_slot 的时候，g_work 就是 g_photo
  // 的逐字节副本，白占一块 PSRAM、白做一次 384KB 的 memcpy，还多一份内存压力。
  // 直接上原图，省掉整个环节。
  const uint8_t* toShow = g_photo;

  if (g_man.slotW > 0 && g_man.slotH > 0 && g_work) {
    // 必须从原图复原：否则换一张或改电量时，上一轮的图标会和新画的重叠
    memcpy(g_work, g_photo, n);
    const BatteryState st = batteryRead();
    drawBatteryOverlay(g_work, g_man.canvasW, g_man.canvasH, g_man.slotX, g_man.slotY,
                       g_man.slotW, g_man.slotH, g_man.slotScale, g_man.slotGlyphH, st);
    if (st.valid) {
      Serial.printf("[电量] %d%%  %.2fV%s%s\n", st.percent, st.voltage,
                    st.charging ? "  充电中" : "", st.full ? "  已充满" : "");
    } else {
      Serial.println("[电量] 读数无效（电量计没应答）");
    }
    toShow = g_work;
  } else {
    Serial.println("[电量] 服务器没给 battery_slot，本次不画电量图标（直接用原图）");
  }

  epdBegin();
  epdShowBuffer(toShow, g_man.canvasW, g_man.canvasH);
  g_idx = idx;
  return true;
}

// 只重画电量图标（照片不变），用于充电模式与 SW4
bool redrawBatteryOnly() {
  const size_t n = canvasBytes();
  if (n == 0 || !g_photo || !g_work) return false;
  if (g_man.slotW <= 0 || g_man.slotH <= 0) return false;

  memcpy(g_work, g_photo, n);
  const BatteryState st = batteryRead();
  drawBatteryOverlay(g_work, g_man.canvasW, g_man.canvasH, g_man.slotX, g_man.slotY,
                     g_man.slotW, g_man.slotH, g_man.slotScale, g_man.slotGlyphH, st);
  Serial.printf("[电量] 重绘：%s%d%%  %.2fV%s%s\n", st.valid ? "" : "(无效) ", st.percent,
                st.voltage, st.charging ? "  充电中" : "", st.full ? "  已充满" : "");

  epdBegin();
  epdShowBuffer(g_work, g_man.canvasW, g_man.canvasH);
  return true;
}

// ======================= 「换一张」 =======================
// 服务器重渲染是异步的：POST 之后要轮询 manifest 的 generated_at，
// 变了才说明新图真的落盘了（只看 count 不行 —— 重渲染后张数常常一样）。
bool requestNewBatchAndWait() {
  if (!requestNextBatch(g_cfg)) return false;

  Serial.println("[换一张] 已请服务器重渲染，等新图…");
  const String oldStamp = g_man.stamp;
  const uint32_t start = millis();
  while (millis() - start < NEXT_BATCH_WAIT_MS) {
    delay(3000);
    DailyManifest fresh;
    if (!fetchManifest(g_cfg, fresh)) continue;
    if (fresh.stamp.length() > 0 && fresh.stamp == oldStamp) continue;   // 还是旧的
    g_man = fresh;
    Serial.println("[换一张] 新的一批已就绪");
    return true;
  }
  Serial.println("[换一张] 等超时了，服务器可能还在渲染（或今天换不出新内容）");
  return false;
}

void showNext() {
  if (!g_man.ok || g_man.count <= 0) {
    Serial.println("[换一张] 手上还没图，忽略");
    return;
  }
  if (g_man.count > 1 && g_idx + 1 < g_man.count) {
    // 服务器一次给了好几张，先在本地轮换 —— 不用联网、立刻见效
    Serial.printf("[换一张] 本地换到第 %d 张（共 %d 张）\n", g_idx + 1, g_man.count);
    if (!showIndex(g_idx + 1)) Serial.println("[换一张] 上屏失败");
    return;
  }

  // 本地这几张看完了，向服务器要新的一批
  if (requestNewBatchAndWait()) {
    showIndex(0);
  } else if (g_man.count > 1) {
    showIndex(0);   // 要不到新的就转回第一张，别卡着不动
  }
}

// ======================= 按键 =======================
enum class Btn { None, Next, Refresh, Batt };

Btn pollButtons() {
  const int pins[3] = {PIN_BTN_NEXT, PIN_BTN_REFRESH, PIN_BTN_BATT};
  for (int i = 0; i < 3; ++i) {
    if (digitalRead(pins[i]) != LOW) continue;
    delay(BTN_DEBOUNCE_MS);
    if (digitalRead(pins[i]) != LOW) continue;
    // 等松手，但加个上限 —— 卡住的按键不该让整机僵在这里
    const uint32_t t0 = millis();
    while (digitalRead(pins[i]) == LOW && millis() - t0 < 5000) delay(10);
    return (Btn)(i + 1);
  }
  return Btn::None;
}

void enableButtons() {
  pinMode(PIN_BTN_NEXT, INPUT_PULLUP);
  pinMode(PIN_BTN_REFRESH, INPUT_PULLUP);
  pinMode(PIN_BTN_BATT, INPUT_PULLUP);
}

// ======================= 保持清醒 =======================
// 返回后调用方会去睡觉。两种用途：
//   · 刷完图后的清醒窗口（给用户时间按键）
//   · 充电模式（额外按间隔重绘电量，拔电就退出）
void stayAwake(uint32_t durationMs, uint32_t batteryRedrawMs, bool exitWhenUnplugged) {
  enableButtons();
  const uint32_t start = millis();
  uint32_t lastRedraw = millis();

  for (;;) {
    lanConfigTick();          // 清醒期间处理局域网配置页的请求
    const uint32_t now = millis();

    if (exitWhenUnplugged && digitalRead(PIN_CHRG) != LOW) {
      Serial.println("[充电] 检测到已拔电，退出充电模式");
      return;
    }
    if (now - start >= durationMs) return;

    if (batteryRedrawMs > 0 && now - lastRedraw >= batteryRedrawMs) {
      lastRedraw = now;
      redrawBatteryOnly();      // 内部会刷 40 秒，回来时 now 已经变了
      reportStatus();           // 充电中每重绘一次就顺带上报，设置页能看出电量在涨
      ledShowBattery(batteryRead());   // 电量跨过阈值时把灯跟上（充完转绿等）
      continue;
    }

    switch (pollButtons()) {
      case Btn::Next:
        Serial.println("[按键] 换一张");
        ledSet(false, true);      // 绿常亮 = 正在刷屏（要等 40 秒）
        showNext();
        ledShowBattery(batteryRead());
        lastRedraw = millis();    // 刚刷过屏，重置充电重绘计时
        break;
      case Btn::Refresh:
        Serial.println("[按键] 重新拉当前图");
        ledSet(false, true);
        if (showIndex(g_idx)) Serial.println("[按键] 已刷新");
        else Serial.println("[按键] 拉取失败");
        ledShowBattery(batteryRead());
        lastRedraw = millis();
        break;
      case Btn::Batt: {
        const BatteryState st = batteryRead();
        Serial.printf("[按键] 电量 %s%d%%  %.2fV%s%s\n", st.valid ? "" : "(无效) ",
                      st.percent, st.voltage, st.charging ? "  充电中" : "",
                      st.full ? "  已充满" : "");
        redrawBatteryOnly();
        ledShowBattery(st);
        lastRedraw = millis();
        break;
      }
      case Btn::None:
        delay(50);
        break;
    }
  }
}

// ======================= 深睡 =======================
void goDeepSleepMinutes(uint32_t minutes) {
  if (minutes < 1) minutes = 1;
  if (minutes > 1440) minutes = 1440;
  Serial.printf("[睡眠] %u 分钟后唤醒\n", (unsigned)minutes);

  // ⚠️ IO45 是 strapping 脚。我们为了读按键给它开过内部上拉（=高），
  //    带着上拉进深睡、下次复位时有可能把 VDD_SPI 锁存成 1.8V —— 那样就起不来了。
  //    本模块是 3.3V flash，必须先撤掉。
  releaseStrappingPins();
  pinMode(PIN_BTN_REFRESH, INPUT);
  pinMode(PIN_BTN_BATT, INPUT);

  // 两颗指示灯全灭，然后放开引脚。亮着的话各吃 1.3mA —— 比整机深睡电流的目标
  // 还大，省下来的电不该被指示灯吃掉。
  ledAllOff();
  pinMode(PIN_LED_RED, INPUT);
  pinMode(PIN_LED_GREEN, INPUT);

  // 屏相关引脚置为输入下拉，别在睡眠时往外灌电流。
  // PIN_EPD_EN 也在里面 —— 它一低，升压输入就被 Q1 断开，屏彻底断电；这是 v2
  // 深睡电流能降下来的关键。（板上 R19 另有一路 100k 下拉兜底，不靠内部下拉。）
  const int epdPins[] = {PIN_EPD_BUSY, PIN_EPD_RST, PIN_EPD_DC,
                         PIN_EPD_CS,   PIN_EPD_SCLK, PIN_EPD_DIN,
                         PIN_EPD_EN};
  for (int p : epdPins) pinMode(p, INPUT_PULLDOWN);

  if (g_photo) { free(g_photo); g_photo = nullptr; }
  if (g_work)  { free(g_work);  g_work  = nullptr; }

  lanConfigStop();          // 先把局域网配置页关掉，再关射频
  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_OFF);
  esp_wifi_stop();

  // RTC_PERIPH / RTC_FAST 关掉省电。
  // 但【不动 RTC_SLOW_MEM】：系统时钟存在那儿，关了它下次醒来时间就是乱的，
  // 算不对下一个唤醒点。这几微安的代价换来的是睡眠时刻准确，值。
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH, ESP_PD_OPTION_OFF);
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_FAST_MEM, ESP_PD_OPTION_OFF);

  esp_sleep_enable_timer_wakeup((uint64_t)minutes * 60ULL * 1000000ULL);

  Serial.flush();
  esp_deep_sleep_start();
}

}  // namespace

// ======================= setup =======================
void setup() {
  // 最优先：清掉上次深睡留下的 GPIO hold，否则按键读不到、屏也复位不了
  releaseStrappingPins();

  Serial.begin(115200);
  delay(300);
  Serial.println();
  Serial.println("===== InkTime 相框 (GDEP073E01 / ESP32-S3) =====");

  // 上电最先做的两件事：把屏供电打开（v2 新增的高边开关），再闪一下绿灯表示
  // "固件活着"。趁早做还有个好处 —— 后面那串诊断正好给升压留够起振时间。
  ledBegin();
  boardPanelPowerOn();
  ledFlashGreen(1);

  // 打印【上一次】复位的原因 —— 排查"一连热点板子就重启"时这是最直接的证据，
  // 能把两种完全不同的病根一刀分开：
  //   欠压复位 → 供电问题（发射瞬时电流太大）
  //   PANIC    → 固件崩溃（HTTP 处理里有 bug）
  // 不打印这个的话，两者现象一模一样，只能靠猜。
  {
    const esp_reset_reason_t r = esp_reset_reason();
    const char* nm = "其它";
    switch (r) {
      case ESP_RST_POWERON:   nm = "上电"; break;
      case ESP_RST_SW:        nm = "软件重启"; break;
      case ESP_RST_PANIC:     nm = "★★崩溃 PANIC★"; break;
      case ESP_RST_INT_WDT:   nm = "中断看门狗"; break;
      case ESP_RST_TASK_WDT:  nm = "任务看门狗"; break;
      case ESP_RST_WDT:       nm = "看门狗"; break;
      case ESP_RST_DEEPSLEEP: nm = "深睡唤醒"; break;
      case ESP_RST_BROWNOUT:  nm = "★★欠压复位 BROWNOUT★"; break;
      case ESP_RST_SDIO:      nm = "SDIO"; break;
      default: break;
    }
    Serial.printf("[系统] 上次复位原因: %s (代码 %d)\n", nm, (int)r);
  }
  // 开机先把身家报清楚：上板排查时这一行能立刻告诉你是几兆的 flash、
  // PSRAM 有没有起来 —— 一帧 384000 字节，两个缓冲没有 PSRAM 是装不下的。
  Serial.printf("[系统] %s rev%d  flash %uMB  PSRAM %uKB  可用堆 %uKB\n",
                ESP.getChipModel(), (int)ESP.getChipRevision(),
                (unsigned)(ESP.getFlashChipSize() / (1024UL * 1024UL)),
                (unsigned)(ESP.getPsramSize() / 1024UL),
                (unsigned)(ESP.getFreeHeap() / 1024UL));

  // 上电按住的键只在这里判一次。
  // 注意不判 SW5(IO0)：IO0 上电被拉低会直接进 ROM 下载模式，固件根本跑不到这。
  const bool wantSelfTest = heldAtBoot(PIN_BTN_REFRESH);   // SW3
  const bool wantPortal   = heldAtBoot(PIN_BTN_NEXT);      // SW2
  const bool want709Test  = heldAtBoot(PIN_BTN_BATT);      // SW4 → 7.09" 屏自检
  pinMode(PIN_BTN_BATT, INPUT);

  configLoad(g_cfg);
  epdSelectPanel(g_cfg.panel.c_str());   // 按配置选驱动 —— 必须在任何 epdBegin() 之前

  // 7.09" (GDEB0709E01) 首次上板自检：按住 SW4 开机。
  // 只跑驱动 + 色带，不碰服务器，用来验证供电/SPI/双片选/颜色码这条链。
  if (want709Test) {
    Serial.println("[BOOT] SW4 按住 → 7.09\" GDEB0709E01 自检（1200x1600 双片选）");
    epd709Begin();
    epd709ColorBars();
    Serial.println("[BOOT] 709 自检完成：左半屏应为 黑/白/黄，右半屏 红/蓝/绿");
    Serial.println("[BOOT] 若左右对调 → 改 epd709.cpp 里 CS_M/CS_S 的映射");
    goDeepSleepMinutes(10);
  }

  // 自检【只在按住 SW3 时】跑。
  //
  // 曾经让"未配置 → 开机自动自检"，本意是给新板一个直观提示。**实测这个设计有害**：
  // 调试配网时只要板子复位一次，就会在屏上刷 35 秒色块，而这 35 秒里热点是关着的
  // —— 现象就是"一连热点板子就重启、怎么都连不上"，把真正的病根（供电欠压）
  // 掩盖成了一堆假象。调试期间自动流程越少越好。
  if (wantSelfTest) {
    // SW3 的六色自检是照 7.3" 屏写的（走 GxEPD2 的 CS=IO46）；7.09" 用 SW4
    if (g_cfg.panel.indexOf("7.09") >= 0) {
      Serial.println("[BOOT] SW3 六色自检只适用于 7.3\" 屏；7.09\" 请按住 SW4");
    } else {
      Serial.println("[BOOT] SW3 按住 → 六色自检");
      runSelfTest();
    }
  }

  if (wantPortal) {
    // SW2 按住 = 打开配网页，【保留现有配置】。
    // 以前这里会 configClear()，但改个屏幕型号/密码就把 WiFi 全清掉太粗暴 ——
    // 配网页本来就会把当前值回填到表单里，不清也能改。
    Serial.println("[BOOT] SW2 按住 → 进入配网页（保留现有配置）");
    ledSet(true, false);        // 红灯常亮 = 在配网等人操作
    netPortalRun(g_cfg, AP_PORTAL_TIMEOUT_MS);
    // 保存的话 netPortalRun 内部直接重启；走到这里说明是超时返回
    configLoad(g_cfg);
    epdSelectPanel(g_cfg.panel.c_str());
  }

  if (!g_cfg.valid) {
    Serial.println("[BOOT] 尚未配置 → 进配网模式（不设超时，配好为止）");
    ledSet(true, false);        // 红灯常亮 = 在配网等人操作
    netPortalRun(g_cfg, 0);
  }

  // 主循环用 240MHz：上屏那 150 万次逐像素绘制在 80MHz 下太肉
  setCpuFrequencyMhz(240);

  batteryBegin();
  pinMode(PIN_CHRG, INPUT);
  pinMode(PIN_STDBY, INPUT);

  // 开机就把充电状态打出来。CHRG/STDBY 是 TP4056 的两个开漏脚，低电平有效。
  // 插着 USB 却读到两个都高 = TP4056 没在工作（没插好 / 芯片没焊好 / 电池回路不通）。
  {
    const int chrg = digitalRead(PIN_CHRG);
    const int stdby = digitalRead(PIN_STDBY);
    Serial.printf("[充电] CHRG=%d STDBY=%d → %s\n", chrg, stdby,
                  chrg == LOW ? "正在充电"
                              : (stdby == LOW ? "已充满"
                                              : "两个都是高：没插充电器，或充电回路没通"));
  }

  if (!connectWiFi(g_cfg, WIFI_CONNECT_TIMEOUT_MS)) {
    Serial.println("[BOOT] 连不上 WiFi → 进配网让用户改（10 分钟没动静就先去睡）");
    ledSet(true, false);        // 红灯常亮 = 在配网等人操作
    netPortalRun(g_cfg, AP_PORTAL_TIMEOUT_MS);
    configLoad(g_cfg);          // 配网页可能改过配置
    epdSelectPanel(g_cfg.panel.c_str());
    if (!connectWiFi(g_cfg, WIFI_CONNECT_TIMEOUT_MS)) {
      ledFlashRed(3);
      goDeepSleepMinutes(NO_IMAGE_RETRY_MIN);
    }
  }

  // 从这里一路绿灯常亮：对时 → 取图 → 刷屏。刷屏那 40 秒里它是"固件在跑"的
  // 唯一外部指示 —— v1 排查"屏不亮"时最缺的就是这个（日志全正常、屏是黑的）。
  ledSet(false, true);

  syncTime(g_cfg);

  if (!fetchManifest(g_cfg, g_man)) {
    Serial.printf("[BOOT] 拿不到图：%s\n", g_man.err);
    Serial.printf("[BOOT] %u 分钟后重试（不浪费一整天）\n", (unsigned)NO_IMAGE_RETRY_MIN);
    ledFlashRed(3);
    goDeepSleepMinutes(NO_IMAGE_RETRY_MIN);
  }

  const size_t n = canvasBytes();
  g_photo = (uint8_t*)psramAlloc(n);
  if (!g_photo) {
    Serial.println("[BOOT] 帧缓冲分配失败，无法上屏");
    ledFlashRed(3);
    goDeepSleepMinutes(NO_IMAGE_RETRY_MIN);
  }
  // 第二块缓冲【按需分配】：只有服务器给了 battery_slot（要画电量图标）时才用得着。
  // 没给就不占这块 384KB —— 现在的服务器版本就没给。
  if (g_man.slotW > 0 && g_man.slotH > 0) {
    g_work = (uint8_t*)psramAlloc(n);
    if (!g_work) Serial.println("[BOOT] 警告：电量图标的第二块缓冲没分配上，本次不画图标");
  } else {
    Serial.println("[BOOT] 服务器未提供 battery_slot，不分配第二块缓冲");
  }

  // 当天首次：在本方向的图里随机挑一张（服务器一次会渲染好几张）
  const int pick = (g_man.count > 1) ? (int)random(0, g_man.count) : 0;
  Serial.printf("[BOOT] 本方向 %d 张，随机取第 %d 张\n", g_man.count, pick);
  if (showIndex(pick)) {
    ledFlashGreen(2);      // 办成了（闪完两灯都是灭的）
  } else {
    Serial.println("[BOOT] 上屏失败");
    ledFlashRed(3);
  }
  reportStatus();

  // ---- 刷完了，接下来是清醒/充电 ----

  // 清醒期间开放局域网配置页：手机/电脑在同一个 WiFi 下直接访问 http://<板子IP>/
  lanConfigBegin(g_cfg);

  if (digitalRead(PIN_CHRG) == LOW) {
    Serial.println("[充电] 检测到正在充电 → 充电模式（保持清醒，按间隔重绘电量）");
    ledShowBattery(batteryRead());   // 充电中红 / 已充满绿
    const uint32_t redrawMs = (uint32_t)g_cfg.chargingRedrawMin * 60UL * 1000UL;
    if (redrawMs == 0) {
      Serial.println("[充电] 配网页里设成了不重绘，仍保持清醒以便按键可用");
    } else {
      Serial.printf("[充电] 每 %u 分钟重绘一次电量\n", (unsigned)g_cfg.chargingRedrawMin);
    }
    // 上限给足一天，真正的退出条件是拔电（函数内部检测）
    stayAwake(24UL * 60UL * 60UL * 1000UL, redrawMs, /*exitWhenUnplugged=*/true);
    // 刚拔电，接下来按正常流程睡到下一个刷新点
  } else {
    Serial.println("[运行] 清醒 10 分钟，期间可以按键换图 / 刷新");
    ledShowBattery(batteryRead());   // 低电红常亮，电量正常则全灭
    stayAwake(AWAKE_WINDOW_MS, 0, /*exitWhenUnplugged=*/false);
  }

  goDeepSleepMinutes(minutesToNextRefresh(g_cfg));
}

void loop() {
  // 流程在 setup 里线性走完，正常情况永远到不了这里（最后一定进深睡）。
  delay(1000);
}
