#include "overlay.h"

#include <stdio.h>
#include <string.h>

#include "board.h"
#include "digits.h"

namespace {

// 电池外形尺寸（px）。固定值，不随画布缩放 —— 配合固定的 5×8 字模。
constexpr int BODY_W = 28;
constexpr int BODY_H = 14;
constexpr int BODY_BORDER = 2;
constexpr int CAP_W = 3;      // 右侧正极凸点
constexpr int CAP_H = 8;
constexpr int GAP_TO_TEXT = 4;

// 充电闪电 7×8，bit6 = 最左
constexpr int BOLT_W = 7;
constexpr int BOLT_H = 8;
constexpr uint8_t kBolt[BOLT_H] = {
    0x18,  // ...##..
    0x30,  // ..##...
    0x60,  // .##....
    0x7F,  // #######
    0x18,
    0x30,
    0x60,
    0x00,
};

inline void put(uint8_t* fb, int cw, int ch, int x, int y, uint8_t color) {
  if (x < 0 || y < 0 || x >= cw || y >= ch) return;
  fb[(size_t)y * (size_t)cw + (size_t)x] = color;
}

void fillRect(uint8_t* fb, int cw, int ch, int x, int y, int w, int h, uint8_t color) {
  for (int j = 0; j < h; ++j)
    for (int i = 0; i < w; ++i) put(fb, cw, ch, x + i, y + j, color);
}

void rectOutline(uint8_t* fb, int cw, int ch, int x, int y, int w, int h, int t,
                 uint8_t color) {
  fillRect(fb, cw, ch, x, y, w, t, color);
  fillRect(fb, cw, ch, x, y + h - t, w, t, color);
  fillRect(fb, cw, ch, x, y, t, h, color);
  fillRect(fb, cw, ch, x + w - t, y, t, h, color);
}

int drawGlyph(uint8_t* fb, int cw, int ch, int x, int y, char c, uint8_t color) {
  const int idx = glyphIndex(c);
  if (idx < 0) return GLYPH_ADVANCE;
  for (int r = 0; r < GLYPH_H; ++r) {
    const uint8_t bits = kGlyphs5x8[idx][r];
    for (int b = 0; b < GLYPH_W; ++b) {
      if (bits & (1 << (GLYPH_W - 1 - b))) put(fb, cw, ch, x + b, y + r, color);
    }
  }
  return GLYPH_ADVANCE;
}

int imin(int a, int b) { return a < b ? a : b; }

}  // namespace

void drawBatteryOverlay(uint8_t* fb, int cw, int ch, int slotX, int slotY, int slotW,
                        int slotH, const BatteryState& st) {
  // 先把槽底刷白。服务器渲染时已经留白了，这里再刷一遍是为了【重绘】时盖掉
  // 上一次画的图标 —— 否则 82% 改成 79% 时两位数字会叠在一起。
  fillRect(fb, cw, ch, slotX, slotY, slotW, slotH, PAL_WHITE);

  const int pct = st.valid ? imin(100, st.percent < 0 ? 0 : st.percent) : 0;

  char txt[8];
  if (st.valid) snprintf(txt, sizeof(txt), "%d%%", pct);
  else          snprintf(txt, sizeof(txt), "--");

  // 整组水平居中：电池 + 凸点 + 空档 + 数字
  const int textW = glyphTextWidth(txt);
  const int groupW = BODY_W + CAP_W + GAP_TO_TEXT + textW;
  int gx = slotX + (slotW - groupW) / 2;
  if (gx < slotX) gx = slotX;
  const int gy = slotY + (slotH - BODY_H) / 2;

  // --- 电池外壳 + 正极 ---
  rectOutline(fb, cw, ch, gx, gy, BODY_W, BODY_H, BODY_BORDER, PAL_BLACK);
  fillRect(fb, cw, ch, gx + BODY_W, gy + (BODY_H - CAP_H) / 2, CAP_W, CAP_H, PAL_BLACK);

  // --- 填充条：低于阈值变红，否则绿 ---
  const int innerX = gx + BODY_BORDER;
  const int innerY = gy + BODY_BORDER;
  const int innerW = BODY_W - 2 * BODY_BORDER;
  const int innerH = BODY_H - 2 * BODY_BORDER;
  const uint8_t fillColor = (pct < BATT_LOW_PERCENT) ? PAL_RED : PAL_GREEN;
  const int barW = st.valid ? (innerW * pct) / 100 : 0;
  if (barW > 0) fillRect(fb, cw, ch, innerX, innerY, barW, innerH, fillColor);

  // --- 充电中：电池内部叠一个黑色闪电 ---
  // 画在填充条之上：红/绿底上黑色都看得清，空电池时白底更看得清。
  if (st.charging) {
    const int bx = innerX + (innerW - BOLT_W) / 2;
    const int by = innerY + (innerH - BOLT_H) / 2;
    for (int r = 0; r < BOLT_H; ++r)
      for (int b = 0; b < BOLT_W; ++b)
        if (kBolt[r] & (1 << (BOLT_W - 1 - b))) put(fb, cw, ch, bx + b, by + r, PAL_BLACK);
  }

  // --- 百分比数字 ---
  int tx = gx + BODY_W + CAP_W + GAP_TO_TEXT;
  const int ty = gy + (BODY_H - GLYPH_H) / 2;
  for (const char* p = txt; *p; ++p) tx += drawGlyph(fb, cw, ch, tx, ty, *p, PAL_BLACK);
}
