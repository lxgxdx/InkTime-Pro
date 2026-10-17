#include "battery.h"

#include <Adafruit_MAX1704X.h>
#include <Arduino.h>
#include <Wire.h>
#include <math.h>

#include "board.h"

namespace {
Adafruit_MAX17048 g_gauge;
bool g_gaugeOk = false;

bool voltageSane(float v) { return !isnan(v) && v >= 1.0f && v <= 5.0f; }

// 扫描 I2C 总线，把应答的地址打出来。诊断"电量计不应答"时先看这个：
//   空白      → 总线上根本没器件（U7 没焊好 / 没供电 / SCL·SDA 没连到 U7）
//   0x36      → MAX17048 在
//   其它地址  → 芯片型号不对（买到错型号/翻新片）
void i2cScan() {
  Serial.println("[I2C] 扫描 0x01~0x7F …");
  int found = 0;
  for (uint8_t a = 1; a < 0x7F; ++a) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      Serial.printf("[I2C]   应答 0x%02X%s\n", a, (a == 0x36) ? "   ← MAX17048" : "");
      ++found;
    }
  }
  Serial.printf("[I2C] 共 %d 个器件\n", found);
  if (found == 0) {
    Serial.println("[I2C] → 一个都没有：U7 没焊好/没供电，或 SCL/SDA 没连到 U7");
  }
}
}  // namespace

// 位号备忘（排查时别认错）：
//   U2 = MIC5219-3.3 LDO（+BATT → +3V3）    U3 = TP4056 充电
//   U4 = DW01A 保护   U7 = MAX17048 电量计   U8 = MT3608 升压
bool batteryBegin() {
  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);

  // TP4056 的状态脚是开漏输出 + 板上有外部上拉（R5/R6），所以这里用普通 INPUT：
  // 再开内部上拉只会和外部上拉并联，白费电还改变不了读数。
  pinMode(PIN_CHRG, INPUT);
  pinMode(PIN_STDBY, INPUT);

  // begin() 内部做三件事：地址探测（0x36 是否 ACK）、读 IC 版本、软复位。
  // 返回 true 说明芯片真的应答了。
  g_gaugeOk = g_gauge.begin(&Wire);
  if (!g_gaugeOk) {
    Serial.println("[电量] 100kHz 下地址 0x36 没应答 → 扫一下总线");
    i2cScan();
  }

  // 不管 begin() 成没成，都从高到低试一遍读电压，找出【最高能跑通】的时钟。
  // 这是判断总线余量最直接的办法：
  //   4.7k 上拉（R12/R13）+ 短走线 → 400kHz 都该轻松跑通
  //   只有 10kHz 能跑            → 总线有漏电 / 虚焊 / 寄生电容
  static const uint32_t kRates[] = {400000, 200000, 100000, 50000, 25000, 10000};
  float v = NAN;
  uint32_t maxRate = 0;
  for (uint32_t r : kRates) {
    Wire.setClock(r);
    const float got = g_gauge.cellVoltage();
    if (voltageSane(got)) {
      Serial.printf("[电量]   %6lu Hz → 正常 %.3fV\n", (unsigned long)r, got);
      v = got;
      maxRate = r;
      break;
    }
    Serial.printf("[电量]   %6lu Hz → 失败\n", (unsigned long)r);
  }

  if (maxRate == 0) {
    g_gaugeOk = false;
    Serial.println("[电量] 全档位都读不到电压 → 不是速率问题");
    i2cScan();
  } else {
    g_gaugeOk = true;
    // 报告能跑的最高档，但【实际运行封顶 100kHz】—— 一次性读成功不等于长期可靠
    const uint32_t runRate = (maxRate > 100000) ? 100000 : maxRate;
    Wire.setClock(runRate);
    Serial.printf("[电量] MAX17048 就绪：最高可跑 %lu Hz，实际用 %lu Hz，当前 %.2fV\n",
                  (unsigned long)maxRate, (unsigned long)runRate, v);
    if (maxRate < 100000) {
      Serial.println("[电量] ⚠ 连 100kHz 都跑不动 —— 4.7k 上拉正常时 400kHz 都没问题，");
      Serial.println("[电量]    说明总线有漏电/虚焊/寄生电容（先洗板，再复查 R12/R13）");
    }
  }
  return g_gaugeOk;
}

BatteryState batteryRead() {
  BatteryState st{};
  // 开漏 + 外部上拉：低 = 有效
  st.charging = (digitalRead(PIN_CHRG) == LOW);
  st.full     = (digitalRead(PIN_STDBY) == LOW);
  st.valid    = false;
  st.percent  = 0;
  st.voltage  = 0.0f;

  if (!g_gaugeOk) return st;

  const float pct = g_gauge.cellPercent();
  const float v   = g_gauge.cellVoltage();
  // 空焊盘/没接电池时 MAX17048 会给出 NaN 或离谱的电压，别把它当成 0% 显示
  if (isnan(pct) || isnan(v) || v < 1.0f || v > 5.0f) {
    return st;
  }

  st.valid   = true;
  st.percent = (int)lroundf(pct);
  if (st.percent < 0)   st.percent = 0;
  if (st.percent > 100) st.percent = 100;
  st.voltage = v;
  return st;
}
