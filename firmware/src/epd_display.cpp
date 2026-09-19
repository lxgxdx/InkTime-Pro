#include "epd_display.h"

#include <Arduino.h>
#include <SPI.h>
#include <GxEPD2_7C.h>

#include "board.h"

// 页高取面板物理高的 1/4。7C 驱动的页缓冲是 (WIDTH/2)*页高 = 400*120 = 48000 字节，
// 分 4 页推完，合计 192000 = 800*480/2，即每个像素半字节（4bpp）。
// 代价是绘制回调要被调用 4 遍（每页过滤出自己那 120 行），换来的是 SPI 流量减半。
static GxEPD2_7C<GxEPD2_730c_GDEY073D46, GxEPD2_730c_GDEY073D46::HEIGHT / 4>
    epd(GxEPD2_730c_GDEY073D46(PIN_EPD_CS, PIN_EPD_DC, PIN_EPD_RST, PIN_EPD_BUSY));

// 服务器调色板索引 → GxEPD2 颜色常量。
//
// ⚠️⚠️ 右边这列【名字看着别扭是故意的，别照着名字"修正"】。
//
// 两套码表打架：
//
//   GxEPD2 的 GDEY073D46 驱动内部（GxEPD2_7C.h 的 color7()）：
//       0x00 黑   0x01 白   0x02 绿   0x03 蓝   0x04 红   0x05 黄   0x06 橙
//
//   我们这块屏 GDEP073E01 实际（规格书 + 厂商 waveshare/e-Paper 的 EPD_7in3e.h）：
//       0x00 黑   0x01 白   0x02 黄   0x03 红   0x05 蓝   0x06 绿
//       （0x04 无定义 —— 厂家在头文件里把它注释掉了）
//
//   两套表【只有黑白是相同的】。所以要屏出红色，就得传那个"名叫 GxEPD_BLUE、
//   内部码却是 0x03"的常量。
//
// 这不是推理出来的：上板实测第一版颜色全乱（红成了灰、黄成了蓝、蓝成了红、
// 绿成了黄），拿观察结果逐条反查 + 对着厂商头文件核对才定的这张表。
// 改回去 = 再来一次颜色错乱。
//
// 另外这张表必须 6 项齐全。原项目那个 .ino 只写了 4 项、其余 default→白，
// 结果蓝和绿被静默画成白色 —— 图上看着"没问题"，只是颜色没了。
static const uint16_t kGxColor[PAL_COUNT] = {
    GxEPD_BLACK,    // 服务器 0 黑 → 面板 0x00
    GxEPD_WHITE,    // 服务器 1 白 → 面板 0x01
    GxEPD_BLUE,     // 服务器 2 红 → 面板 0x03  ← 名字叫 BLUE，出来是红
    GxEPD_GREEN,    // 服务器 3 黄 → 面板 0x02  ← 名字叫 GREEN，出来是黄
    GxEPD_YELLOW,   // 服务器 4 蓝 → 面板 0x05  ← 名字叫 YELLOW，出来是蓝
    GxEPD_ORANGE,   // 服务器 5 绿 → 面板 0x06  ← 名字叫 ORANGE，出来是绿
};

// 上面那张表对应的面板原始码，只用于自检时打印出来核对
static const uint8_t kPanelCode[PAL_COUNT] = {0x00, 0x01, 0x03, 0x02, 0x05, 0x06};

uint8_t epdPanelCode(int i) {
  return (i >= 0 && i < PAL_COUNT) ? kPanelCode[i] : 0xFF;
}

const char* epdPaletteName(int i) {
  static const char* const kNames[PAL_COUNT] = {"黑", "白", "红", "黄", "蓝", "绿"};
  return (i >= 0 && i < PAL_COUNT) ? kNames[i] : "?";
}

void epdBegin() {
  SPI.end();
  SPI.begin(PIN_EPD_SCLK, -1 /* 屏不回数据，无 MISO */, PIN_EPD_DIN, PIN_EPD_CS);
  epd.init(0, true, 2, false);
}

void epdShowBuffer(const uint8_t* fb, int w, int h) {
  // 面板原生是 800×480 横向 → rotation 0；竖用 480×800 → rotation 1。
  // 这两个值是靠自检模式人工核对方向后才敢定死的（见 README）。
  epd.setRotation((w == PANEL_W && h == PANEL_H) ? 0 : 1);

  // 计时不是"顺手测个性能"，是判断屏到底有没有响应的主要手段。
  //
  // 这块屏全刷固定约 40 秒，而驱动是靠轮询 BUSY 脚等刷新完成的。屏要是没供电
  // （转接板 VCC 没有 5V），BUSY 悬空，驱动会一路走完、不报任何错、耗时接近 0
  // —— 日志看着完全正常，屏却是黑的。所以：
  //     ≈40 秒 → 屏真的在刷，硬件通路是通的
  //     <3 秒  → 屏那头没响应，先去量 +5VEPD
  const uint32_t t0 = millis();
  epd.setFullWindow();
  epd.firstPage();
  // 分页进度打印：这不是噪音，是定位"刷屏途中被看门狗打断"的探针 ——
  // 只看到"开始"没有"第 N 页完成"，就说明卡在第 N 页里；全都打完却没画完，
  // 那卡在最后那次面板刷新（等 BUSY）里。这两种的修法完全不同。
  int pageNo = 0;
  Serial.println("[屏] 开始分页绘制");
  do {
    for (int y = 0; y < h; ++y) {
      const uint8_t* row = fb + (size_t)y * (size_t)w;
      for (int x = 0; x < w; ++x) {
        const uint8_t idx = row[x];
        // 越界索引兜底成白，而不是崩或画黑 —— 数据坏了也别毁掉整幅画
        epd.drawPixel(x, y, idx < PAL_COUNT ? kGxColor[idx] : GxEPD_WHITE);
      }
      // ⚠️ 每 16 行让出一次 CPU —— 这行不是优化，是【不加就会复位】。
      //
      // GxEPD2 的分页绘制会把外面这个回调连跑 4 遍（每页一遍），每遍 800×480
      // = 38 万次 drawPixel，全程一次不让出。实测后果：刷屏刚开始就触发
      // 【中断看门狗】复位（rst:0x8 TG1WDT_SYS_RST，esp_reset_reason()=5），
      // 板子每 5~6 秒重启一次、屏幕永远刷不完、USB 也跟着反复掉线。
      //
      // 参考项目那份 13.3" 固件在同样位置也有一句 `if ((y & 0x0F) == 0) yield();`
      // —— 这是它的用意，别删。
      if ((y & 0x0F) == 0) yield();
    }
    Serial.printf("[屏] 第 %d 页绘制完成（%.1f 秒）\n", ++pageNo, (millis() - t0) / 1000.0);
  } while (epd.nextPage());
  Serial.printf("[屏] 分页绘制全部完成（%.1f 秒），开始等面板刷新…\n", (millis() - t0) / 1000.0);

  const uint32_t dt = millis() - t0;
  Serial.printf("[屏] %dx%d 上屏耗时 %.1f 秒", w, h, dt / 1000.0);
  if (dt < 3000) {
    Serial.println("  ← 太快，屏很可能没响应：先量转接板 VCC 有没有 5V");
  } else {
    Serial.println("  （正常）");
  }

  epd.hibernate();
}

void epdHibernate() { epd.hibernate(); }
