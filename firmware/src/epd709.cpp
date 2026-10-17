#include "epd709.h"

#include <Arduino.h>
#include <SPI.h>

#include "board.h"

// ============================================================================
// 7.09" GDEB0709E01 / GDEP133C02，移植自 FrameFilm 的 hal_epd_709.c
// （GPL-3.0-or-later，作者 Kiritro）
//
// 协议要点（和 GxEPD2 那套完全不同，别按老习惯改）：
//   · CS 拉低的第一个字节 = 命令，之后的字节 = 数据；没有 DC 线
//   · 双片选：一片管左半屏，另一片管右半屏
//   · 4bpp：每字节 2 像素，高半字节在前；整屏一行 1200px = 600 字节
//   · BUSY 低 = 忙
//
// ⚠️ CS_M / CS_S 谁管左/右**还没实测**。这里先按 CS_M=左、CS_S=右。
//    如果色带测试发现左右两半对调了，把下面两个引脚对调即可（一行改动）。
// ============================================================================

namespace {

constexpr int E709_W    = 1200;
constexpr int E709_H    = 1600;
constexpr int E709_HALF_W     = E709_W / 2;    // 600 像素/半屏
constexpr int E709_ROW_BYTES  = E709_HALF_W / 2;     // 300 字节/半屏/行（4bpp）
constexpr uint32_t E709_TOTAL_BYTES = (uint32_t)E709_W * E709_H / 4;  // 480000

// 首测先跑 4MHz（参考工程用 10MHz）—— 先把链路跑通，再往上提
constexpr uint32_t SPI_HZ = 4000000;

// 709 屏的 4bit 颜色码（和老 7.3" 屏完全一致）
constexpr uint8_t C_BLACK  = 0x00;
constexpr uint8_t C_WHITE  = 0x01;
constexpr uint8_t C_YELLOW = 0x02;
constexpr uint8_t C_RED    = 0x03;
constexpr uint8_t C_BLUE   = 0x05;
constexpr uint8_t C_GREEN  = 0x06;

// 命令
constexpr uint8_t PSR             = 0x00;
constexpr uint8_t PWR             = 0x01;
constexpr uint8_t POF             = 0x02;
constexpr uint8_t PON             = 0x04;
constexpr uint8_t BTST_N          = 0x05;
constexpr uint8_t BTST_P          = 0x06;
constexpr uint8_t DTM             = 0x10;
constexpr uint8_t DRF             = 0x12;
constexpr uint8_t CDI             = 0x50;
constexpr uint8_t TCON            = 0x60;
constexpr uint8_t TRES            = 0x61;
constexpr uint8_t AN_TM           = 0x74;
constexpr uint8_t AGID            = 0x86;
constexpr uint8_t BUCK_BOOST_VDDN = 0xB0;
constexpr uint8_t TFT_VCOM_POWER  = 0xB1;
constexpr uint8_t EN_BUF          = 0xB6;
constexpr uint8_t BOOST_VDDP_EN   = 0xB7;
constexpr uint8_t CCSET           = 0xE0;
constexpr uint8_t PWS             = 0xE3;
constexpr uint8_t CMD66           = 0xF0;

// 初始化参数（来自 GDEP133C02 官方驱动）
const uint8_t PSR_V[2]             = {0xDF, 0x69};
const uint8_t PWR_V[6]             = {0x0F, 0x00, 0x28, 0x2C, 0x28, 0x38};
const uint8_t POF_V[1]             = {0x00};
const uint8_t DRF_V[1]             = {0x01};
const uint8_t CDI_V[1]             = {0xF7};
const uint8_t TCON_V[2]            = {0x03, 0x03};
const uint8_t TRES_V[4]            = {0x04, 0xB0, 0x03, 0x20};
const uint8_t CMD66_V[6]           = {0x49, 0x55, 0x13, 0x5D, 0x05, 0x10};
const uint8_t EN_BUF_V[1]          = {0x07};
const uint8_t CCSET_V[1]           = {0x01};
const uint8_t PWS_V[1]             = {0x22};
const uint8_t AN_TM_V[9]           = {0xC0, 0x1E, 0x1E, 0xCE, 0xCE, 0xCE, 0x15, 0x15, 0x55};
const uint8_t AGID_V[1]            = {0x10};
const uint8_t BTST_P_V[2]          = {0xE8, 0x28};
const uint8_t BOOST_VDDP_EN_V[1]   = {0x01};
const uint8_t BTST_N_V[2]          = {0xE8, 0x28};
const uint8_t BUCK_BOOST_VDDN_V[1] = {0x01};
const uint8_t TFT_VCOM_POWER_V[1]  = {0x02};

enum CS { CS_LEFT, CS_RIGHT, CS_BOTH };

inline void csLeft(bool on)  { digitalWrite(PIN_EPD_CS_M, on ? LOW : HIGH); }
inline void csRight(bool on) { digitalWrite(PIN_EPD_CS_S, on ? LOW : HIGH); }

void xferBegin(CS cs) {
  if (cs == CS_LEFT || cs == CS_BOTH) csLeft(true);
  if (cs == CS_RIGHT || cs == CS_BOTH) csRight(true);
  SPI.beginTransaction(SPISettings(SPI_HZ, MSBFIRST, SPI_MODE0));
}

void xferEnd() {
  SPI.endTransaction();
  csLeft(false);
  csRight(false);
}

void sendCmd(uint8_t c) { SPI.transfer(c); }
void sendData(const uint8_t* d, size_t n) { SPI.writeBytes(d, n); }

void cmdData(CS cs, uint8_t cmd, const uint8_t* d, size_t n) {
  xferBegin(cs);
  sendCmd(cmd);
  if (n) sendData(d, n);
  xferEnd();
}

void cmdOnly(CS cs, uint8_t cmd) {
  xferBegin(cs);
  sendCmd(cmd);
  xferEnd();
}

// 等 BUSY 释放。加超时 —— 第一次上板时 BUSY 若没接好，不能把整机卡死在这里。
void waitBusy(const char* tag) {
  const uint32_t t0 = millis();
  while (digitalRead(PIN_EPD_BUSY) == LOW) {
    if (millis() - t0 > 120000UL) {
      Serial.printf("[709] !! 等 BUSY 超时（%s），继续往下走\n", tag);
      return;
    }
    delay(1);
  }
}

void initSequence() {
  // 下面这些「只发左片选」的是 GDEP133C02 官方驱动的写法，不是笔误
  cmdData(CS_LEFT, AN_TM, AN_TM_V, sizeof(AN_TM_V));
  cmdData(CS_BOTH, CMD66, CMD66_V, sizeof(CMD66_V));
  cmdData(CS_BOTH, PSR, PSR_V, sizeof(PSR_V));
  cmdData(CS_BOTH, CDI, CDI_V, sizeof(CDI_V));
  cmdData(CS_BOTH, TCON, TCON_V, sizeof(TCON_V));
  cmdData(CS_BOTH, AGID, AGID_V, sizeof(AGID_V));
  cmdData(CS_BOTH, PWS, PWS_V, sizeof(PWS_V));
  cmdData(CS_BOTH, CCSET, CCSET_V, sizeof(CCSET_V));
  cmdData(CS_BOTH, TRES, TRES_V, sizeof(TRES_V));
  cmdData(CS_LEFT, PWR, PWR_V, sizeof(PWR_V));
  cmdData(CS_LEFT, EN_BUF, EN_BUF_V, sizeof(EN_BUF_V));
  cmdData(CS_LEFT, BTST_P, BTST_P_V, sizeof(BTST_P_V));
  cmdData(CS_LEFT, BOOST_VDDP_EN, BOOST_VDDP_EN_V, sizeof(BOOST_VDDP_EN_V));
  cmdData(CS_LEFT, BTST_N, BTST_N_V, sizeof(BTST_N_V));
  cmdData(CS_LEFT, BUCK_BOOST_VDDN, BUCK_BOOST_VDDN_V, sizeof(BUCK_BOOST_VDDN_V));
  cmdData(CS_LEFT, TFT_VCOM_POWER, TFT_VCOM_POWER_V, sizeof(TFT_VCOM_POWER_V));
}

void refresh() {
  Serial.println("[709] PON…");
  cmdOnly(CS_BOTH, PON);
  waitBusy("PON");

  Serial.println("[709] DRF（面板刷新，预计 30~60 秒）…");
  const uint32_t t0 = millis();
  cmdData(CS_BOTH, DRF, DRF_V, sizeof(DRF_V));
  waitBusy("DRF");
  Serial.printf("[709] 刷新完成，用时 %.1f 秒\n", (millis() - t0) / 1000.0);

  cmdData(CS_BOTH, POF, POF_V, sizeof(POF_V));
  waitBusy("POF");
}

// 把一行 600 像素（4bpp，高低半字节）发出去
void sendRow(const uint8_t* row) { sendData(row, E709_ROW_BYTES); }

}  // namespace

void epd709Begin() {
  // 引脚：SPI 三线共用，DC 按手册接低（本协议不用它），两个片选手动控制
  pinMode(PIN_EPD_CS_M, OUTPUT);
  pinMode(PIN_EPD_CS_S, OUTPUT);
  pinMode(PIN_EPD_RST, OUTPUT);
  pinMode(PIN_EPD_DC, OUTPUT);
  pinMode(PIN_EPD_BUSY, INPUT);

  csLeft(false);
  csRight(false);
  digitalWrite(PIN_EPD_DC, LOW);   // 手册：4 线模式下 D_CX 接 GND

  SPI.begin(PIN_EPD_SCLK, -1 /* 不回读 */, PIN_EPD_DIN, -1 /* CS 手动 */);

  // 复位：低 20ms → 高 20ms → 等 BUSY
  digitalWrite(PIN_EPD_RST, LOW);
  delay(20);
  digitalWrite(PIN_EPD_RST, HIGH);
  delay(20);
  waitBusy("RST");

  initSequence();

  Serial.println("[709] 初始化序列完成（1200x1600 双片选）");
}

void epd709ColorBars() {
  const uint8_t codes[6] = {C_BLACK, C_WHITE, C_YELLOW, C_RED, C_BLUE, C_GREEN};

  // 左半屏：全局 x = 0..599，色条 0/1/2
  xferBegin(CS_LEFT);
  sendCmd(DTM);
  for (int y = 0; y < E709_H; ++y) {
    uint8_t row[E709_ROW_BYTES];
    for (int x = 0; x < E709_HALF_W; ++x) {
      const uint8_t c = codes[x / 200];
      if ((x & 1) == 0) row[x >> 1] = (uint8_t)(c << 4);
      else              row[x >> 1] |= c;
    }
    sendRow(row);
  }
  xferEnd();

  // 右半屏：全局 x = 600..1199，色条 3/4/5
  xferBegin(CS_RIGHT);
  sendCmd(DTM);
  for (int y = 0; y < E709_H; ++y) {
    uint8_t row[E709_ROW_BYTES];
    for (int x = 0; x < E709_HALF_W; ++x) {
      const uint8_t c = codes[3 + x / 200];
      if ((x & 1) == 0) row[x >> 1] = (uint8_t)(c << 4);
      else              row[x >> 1] |= c;
    }
    sendRow(row);
  }
  xferEnd();

  refresh();
}

void epd709Solid(uint8_t paletteIndex) {
  static const uint8_t codes[6] = {C_BLACK, C_WHITE, C_YELLOW, C_RED, C_BLUE, C_GREEN};
  const uint8_t code = codes[paletteIndex < 6 ? paletteIndex : 0];

  // 两个片选同时拉低，各面板各收 480000 字节
  xferBegin(CS_BOTH);
  sendCmd(DTM);
  uint8_t buf[512];
  memset(buf, code, sizeof(buf));
  uint32_t left = E709_TOTAL_BYTES;
  while (left) {
    const uint32_t n = left > sizeof(buf) ? (uint32_t)sizeof(buf) : left;
    sendData(buf, n);
    left -= n;
  }
  xferEnd();

  refresh();
}

void epd709ShowBuffer(const uint8_t* fb, int w, int h) {
  // 服务器给的是 1 字节/像素（board.h 的 Palette 索引）。
  //   服务器调色板顺序：0黑 1白 2红 3黄 4蓝 5绿
  //   709 面板码：      0x00  0x01  0x03  0x02  0x05  0x06
  static const uint8_t kServerTo709[6] = {0x00, 0x01, 0x03, 0x02, 0x05, 0x06};

  // 1200×1600 原生直发；1600×1200 顺时针转 90° 再发
  const bool rot = (w == E709_H && h == E709_W);
  const int dstW = rot ? h : w;
  const int dstH = rot ? w : h;
  if (dstW != E709_W || dstH != E709_H) {
    Serial.printf("[709] 画布 %dx%d 不支持（只支持 1200x1600 / 1600x1200）\n", w, h);
    return;
  }

  const uint32_t t0 = millis();
  for (int half = 0; half < 2; ++half) {
    xferBegin(half == 0 ? CS_LEFT : CS_RIGHT);
    sendCmd(DTM);
    for (int dy = 0; dy < E709_H; ++dy) {
      uint8_t row[E709_ROW_BYTES];
      for (int lx = 0; lx < E709_HALF_W; ++lx) {
        const int dx = half * E709_HALF_W + lx;
        const int sx = rot ? dy : dx;
        const int sy = rot ? (h - 1 - dx) : dy;
        const uint8_t idx = fb[(size_t)sy * (size_t)w + (size_t)sx];
        const uint8_t c = (idx < 6) ? kServerTo709[idx] : kServerTo709[1];  // 越界当白
        if ((lx & 1) == 0) row[lx >> 1] = (uint8_t)(c << 4);
        else               row[lx >> 1] |= c;
      }
      sendRow(row);
    }
    xferEnd();
    Serial.printf("[709] 第 %d 半屏数据发送完成（%.1f 秒）\n", half + 1,
                  (millis() - t0) / 1000.0);
  }

  refresh();
}

void epd709Sleep() {
  waitBusy("SLEEP");
  cmdData(CS_BOTH, POF, POF_V, sizeof(POF_V));
  waitBusy("SLEEP-POF");
}
