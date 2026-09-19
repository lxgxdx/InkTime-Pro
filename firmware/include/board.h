// 本板的引脚、屏幕常量与调色板索引。
//
// 全部来自 kicad/InkTime.kicad_pcb 的实际网表（不是照抄原项目）：
//   · 屏 SPI 走 IO13/14/46/17/18/21
//   · 充电状态 IO2=CHRG（低=充电中）、IO1=STDBY（低=已充满），R5/R6 上拉到 3V3
//   · 按键 SW2/SW3/SW4 在 IO45/47/48，对地，无外部上拉 → 固件必须开内部上拉
//
// 本头文件刻意不 include GxEPD2：调色板在这里只定义"服务器索引"，
// 到 GxEPD2 颜色常量的映射放在 epd_display.cpp 里，免得每个文件都被动拖进显示库。

#pragma once

#include <stddef.h>
#include <stdint.h>

// 固件版本，只在设备上报里露个脸（服务器设置页「相框设备」那张卡片会显示）
constexpr char FW_VERSION[] = "v0.1.0";

// ======================= 屏幕 =======================
// 面板原生就是横向 800×480（GDEP073E01 规格书 §2 Features）。
// 竖用是逻辑旋转，靠 display.setRotation() 实现，不改这里的值。
constexpr int PANEL_W = 800;
constexpr int PANEL_H = 480;
constexpr int PANEL_BYTES = PANEL_W * PANEL_H;   // 1 byte/px = 384000

// 服务器 SCREENS 里的屏名，下载 URL 的第一段路径就是它（software/config.py）
constexpr char SCREEN_NAME[] = "GDEP073E01";

// 下载密钥的出厂默认值。必须与服务器 config.py 的 DOWNLOAD_KEY 一致 ——
// 不一致时服务器返回 404，而且这个 404 和"文件不存在"是同一个码，光看状态码
// 分不出来，只能去翻服务器日志。配网页里可以改，改完存 NVS。
constexpr char DEFAULT_DOWNLOAD_KEY[] = "inktime_local_test";

// ======================= 屏引脚 =======================
constexpr int PIN_EPD_DIN  = 13;
constexpr int PIN_EPD_SCLK = 14;
constexpr int PIN_EPD_CS   = 46;   // S3 strapping 脚，空置弱下拉=0，配合 IO0=1 → SPI Boot
constexpr int PIN_EPD_DC   = 17;
constexpr int PIN_EPD_RST  = 18;
constexpr int PIN_EPD_BUSY = 21;   // 低=忙（规格书 Note 5-4）

// ======================= 电量计 / 充电状态 =======================
constexpr int PIN_I2C_SCL = 8;
constexpr int PIN_I2C_SDA = 9;

// TP4056 的两个开漏状态脚，都有外部上拉，因此【低电平=有效】
constexpr int PIN_CHRG  = 2;   // 低 = 正在充电
constexpr int PIN_STDBY = 1;   // 低 = 已充满
// ⚠️ 千万别把 IO2 当 LED 用（原固件 LED_BUILTIN=2 就是这么干的）：
//    那是 TP4056 的开漏输出，推挽拉低会和它直接对拉。

// ======================= 按键 =======================
// 全部对地、无外部上拉，靠 INPUT_PULLUP。空闲高、按下低。
constexpr int PIN_BTN_NEXT    = 45;   // SW2：换一张
constexpr int PIN_BTN_REFRESH = 47;   // SW3：重新拉当前图
constexpr int PIN_BTN_BATT    = 48;   // SW4：立即刷新电量图标
// SW5 = IO0 是 BOOT 键（R4 10k 上拉），烧录用，固件不碰。

constexpr unsigned long BTN_DEBOUNCE_MS = 30;
constexpr unsigned long BTN_LONG_MS     = 1200;   // 运行中长按的判定时长

// ⚠️ IO45 是 S3 的 strapping 脚（选 VDD_SPI 3.3V/1.8V）。我们模块是 3.3V flash，
//    所以它上电必须是低 —— 空置时靠芯片内部弱下拉正好是低，安全。
//    但固件为了读按键会给它开内部上拉（=高），此时若调用 ESP.restart()，
//    有把 VDD_SPI 重新锁存成 1.8V 的风险 → 重启前必须先调 releaseStrappingPins()。
void releaseStrappingPins();

// ======================= 服务器调色板索引 =======================
// 顺序必须与 software/config.py 的 SCREENS[].palette 逐项一致：
//   (0,0,0) (255,255,255) (200,0,0) (220,180,0) (0,0,255) (0,150,0)
// bin 文件里每个字节就是一个这个枚举值。
enum Palette : uint8_t {
  PAL_BLACK  = 0,
  PAL_WHITE  = 1,
  PAL_RED    = 2,
  PAL_YELLOW = 3,
  PAL_BLUE   = 4,
  PAL_GREEN  = 5,
  PAL_COUNT  = 6,
};

// 低电量阈值：低于它电量图标变红（见 overlay.cpp）
constexpr int BATT_LOW_PERCENT = 20;

// ======================= 内存 =======================
// 一帧 = 384000 字节，内部 RAM 总共才 512KB，两个缓冲就撑爆了。
// 所以一律走 PSRAM（本模块 N8R8 有 8MB），只有在 PSRAM 不可用时才退回内部 RAM。
// 返回 nullptr 表示彻底失败 —— 调用方必须检查，别直接解引用。
void* psramAlloc(size_t n);
