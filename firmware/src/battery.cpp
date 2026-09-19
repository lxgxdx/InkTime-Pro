#include "battery.h"

#include <Adafruit_MAX1704X.h>
#include <Arduino.h>
#include <Wire.h>
#include <math.h>

#include "board.h"

namespace {
Adafruit_MAX17048 g_gauge;
bool g_gaugeOk = false;
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

  // ⚠️ begin() 返回 true 只代表 I2C 总线起来了，【不代表芯片应答了】——
  //    实测过：芯片没焊好/不在时 begin() 照样 true，串口里却夹着一行
  //    requestFrom(): i2cWriteReadNonStop returned Error -1，然后我们还打印"就绪"。
  //    所以这里多读一次电压做实证：读不出合理值就当没就绪。
  g_gaugeOk = g_gauge.begin(&Wire);
  if (g_gaugeOk) {
    const float v = g_gauge.cellVoltage();
    if (isnan(v) || v < 1.0f || v > 5.0f) {
      Serial.printf("[电量] MAX17048 begin() 成功但读不到电压（读到 %s），当做没就绪\n",
                    isnan(v) ? "NaN" : "离谱值");
      Serial.println("[电量] → 查 U7 焊接、I2C（IO8=SCL / IO9=SDA）、以及电池有没有接");
      g_gaugeOk = false;
    }
  }
  if (!g_gaugeOk) {
    Serial.println("[电量] MAX17048 没应答（没装电池或 I2C 不通），电量将显示 --");
  } else {
    Serial.println("[电量] MAX17048 就绪");
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
