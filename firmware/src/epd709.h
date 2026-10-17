#pragma once

#include <stdint.h>

// 7.09" GDEB0709E01（BOE 24116-01390，控制器 GDEP133C02）
//   1200×1600、六色、双片选（左/右各 600px）、4 线 SPI。
//
// 这套协议和常见的 GxEPD2 屏不一样，要点：
//   · 【没有 DC 线】——CS 拉低后的第一个字节是命令，后面跟的是数据
//   · 两个片选管左右半屏，每行 1200px = 600 字节（4bpp），左 300 + 右 300
//   · BUSY 低 = 忙
//
// 移植自 FrameFilm 的 hal_epd_709.c（GPL-3.0-or-later，作者 Kiritro）。

void epd709Begin();                     // 初始化（复位 + 上电序列）
void epd709ColorBars();                 // 六色竖条：左 黑/白/黄，右 红/蓝/绿

// 把 1 字节/像素（board.h 的 Palette 索引）的缓冲推到屏上并全刷。
// 支持 1200×1600（原生）和 1600×1200（软件转 90°）。
void epd709ShowBuffer(const uint8_t* fb, int w, int h);
void epd709Solid(uint8_t paletteIndex); // 全屏单色：0黑 1白 2黄 3红 4蓝 5绿
void epd709Sleep();                     // POF 掉电
