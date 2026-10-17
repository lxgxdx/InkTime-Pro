#include "board.h"

#include <Arduino.h>
#include "esp_heap_caps.h"

void releaseStrappingPins() {
  // IO45 是 ESP32-S3 的 strapping 脚，上电时被采样来决定 VDD_SPI 是 3.3V 还是 1.8V。
  // 本模块用 3.3V flash，所以它必须是低 —— 空置时芯片内部弱下拉正好给低电平。
  //
  // 但固件为了读 SW2 会给 IO45 开内部上拉（=高）。若此时 ESP.restart()，有把
  // VDD_SPI 重新锁存成 1.8V 的风险，那块 flash 就跑不动了。所以任何重启之前
  // 都要先把上拉撤掉，让内部弱下拉重新接管。
  pinMode(PIN_BTN_NEXT, INPUT);   // 不带上拉 = 弱下拉生效

  // IO46（屏 CS）也是 strapping 脚，但要的是「低 + IO0 高 = SPI Boot」。
  // 屏空闲时 CS 本就该是高，这里特意不去动它：复位后 GPIO 全部回到高阻输入，
  // 由内部弱下拉接管，正是启动需要的电平。
}

void boardPanelPowerOn() {
  // v2 的屏供电开关：IO12 高 → Q2 拉低 Q1 栅极 → Q1(AO3401A) 导通 → 升压有电。
  //
  // ⚠️ 这一句是 v2 相对 v1 唯一"不做就完全没画面"的软件改动。复位后 GPIO 是高阻，
  //    板上的 R19(100k) 会把它拉到低 —— 默认就是"断电"，所以必须显式拉高。
  pinMode(PIN_EPD_EN, OUTPUT);
  digitalWrite(PIN_EPD_EN, HIGH);

  // 等 MT3608 起振 + 转接板自己那颗高压升压稳住。正常流程里 setup 还要跑好几百
  // 毫秒（诊断、连 WiFi）才碰屏，这点延时只是兜底，防止将来有人把调用提到最前面。
  delay(50);
}

void* psramAlloc(size_t n) {
  void* p = heap_caps_malloc(n, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!p) {
    Serial.printf("[内存] PSRAM 分配 %u 字节失败，退回内部 RAM\n", (unsigned)n);
    p = heap_caps_malloc(n, MALLOC_CAP_8BIT);
  }
  if (!p) {
    Serial.printf("[内存] 严重：%u 字节分配彻底失败\n", (unsigned)n);
  }
  return p;
}
