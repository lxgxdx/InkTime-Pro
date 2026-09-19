#include "selftest.h"

#include <Arduino.h>
#include <SPI.h>

#include "board.h"
#include "epd_display.h"

namespace {

// ---------- 低层：手工收发一个字节（DC/CS 自己控，不经 GxEPD2）----------
void rawCmd(uint8_t c) {
  digitalWrite(PIN_EPD_DC, LOW);
  digitalWrite(PIN_EPD_CS, LOW);
  delayMicroseconds(2);
  SPI.transfer(c);
  digitalWrite(PIN_EPD_CS, HIGH);
  delayMicroseconds(2);
}
void rawData(uint8_t d) {
  digitalWrite(PIN_EPD_DC, HIGH);
  digitalWrite(PIN_EPD_CS, LOW);
  delayMicroseconds(2);
  SPI.transfer(d);
  digitalWrite(PIN_EPD_CS, HIGH);
  delayMicroseconds(2);
}

// ---------- 判决性探针：屏到底收不收得到命令 ----------
//
// 绕开 GxEPD2 的整套驱动，用最原始的 SPI 直接跟屏对话。依据是规格书 Note 5-4：
// 「屏开始干活时 BUSY_N 会被拉低」（输出波形 / 写 OTP / 读温度传感器）。
// 所以发一条 0x04 (POWER_ON) 出去，盯 5 秒：
//   · BUSY 掉下去过 → 屏活着、命令通路是通的 → 问题在初始化参数，换驱动即可
//   · BUSY 纹丝不动 → 命令根本没到屏，或者屏已经坏了
//
// 初始化序列逐字节抄自厂商 Waveshare 的 epd7in3.h（EPD_7in3E_init），
// 和 GxEPD2 那份只有 3 处调参不同 —— 顺带也验证了"是不是 GxEPD2 的参数不对"。
static void probePanelCommands() {
  Serial.println("[探针] 绕开驱动，直接给屏发命令，看 BUSY 会不会动…");

  pinMode(PIN_EPD_CS, OUTPUT);   digitalWrite(PIN_EPD_CS, HIGH);
  pinMode(PIN_EPD_DC, OUTPUT);   digitalWrite(PIN_EPD_DC, HIGH);
  pinMode(PIN_EPD_RST, OUTPUT);  digitalWrite(PIN_EPD_RST, HIGH);
  pinMode(PIN_EPD_BUSY, INPUT);

  SPI.begin(PIN_EPD_SCLK, -1, PIN_EPD_DIN, PIN_EPD_CS);
  SPI.beginTransaction(SPISettings(2000000, MSBFIRST, SPI_MODE0));

  digitalWrite(PIN_EPD_RST, LOW);  delay(20);    // 硬复位
  digitalWrite(PIN_EPD_RST, HIGH); delay(120);
  Serial.printf("[探针] 复位后 BUSY=%d（1=空闲，正常）\n", digitalRead(PIN_EPD_BUSY));

  rawCmd(0xAA); rawData(0x49); rawData(0x55); rawData(0x20);
                rawData(0x08); rawData(0x09); rawData(0x18);
  rawCmd(0x01); rawData(0x3F);
  rawCmd(0x00); rawData(0x5F); rawData(0x69);
  rawCmd(0x03); rawData(0x00); rawData(0x54); rawData(0x00); rawData(0x44);
  rawCmd(0x05); rawData(0x40); rawData(0x1F); rawData(0x1F); rawData(0x2C);
  rawCmd(0x06); rawData(0x6F); rawData(0x1F); rawData(0x17); rawData(0x49);
  rawCmd(0x08); rawData(0x6F); rawData(0x1F); rawData(0x1F); rawData(0x22);
  rawCmd(0x30); rawData(0x03);
  rawCmd(0x50); rawData(0x3F);
  rawCmd(0x60); rawData(0x02); rawData(0x00);
  rawCmd(0x61); rawData(0x03); rawData(0x20); rawData(0x01); rawData(0xE0);
  rawCmd(0x84); rawData(0x01);
  rawCmd(0xE3); rawData(0x2F);
  Serial.println("[探针] 初始化序列已发完");

  rawCmd(0x04);                       // POWER_ON —— 关键就在这一条
  bool sawLow = false;
  const uint32_t t0 = millis();
  while (millis() - t0 < 5000) {
    if (digitalRead(PIN_EPD_BUSY) == LOW) { sawLow = true; break; }
    delay(1);
  }
  Serial.printf("[探针] 发 0x04(POWER_ON) 后 5 秒内 BUSY %s\n",
                sawLow ? "掉到过低电平 → 【屏是活的！】命令通路没问题，是初始化参数的事"
                       : "始终是高 → 屏没接收/没执行，命令通路或屏本身有问题");

  if (sawLow) {                       // 让它忙完再收拾干净，别留在半通电状态
    const uint32_t t1 = millis();
    while (digitalRead(PIN_EPD_BUSY) == LOW && millis() - t1 < 10000) delay(1);
    rawCmd(0x02); rawData(0x00); delay(100);   // POWER_OFF
    rawCmd(0x07); rawData(0xA5);               // DEEP_SLEEP
  }

  SPI.endTransaction();
}

// 探一下 BUSY 脚 —— 这是整块屏唯一一个【屏主动输出给我们】的信号，
// 所以它是判断"屏到底活没活"最直接的依据。
//
// 判法：同一个脚分别用 无上下拉 / 内部上拉 / 内部下拉 各读一次。
//   · 读数跟着我们的上下拉走 → 这根线是浮的，屏没在驱动它
//     （屏没供电、排线没通、或者 BS0/BS1 配错导致屏根本不启动）
//   · 下拉时仍然读到高       → 有东西在把它拉高：要么屏活着在驱动，要么转接板有上拉
//
// 注意它只能证伪不能证实：转接板上若有上拉电阻，"下拉读高"也可能只是那颗电阻。
// 但要读到"浮空"，就一定是屏那边没动静。
void probeBusy() {
  pinMode(PIN_EPD_BUSY, INPUT);
  delay(3);
  const int raw = digitalRead(PIN_EPD_BUSY);
  pinMode(PIN_EPD_BUSY, INPUT_PULLUP);
  delay(3);
  const int pu = digitalRead(PIN_EPD_BUSY);
  pinMode(PIN_EPD_BUSY, INPUT_PULLDOWN);
  delay(3);
  const int pd = digitalRead(PIN_EPD_BUSY);
  pinMode(PIN_EPD_BUSY, INPUT);

  Serial.printf("[探针] BUSY 悬空=%d  内部上拉=%d  内部下拉=%d\n", raw, pu, pd);
  if (pu != pd) {
    Serial.println("[探针] ← 读数跟着我们的上下拉走：BUSY 是浮的，屏没在驱动它。");
    Serial.println("       查：① 转接板 VCC 5V ② 50pin 排线插到底没有 ③ BS0/BS1 跳线");
  } else {
    Serial.println("[探针] ← 上下拉都拉不动它：有人在驱动 BUSY（屏活着，或转接板有上拉）");
  }
}

}  // namespace

void runSelfTest() {
  Serial.println();
  Serial.println("========== 六色自检 ==========");
  probeBusy();
  probePanelCommands();
  Serial.println("[自检] 屏上从左到右应为：黑 / 白 / 红 / 黄 / 蓝 / 绿");
  Serial.println("[自检] 共有 6 条色带，色带之间是白缝。");

  uint8_t* fb = (uint8_t*)psramAlloc(PANEL_BYTES);
  if (!fb) {
    Serial.println("[自检] 中止：帧缓冲分配失败。");
    return;
  }
  memset(fb, PAL_WHITE, PANEL_BYTES);

  // 左右各留一段白边，6 条等宽色带按调色板顺序排开。
  // 顺序必须和 software/config.py 的 SCREENS[].palette 一致，否则说明映射表写反了。
  const int gap = 12;
  const int bar_w = (PANEL_W - gap * (PAL_COUNT + 1)) / PAL_COUNT;
  for (int c = 0; c < PAL_COUNT; ++c) {
    const int x0 = gap + c * (bar_w + gap);
    for (int y = 60; y < PANEL_H - 60; ++y) {
      uint8_t* row = fb + (size_t)y * PANEL_W;
      for (int x = x0; x < x0 + bar_w; ++x) row[x] = (uint8_t)c;
    }
    Serial.printf("[自检] 第 %d 条 x=%d..%d 应为「%s」→ 面板码 0x%02X\n", c + 1, x0,
                  x0 + bar_w - 1, epdPaletteName(c), epdPanelCode(c));
  }

  epdBegin();
  epdShowBuffer(fb, PANEL_W, PANEL_H);   // 内部会打印耗时，见 epd_display.cpp
  free(fb);

  Serial.println("[自检] 完成（面板已休眠）。");
  Serial.println("[自检] 颜色或顺序不对 → 先查引脚与转接板线序，再考虑换厂商驱动。");
  Serial.println("==============================");
}
