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
// ⚠️ 本函数【只画、不复原】。重绘前调用方必须先把 framebuffer 复原成服务器
//    原图（memcpy 一份 photo_buf 回来），否则上一轮的图标会和新的叠在一起。
void drawBatteryOverlay(uint8_t* fb, int cw, int ch,
                        int slotX, int slotY, int slotW, int slotH,
                        const BatteryState& st);
