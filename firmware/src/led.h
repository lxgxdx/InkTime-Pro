#pragma once

// v2 新增的两颗状态指示灯。接法（kicad/InkTime.kicad_sch）：
//   IO10 → R16 1k → LED1 阳极 → 阴极 → GND     （红）
//   IO11 → R17 1k → LED2 阳极 → 阴极 → GND     （绿）
// 高电平点亮，约 1.3mA —— 看得见，不刺眼。
//
// ⚠️ 深睡时必须【全灭】。两颗灯各吃 1.3mA，加起来比整机深睡电流目标还大 ——
//    省下来的电被指示灯吃掉就白忙了。goDeepSleepMinutes() 里会强制关掉它们。
//
// 用灯约定（谁亮谁不亮就是一句话的事，照这个来）：
//   全灭        = 空闲 / 深睡
//   绿常亮      = 正在干活（联网、取图、刷屏）
//   绿闪 N 下   = 办成了
//   红常亮      = 正在等人（配网）/ 充电中 / 低电
//   红闪 N 下   = 办砸了

void ledBegin();                      // 初始化成输出并全灭
void ledSet(bool red, bool green);    // 直接设定两灯
void ledAllOff();

// 阻塞式闪几下（每下 90ms 亮 + 90ms 灭），结束时【两颗都是灭的】。
// 用在"成了/砸了"这类一次性提示上，总共不到一秒，不影响刷屏节奏。
void ledFlashGreen(int times);
void ledFlashRed(int times);
