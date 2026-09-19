#pragma once

#include <stdint.h>

// 初始化 SPI 与面板（含硬件复位）。可重复调用 —— 面板从 deep sleep 唤醒后
// GxEPD2 会自己重新走一遍复位时序。
void epdBegin();

// 把 w×h、每像素 1 字节（值为 board.h 的 Palette 枚举）的缓冲推到屏上并全刷。
//
// w/h 必须是 480×800 或 800×480 之一，内部据此自动选旋转方向。
// 全刷约 40 秒（Spectra 6 的固有速度），期间屏会明显闪烁变色，属正常现象。
// 返回后面板已进 deep sleep。
void epdShowBuffer(const uint8_t* fb, int w, int h);

// 只让面板休眠、不重画（例如本次没拿到新图时）。
void epdHibernate();

// 自检用：调色板索引（0..5）→ 真正发给面板的原始颜色码 / 对应的中文色名。
// 暴露出来是为了让自检日志里能打出"这一条是红、发的是 0x03"，
// 出颜色问题时能一眼看出是映射表错了还是面板不对。
uint8_t epdPanelCode(int paletteIndex);
const char* epdPaletteName(int paletteIndex);
