#include "daily_fetch.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>

#include "board.h"

namespace {

// 两个方向都要试一遍，顺序即优先级
const char* const kOrients[2] = {"portrait", "landscape"};

String baseUrl(const FrameConfig& cfg) {
  String hp = cfg.hostPort;
  hp.trim();
  // 末尾斜杠去掉，免得拼出 //static/...
  while (hp.endsWith("/")) hp.remove(hp.length() - 1);
  if (hp.startsWith("http://") || hp.startsWith("https://")) return hp;
  return "http://" + hp;
}

String dirUrl(const FrameConfig& cfg, const char* orient, const String& file) {
  return baseUrl(cfg) + "/static/inktime/" + cfg.downloadKey + "/" + cfg.panel + "/" +
         orient + "/" + file;
}

// 下载到内存缓冲，返回实际读到的字节数；失败返回 -1。
//
// 不用 http.getString()：一张图 384000 字节，getString 会在堆上再复制一份
// 还可能失败。这里直接往调用方的缓冲里流式读，只校验总长度。
// 大文件下载：分块阻塞读 + 断点续传。
//
// 教训：7.09" 的图 1.92MB（老屏的 5 倍）。原来按"总共 60 秒"判超时，实测只收到
// 5~8KB 就断流 —— 弱信号（-70dBm）下 TCP 重传会停十几秒，而原来的
// available() 轮询等不到数据，看着就像卡死。现在：
//   · 用 readBytes 阻塞读 4KB 块（socket 超时 5 秒），不靠 available() 轮询
//   · 只要还在进数据就一直等；连续 idleMs 没进展才算断
//   · 断了就带 Range 头续传，最多 10 次；总时长 capMs 兜底
int downloadToBuffer(const String& url, uint8_t* buf, size_t expect, uint32_t idleMs,
                     uint32_t capMs) {
  const uint32_t t0 = millis();
  size_t got = 0;
  size_t nextReport = 256UL * 1024UL;

  // 注意：modem sleep 已经在 connectWiFi() 里全局关掉了（见那里的注释），
  // 这里不再临时开关 —— 否则传完又把它打开，beacon 超时还会回来。

  for (int attempt = 1; attempt <= 10 && got < expect; ++attempt) {
    if (millis() - t0 > capMs) {
      Serial.printf("[拉图] 总时长超过 %lu 秒，放弃\n", (unsigned long)(capMs / 1000));
      break;
    }

    HTTPClient http;
    http.begin(url);
    http.setTimeout(5000);
    if (got > 0) {
      http.addHeader("Range", "bytes=" + String((unsigned)got) + "-");
      Serial.printf("[拉图] 断点续传：从 %u 字节继续（第 %d 次）\n", (unsigned)got, attempt);
    }

    const int code = http.GET();
    if (code != HTTP_CODE_OK && code != HTTP_CODE_PARTIAL_CONTENT) {
      Serial.printf("[拉图] GET %s → HTTP %d\n", url.c_str(), code);
      http.end();
      delay(500);
      continue;
    }
    if (code == HTTP_CODE_OK && got > 0) {
      // 服务器没理 Range，返回了整份文件 —— 只能从头覆盖，否则会越界
      Serial.println("[拉图] 服务器忽略了 Range，从头重下");
      got = 0;
    }

    const size_t wantTotal = (code == HTTP_CODE_PARTIAL_CONTENT) ? (expect - got) : expect;
    const int len = http.getSize();
    if (len > 0 && (size_t)len != wantTotal) {
      Serial.printf("[拉图] 长度不符：Content-Length=%d，应为 %u\n", len, (unsigned)wantTotal);
      http.end();
      return -1;
    }

    WiFiClient* stream = http.getStreamPtr();
    uint32_t lastProgress = millis();
    while (got < expect) {
      const size_t chunk = (expect - got > 4096) ? 4096 : (expect - got);
      const int r = stream->readBytes(buf + got, chunk);
      if (r > 0) {
        got += (size_t)r;
        lastProgress = millis();
        if (got >= nextReport) {
          const float secs = (millis() - t0) / 1000.0f;
          Serial.printf("[拉图] %u KB / %u KB（%.1f 秒，%.1f KB/s）\n", (unsigned)(got / 1024),
                        (unsigned)(expect / 1024), secs,
                        (got / 1024.0f) / (secs > 0.01f ? secs : 1));
          nextReport += 256UL * 1024UL;
        }
        continue;
      }
      if (!http.connected()) break;
      if (millis() - lastProgress > idleMs) {
        Serial.printf("[拉图] 连续 %lu 秒没有新数据（已到 %u/%u）\n",
                      (unsigned long)(idleMs / 1000), (unsigned)got, (unsigned)expect);
        break;
      }
      if (millis() - t0 > capMs) break;
    }
    http.end();
  }

  if (got != expect) {
    Serial.printf("[拉图] 字节数不对：读到 %u，应为 %u\n", (unsigned)got, (unsigned)expect);
    return -1;
  }
  return (int)got;
}

}  // namespace

bool fetchManifest(const FrameConfig& cfg, DailyManifest& out) {
  out.ok = false;
  out.orientation = "";
  out.count = 0;
  out.err[0] = 0;

  if (cfg.hostPort.length() == 0) {
    snprintf(out.err, sizeof(out.err), "还没配置服务器地址");
    return false;
  }

  int lastCode = 0;
  for (int i = 0; i < 2; ++i) {
    const String url = dirUrl(cfg, kOrients[i], "manifest.json");

    HTTPClient http;
    http.begin(url);
    http.setTimeout(15000);
    const int code = http.GET();
    if (code != HTTP_CODE_OK) {
      Serial.printf("[拉图] %s 方向没有图（HTTP %d）\n", kOrients[i], code);
      lastCode = code;
      http.end();
      continue;
    }
    const String body = http.getString();
    http.end();

    JsonDocument doc;
    const DeserializationError e = deserializeJson(doc, body);
    if (e) {
      Serial.printf("[拉图] %s 的 manifest 解析失败：%s\n", kOrients[i], e.c_str());
      snprintf(out.err, sizeof(out.err), "manifest 解析失败：%s", e.c_str());
      return false;
    }

    out.canvasW = doc["canvas"]["width"] | 0;
    out.canvasH = doc["canvas"]["height"] | 0;
    out.count   = doc["count"] | 0;
    out.slotX   = doc["battery_slot"]["x"] | 0;
    out.slotY   = doc["battery_slot"]["y"] | 0;
    out.slotW   = doc["battery_slot"]["w"] | 0;
    out.slotH   = doc["battery_slot"]["h"] | 0;
    // 图标放大倍数：服务器按画布短边算好（480 宽 = 1 倍，1200 宽 = 2 倍）。
    // 老服务器没这个字段时缺省 1，行为和以前完全一致。
    out.slotScale = doc["battery_slot"]["scale"] | 1;
    if (out.slotScale < 1) out.slotScale = 1;
    if (out.slotScale > 4) out.slotScale = 4;
    // 百分比数字的字形高（比中文矮约 20%）。老服务器没这个字段时 0 = 跟槽高一样。
    out.slotGlyphH = doc["battery_slot"]["glyph_h"] | 0;
    if (out.slotGlyphH < 0) out.slotGlyphH = 0;
    out.stamp   = String((const char*)(doc["generated_at"] | ""));
    out.orientation = String(kOrients[i]);

    if (out.count <= 0 || out.canvasW <= 0 || out.canvasH <= 0) {
      snprintf(out.err, sizeof(out.err), "%s 的 manifest 内容不合理（count=%d canvas=%dx%d）",
               kOrients[i], out.count, out.canvasW, out.canvasH);
      return false;
    }
    if (out.slotW <= 0 || out.slotH <= 0) {
      // 老版本服务器没有 battery_slot。不是致命错误 —— 不画电量图标就是了，
      // 总比整个不显示强。
      Serial.println("[拉图] manifest 里没有 battery_slot（服务器版本偏旧？），本次不画电量图标");
    }

    out.ok = true;
    Serial.printf("[拉图] 用 %s 方向：%d 张，画布 %dx%d，电量槽 (%d,%d %dx%d)\n",
                  out.orientation.c_str(), out.count, out.canvasW, out.canvasH,
                  out.slotX, out.slotY, out.slotW, out.slotH);
    return true;
  }

  // 按错误码给出真正有用的方向。"连不上"和"404"完全是两回事，混在一起报
  // 会让人对着错的方向查半天 —— 实测踩过：服务器地址端口写反（8765 写成 8675），
  // 现象是"两个方向都没有图"，但真正的原因跟图一点关系都没有。
  if (lastCode < 0) {
    snprintf(out.err, sizeof(out.err),
             "连不上服务器（HTTP %d）。先查配网页里的【服务器地址和端口】对不对 —— "
             "地址或端口写错不会报 404，只会像这样连接失败", lastCode);
  } else if (lastCode == 404) {
    snprintf(out.err, sizeof(out.err),
             "两个方向都没有图。可能是今天还没渲染，也可能是下载密钥不对"
             "（服务器两种情况都回 404，分不出来）");
  } else {
    snprintf(out.err, sizeof(out.err), "拿不到 manifest（最后一个 HTTP 码 %d）", lastCode);
  }
  return false;
}

bool fetchPhotoBin(const FrameConfig& cfg, const DailyManifest& m, int idx, uint8_t* buf,
                   size_t bufLen) {
  if (!m.ok) return false;
  if (idx < 0 || idx >= m.count) {
    Serial.printf("[拉图] 序号 %d 超出范围（本方向共 %d 张）\n", idx, m.count);
    return false;
  }
  const size_t expect = (size_t)m.canvasW * (size_t)m.canvasH;
  if (bufLen < expect) {
    Serial.printf("[拉图] 缓冲太小：%u < %u\n", (unsigned)bufLen, (unsigned)expect);
    return false;
  }

  const String url = dirUrl(cfg, m.orientation.c_str(), "photo_" + String(idx) + ".bin");
  Serial.printf("[拉图] 取第 %d 张：%s\n", idx, url.c_str());
  // 20 秒没新数据才叫超时；总上限 3 分钟（1.92MB 在 30KB/s 下约 64 秒）
  return downloadToBuffer(url, buf, expect, 20000UL, 180000UL) >= 0;
}

void reportDeviceStatus(const FrameConfig& cfg, int soc, float voltage, bool charging,
                        bool full, const char* orientation) {
  if (cfg.hostPort.length() == 0) return;

  JsonDocument doc;
  // 读不到就如实报 null，别报 0 —— 报 0 的话设置页显示"0%"，跟真的没电分不出来
  if (soc < 0) doc["soc"] = nullptr;
  else         doc["soc"] = soc;
  doc["voltage"]     = (voltage > 0.1f) ? voltage : 0;
  doc["charging"]    = charging;
  doc["full"]        = full;
  doc["orientation"] = orientation ? orientation : "";
  doc["fw"]          = FW_VERSION;
  doc["rssi"]        = (WiFi.status() == WL_CONNECTED) ? (int)WiFi.RSSI() : 0;

  String body;
  serializeJson(doc, body);

  HTTPClient http;
  http.begin(baseUrl(cfg) + "/api/device/report");
  http.setTimeout(8000);
  http.addHeader("Content-Type", "application/json");
  const int code = http.POST(body);
  http.end();

  Serial.printf("[上报] 设备状态 → HTTP %d\n", code);
}

bool requestNextBatch(const FrameConfig& cfg) {
  if (cfg.hostPort.length() == 0) return false;

  const String url = baseUrl(cfg) + "/api/next";
  HTTPClient http;
  http.begin(url);
  http.setTimeout(10000);
  // 服务端是 POST（GET 会 405），空 body
  const int code = http.POST((uint8_t*)nullptr, 0);
  http.end();

  Serial.printf("[换一张] POST /api/next → HTTP %d\n", code);
  if (code != HTTP_CODE_OK) {
    Serial.println("[换一张] 请求被拒。若服务器关掉了网页控制台（ENABLE_REVIEW_WEBUI=False），"
                   "这个接口会回 404。");
    return false;
  }
  return true;
}
