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
  return baseUrl(cfg) + "/static/inktime/" + cfg.downloadKey + "/" + SCREEN_NAME + "/" +
         orient + "/" + file;
}

// 下载到内存缓冲，返回实际读到的字节数；失败返回 -1。
//
// 不用 http.getString()：一张图 384000 字节，getString 会在堆上再复制一份
// 还可能失败。这里直接往调用方的缓冲里流式读，只校验总长度。
int downloadToBuffer(const String& url, uint8_t* buf, size_t expect, uint32_t timeoutMs) {
  HTTPClient http;
  http.begin(url);
  http.setTimeout(15000);

  const int code = http.GET();
  if (code != HTTP_CODE_OK) {
    Serial.printf("[拉图] GET %s → HTTP %d\n", url.c_str(), code);
    http.end();
    return -1;
  }

  const int len = http.getSize();
  if (len > 0 && (size_t)len != expect) {
    Serial.printf("[拉图] 长度不符：Content-Length=%d，按画布应为 %u\n", len,
                  (unsigned)expect);
    http.end();
    return -1;
  }

  WiFiClient* stream = http.getStreamPtr();
  size_t got = 0;
  const uint32_t start = millis();
  while (got < expect) {
    if (millis() - start > timeoutMs) {
      Serial.printf("[拉图] 超时，只读到 %u/%u 字节\n", (unsigned)got, (unsigned)expect);
      http.end();
      return -1;
    }
    const size_t avail = stream->available();
    if (avail == 0) {
      if (!http.connected()) break;   // 连接断了且没读满 → 下面的长度校验会拦下
      delay(1);
      continue;
    }
    size_t want = avail;
    if (want > expect - got) want = expect - got;
    const int r = stream->read(buf + got, want);
    if (r > 0) got += (size_t)r;
  }
  http.end();

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
  return downloadToBuffer(url, buf, expect, 60UL * 1000UL) >= 0;
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
