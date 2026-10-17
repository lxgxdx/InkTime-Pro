#pragma once

#include <stdint.h>

// 电量计 + 充电状态的一次读数
struct BatteryState {
  int  percent;    // 0..100
  float voltage;   // V
  bool charging;   // CHRG 低 = 正在充电
  bool full;       // STDBY 低 = 已充满
  bool valid;      // 电量计读成功；false 时图标显示 "--" 而不是假装 0%
};

// 把电量图标画进 framebuffer（1 字节/像素，值为 board.h 的 Palette 枚举）。
//
// slot 是服务器在 manifest.json 的 battery_slot 里给的右下角留白区坐标 ——
// 服务器渲染照片时已经把这块留白了，所以这里直接画，不会盖住文案和地点。
//
// slotScale 是 manifest 里的 battery_slot.scale：图标整体按整数倍放大（5×8 点阵
// 只能整数倍放大才不糊）。480 宽的屏是 1，1200 宽的 7.09" 是 2，老服务器缺省 1。
//
// ⚠️ 本函数【只画、不复原】。重绘前调用方必须先把 framebuffer 复原成服务器
//    原图（memcpy 一份 photo_buf 回来），否则上一轮的图标会和新的叠在一起。
// slotGlyphH = 百分比数字的字形高（服务器按数字 bbox 量的，比中文字高矮约 20%）；
// 传 0 时跟槽高一样，兼容老服务器。
void drawBatteryOverlay(uint8_t* fb, int cw, int ch,
                        int slotX, int slotY, int slotW, int slotH, int slotScale,
                        int slotGlyphH, const BatteryState& st);
