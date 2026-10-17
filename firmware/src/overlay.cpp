#include "overlay.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "board.h"
#include "digits.h"

namespace {

// 电池图标的高度 = 服务器给的槽高（= 日期/地点那一行**数字的实际字形高**，不是字号），
// 宽 = 2×高；里面的百分比数字也用同样的高度。
// 里面所有细节都按高度 d 等比推导，和 render_daily_photo.py 的
// _battery_group_width() 是同一套公式 —— 两边改一个，另一个必须跟着改。
constexpr int GROUP_CHARS = 4;      // 按 "100%" 的最宽情况占位

inline int imin(int a, int b) { return a < b ? a : b; }
inline int imax(int a, int b) { return a > b ? a : b; }
inline int iround(double v) { return (int)lround(v); }

struct Dims {
  int d;        // 高度
  int bodyW, bodyH, border;
  int capW, capH;
  int gap;
  int glyphW, glyphH, adv;
};

// d = 电池外壳高（服务器量的**中文字形高**）
// gh = 百分比数字的字形高（服务器量的**数字字形高**，比中文矮约 20%）
Dims dimsFor(int d, int gh) {
  Dims m;
  m.d = d;
  m.bodyW = 2 * d;
  m.bodyH = d;
  m.border = imax(1, iround(d / 7.0));
  m.capW = imax(1, iround(d * 3.0 / 14.0));
  m.capH = imax(2, iround(d * 8.0 / 14.0));
  m.gap = imax(2, iround(d * 4.0 / 14.0));
  m.glyphH = (gh > 0 && gh <= d) ? gh : d;
  m.glyphW = imax(3, iround(m.glyphH * 5.0 / 8.0));
  m.adv = m.glyphW + imax(1, iround(m.glyphW / 5.0));
  return m;
}

int groupWidth(const Dims& m) {
  return m.bodyW + m.capW + m.gap + GROUP_CHARS * m.adv;
}

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

// 把 5×8 点阵按 gw×gh 最近邻放大（非整数倍也能画，笔画会略有粗细差异，
// 但电量数字本来就小，不做抗锯齿反而更清楚）。
void drawGlyphScaled(uint8_t* fb, int cw, int ch, int x, int y, char c, uint8_t color,
                     int gw, int gh) {
  const int idx = glyphIndex(c);
  if (idx < 0) return;
  for (int r = 0; r < gh; ++r) {
    const int sr = r * GLYPH_H / gh;
    const uint8_t bits = kGlyphs5x8[idx][sr];
    for (int b = 0; b < gw; ++b) {
      const int sb = b * GLYPH_W / gw;
      if (bits & (1 << (GLYPH_W - 1 - sb))) put(fb, cw, ch, x + b, y + r, color);
    }
  }
}

// 充电闪电：7×8 的原图，按需要的大小最近邻放大
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

void drawBoltScaled(uint8_t* fb, int cw, int ch, int x, int y, int w, int h,
                    uint8_t color) {
  for (int r = 0; r < h; ++r) {
    const int sr = r * BOLT_H / h;
    for (int b = 0; b < w; ++b) {
      const int sb = b * BOLT_W / w;
      if (kBolt[sr] & (1 << (BOLT_W - 1 - sb))) put(fb, cw, ch, x + b, y + r, color);
    }
  }
}

}  // namespace

void drawBatteryOverlay(uint8_t* fb, int cw, int ch, int slotX, int slotY, int slotW,
                        int slotH, int slotScale, int slotGlyphH,
                        const BatteryState& st) {
  // 先把槽底刷白。服务器渲染时已经留白了，这里再刷一遍是为了【重绘】时盖掉
  // 上一次画的图标 —— 否则 82% 改成 79% 时两位数字会叠在一起。
  fillRect(fb, cw, ch, slotX, slotY, slotW, slotH, PAL_WHITE);

  // 高度以槽高为准；槽高不合理（老服务器）时退回 slotScale × 14。
  int d = (slotH >= 8) ? slotH : 14 * imax(1, slotScale);

  // 老服务器给的槽可能很窄（72px），按新几何会撑出去 —— 缩到刚好放得下。
  // 这样"先刷固件、后更新服务器"也不会把地点文字盖掉。
  if (slotW > 0) {
    while (d > 6 && groupWidth(dimsFor(d, slotGlyphH)) > slotW) d--;
  }
  const Dims m = dimsFor(d, slotGlyphH);

  const int pct = st.valid ? imin(100, st.percent < 0 ? 0 : st.percent) : 0;

  char txt[8];
  if (st.valid) snprintf(txt, sizeof(txt), "%d%%", pct);
  else          snprintf(txt, sizeof(txt), "--");

  const int n = (int)strlen(txt);
  const int textW = n * m.adv;
  const int groupW = m.bodyW + m.capW + m.gap + textW;

  // 右对齐：图标右边缘贴齐槽的右边缘（= 页面右边距），看着才"靠右"。
  int gx = slotX + slotW - groupW;
  if (gx < slotX) gx = slotX;
  const int gy = slotY + (slotH - m.bodyH) / 2;

  // --- 电池外壳 + 正极 ---
  rectOutline(fb, cw, ch, gx, gy, m.bodyW, m.bodyH, m.border, PAL_BLACK);
  fillRect(fb, cw, ch, gx + m.bodyW, gy + (m.bodyH - m.capH) / 2, m.capW, m.capH,
           PAL_BLACK);

  // --- 填充条：低于阈值变红，否则绿 ---
  const int innerX = gx + m.border;
  const int innerY = gy + m.border;
  const int innerW = m.bodyW - 2 * m.border;
  const int innerH = m.bodyH - 2 * m.border;
  const uint8_t fillColor = (pct < BATT_LOW_PERCENT) ? PAL_RED : PAL_GREEN;
  const int barW = st.valid ? (innerW * pct) / 100 : 0;
  if (barW > 0) fillRect(fb, cw, ch, innerX, innerY, barW, innerH, fillColor);

  // --- 充电中：电池内部叠一个黑色闪电 ---
  if (st.charging && innerW > 4 && innerH > 4) {
    int boltH = imax(3, imin(innerH, iround(m.bodyH * 0.78)));
    int boltW = imax(2, iround(boltH * 7.0 / 8.0));
    if (boltW > innerW) {
      boltW = innerW;
      boltH = imax(2, iround(boltW * 8.0 / 7.0));
    }
    drawBoltScaled(fb, cw, ch, innerX + (innerW - boltW) / 2,
                   innerY + (innerH - boltH) / 2, boltW, boltH, PAL_BLACK);
  }

  // --- 百分比数字 ---
  int tx = gx + m.bodyW + m.capW + m.gap;
  const int ty = gy + (m.bodyH - m.glyphH) / 2;
  for (const char* p = txt; *p; ++p) {
    drawGlyphScaled(fb, cw, ch, tx, ty, *p, PAL_BLACK, m.glyphW, m.glyphH);
    tx += m.adv;
  }
}
