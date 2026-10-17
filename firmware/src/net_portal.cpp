#include "net_portal.h"

#include <Arduino.h>
#include <WebServer.h>
#include <WiFi.h>
#include "esp_wifi.h"   // esp_wifi_stop / esp_wifi_deinit（hardResetWiFi 用）

#include "board.h"

namespace {

WebServer server(80);
FrameConfig g_pending;      // 表单提交后暂存，保存完就重启

// 扫描到的热点，拼成 <option> 片段缓存下来。
//
// ⚠️ 不能在每次加载页面时扫 —— 见 netPortalRun 里的说明，那样会把 AP 拽掉。
String g_scanOptions;

// 局域网配网页状态。g_lanMode 只影响页面上的提示文案（局域网下不扫热点）。
bool g_lanOn = false;
bool g_lanMode = false;

String htmlEscape(const String& s) {
  String out;
  out.reserve(s.length());
  for (size_t i = 0; i < s.length(); ++i) {
    const char c = s[i];
    if (c == '&')       out += F("&amp;");
    else if (c == '<')  out += F("&lt;");
    else if (c == '>')  out += F("&gt;");
    else if (c == '"')  out += F("&quot;");
    else                out += c;
  }
  return out;
}

String buildPage(const FrameConfig& cur) {
  String html;
  html.reserve(6144);
  html += F("<!DOCTYPE html><html><head><meta charset='utf-8'>");
  html += F("<meta name='viewport' content='width=device-width,initial-scale=1'>");
  html += F("<title>InkTime 相框配置</title>");
  html += F("<style>body{font-family:system-ui,-apple-system,'Microsoft YaHei',sans-serif;"
            "max-width:520px;margin:24px auto;padding:0 16px;line-height:1.7;color:#222}"
            "h2{margin:.2em 0 .8em}label{display:block;margin:.9em 0 .25em;font-weight:600}"
            "input,select{width:100%;box-sizing:border-box;padding:8px;font-size:16px;"
            "border:1px solid #bbb;border-radius:6px}"
            "input[type=submit]{margin-top:1.4em;background:#1a5fb4;color:#fff;border:0;"
            "padding:12px;font-size:17px;border-radius:6px}"
            ".hint{color:#666;font-size:13px;margin-top:.25em}"
            ".warn{color:#c00}</style></head><body>");
  html += F("<h2>InkTime 相框配置</h2><form method='POST' action='/save'>");

  html += F("<label>WiFi 名称</label>");
  html += F("<select id='ss' onchange=\"document.getElementById('ssid').value=this.value\">");
  html += F("<option value=''>（从下面列表选，或直接在输入框里填）</option>");
  html += g_scanOptions;   // 进入配网时扫一次的结果，直接用
  html += F("</select>");
  html += F("<input id='ssid' name='ssid' value='");
  html += htmlEscape(cur.wifiSsid);
  html += F("'>");
  if (g_scanOptions.length() == 0) {
    if (g_lanMode) {
      html += F("<div class='hint'>局域网模式下不扫描热点，请在输入框里直接填 WiFi 名称。</div>");
    } else {
      html += F("<div class='hint warn'>没扫到任何热点，请手动填写 WiFi 名称。</div>");
    }
  }
  html += F("<div class='hint'>下拉框只负责填输入框，最终以输入框内容为准。</div>");

  // 密码框不能只说"留空则不修改"：第一次配置时留空 = 存了个空密码，
  // 之后永远连不上、而且从界面上完全看不出哪里错了。所以按当前状态把话说清楚。
  html += F("<label>WiFi 密码</label><input name='pass' type='password' placeholder='");
  html += (cur.wifiPass.length() > 0) ? F("留空则不修改（已存有密码）")
                                      : F("必填 —— 目前没有存密码");
  html += F("'>");
  if (cur.wifiPass.length() == 0) {
    html += F("<div class='hint warn'>当前没有存 WiFi 密码。WPA2 热点必须填密码，"
              "留空会一直连不上。</div>");
  }

  html += F("<label>服务器地址</label>");
  html += F("<input name='hostport' placeholder='192.168.88.68:8765' value='");
  html += htmlEscape(cur.hostPort);
  html += F("'>");
  html += F("<div class='hint'>跑 InkTime 服务器那台机器的 IP 和端口，不用写 http://。</div>");

  html += F("<label>下载密钥</label>");
  html += F("<input name='dlkey' value='");
  html += htmlEscape(cur.downloadKey);
  html += F("'>");
  html += F("<div class='hint'>要和服务器 config.py 里的 DOWNLOAD_KEY 一字不差，"
            "写错会拉不到图（服务器返回 404）。</div>");

  html += F("<label>屏幕型号</label><select name='panel'>");
  {
    static const char* const ids[2]    = {"GDEP073E01", "7.09_E6"};
    static const char* const labels[2] = {"7.3 英寸 800×480（GDEP073E01）",
                                          "7.09 英寸 1200×1600（GDEB0709E01）"};
    for (int i = 0; i < 2; ++i) {
      html += F("<option value='");
      html += ids[i];
      html += F("'");
      if (cur.panel == ids[i]) html += F(" selected");
      html += F(">");
      html += labels[i];
      html += F("</option>");
    }
  }
  html += F("</select>");
  html += F("<div class='hint'>要和服务器设置页里【已启用】的屏幕名一字不差；"
            "两块屏一次只插一块。</div>");

  html += F("<label>每天几点换图</label><select name='hour'>");
  for (int h = 0; h < 24; ++h) {
    html += "<option value='";
    html += String(h);
    html += "'";
    if (h == (int)cur.refreshHour) html += F(" selected");
    html += F(">");
    html += String(h);
    html += F(" 点</option>");
  }
  html += F("</select>");

  html += F("<label>充电时多久重绘一次电量</label><select name='chgmin'>");
  html += F("<option value='0'");
  if (cur.chargingRedrawMin == 0) html += F(" selected");
  html += F(">不重绘</option>");
  for (int m = 10; m <= 60; m += 10) {
    html += "<option value='";
    html += String(m);
    html += "'";
    if ((int)cur.chargingRedrawMin == m) html += F(" selected");
    html += F(">");
    html += String(m);
    html += F(" 分钟</option>");
  }
  html += F("</select>");
  html += F("<div class='hint'>墨水屏每次重绘都要全屏刷新约 40 秒、屏幕会明显闪一下，"
            "别设太密。选「不重绘」就只在每天换图时更新一次电量。</div>");

  html += F("<label>时区</label><select name='tz'>");
  for (int t = -12; t <= 14; ++t) {
    html += "<option value='";
    html += String(t);
    html += "'";
    if (t == (int)cur.tzOffsetHours) html += F(" selected");
    html += F(">");
    if (t >= 0) html += F("+");
    html += String(t);
    html += F("</option>");
  }
  html += F("</select>");

  html += F("<input type='submit' value='保存并重启'>");
  html += F("</form></body></html>");
  return html;
}

void handleRoot() { server.send(200, "text/html; charset=utf-8", buildPage(g_pending)); }

void handleSave() {
  FrameConfig c = g_pending;

  const String ssid = server.arg("ssid");
  const String pass = server.arg("pass");
  const String host = server.arg("hostport");
  const String key  = server.arg("dlkey");

  String s = ssid; s.trim();
  String h = host; h.trim();
  String k = key;  k.trim();

  if (s.length() > 0) c.wifiSsid = s;
  // 密码留空表示不修改 —— 页面上不回显密码，每次重填太烦
  if (pass.length() > 0) c.wifiPass = pass;
  c.hostPort    = h;
  if (k.length() > 0) c.downloadKey = k;

  // 只接受这两个已知型号：名字决定用哪套驱动，乱填会让屏直接不显示
  const String panel = server.arg("panel");
  if (panel == "GDEP073E01" || panel == "7.09_E6") c.panel = panel;

  int tz = server.arg("tz").toInt();
  c.tzOffsetHours = (tz < -12) ? -12 : (tz > 14 ? 14 : tz);
  int hour = server.arg("hour").toInt();
  c.refreshHour = (uint8_t)((hour < 0) ? 0 : (hour > 23 ? 23 : hour));
  int chg = server.arg("chgmin").toInt();
  c.chargingRedrawMin = (uint16_t)((chg < 0) ? 0 : (chg > 240 ? 240 : chg));
  c.valid = (c.wifiSsid.length() > 0);

  // 有 SSID 却没密码，WPA2 热点必然连不上。与其存进去让人在串口日志里猜半天，
  // 不如当场拒绝、把话说清楚。
  if (c.wifiSsid.length() > 0 && c.wifiPass.length() == 0) {
    server.send(200, "text/html; charset=utf-8",
                F("<html><head><meta charset='utf-8'></head><body "
                  "style='font-family:sans-serif;max-width:520px;margin:40px auto'>"
                  "<h3>没填 WiFi 密码，没有保存</h3>"
                  "<p>WPA2 加密的热点必须填密码。请点浏览器后退、把密码填上再保存。</p>"
                  "<p style='color:#666'>如果这个热点确实没有密码（开放网络），"
                  "那它不需要配置——但路由器上的加密方式要确认一下。</p>"
                  "</body></html>"));
    return;
  }

  configSave(c);

  server.send(200, "text/html; charset=utf-8",
              F("<html><head><meta charset='utf-8'></head><body style='font-family:sans-serif'>"
                "<h3>保存成功，相框即将重启…</h3></body></html>"));
  delay(800);

  // 重启前必须把 IO45 的内部上拉撤掉：它是 strapping 脚，带着上拉复位有让
  // VDD_SPI 锁存成 1.8V 的风险（本模块是 3.3V flash，那样就起不来了）。
  releaseStrappingPins();
  ESP.restart();
}

// AP 配网和局域网配网共用同一个 WebServer，路由只注册一次。
bool g_routesReady = false;
void ensureRoutes() {
  if (g_routesReady) return;
  server.on("/", HTTP_GET, handleRoot);
  server.on("/save", HTTP_POST, handleSave);
  g_routesReady = true;
}

}  // namespace

// ── 局域网配网页（WiFi 已连上时用 80 端口提供同一张表单） ──────────────

void lanConfigBegin(const FrameConfig& cur) {
  if (g_lanOn) return;
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[配置] 没连上 WiFi，局域网配置页不启动");
    return;
  }
  g_lanMode = true;
  g_pending = cur;
  ensureRoutes();
  server.begin();
  g_lanOn = true;
  Serial.printf("[配置] 局域网配置页：http://%s/ （清醒期间可用）\n",
                WiFi.localIP().toString().c_str());
}

void lanConfigTick() {
  if (g_lanOn) server.handleClient();
}

void lanConfigStop() {
  if (!g_lanOn) return;
  server.stop();
  g_lanOn = false;
  g_lanMode = false;
  Serial.println("[配置] 局域网配置页已关闭");
}

// ⚠️⚠️ 临时调试开关，定位完改回 false ⚠️⚠️
//
// true  = 跳过扫描 + 开一个【无密码】的热点 + 信道 1
// false = 正常行为（扫描给下拉框用、WPA2 密码、信道 6）
//
// 目的：把"认证"、"扫描"、"模式切换"这三个变量一次性从方程里去掉，
// 让"手机能不能连上"只剩下【射频 + 板子本身】这一个问题。
// 如果连开放热点都连不上，那就不是密码/加密的事，是收发本身的问题。
// 无密码只是本地一个没有外网的调试热点，定位完就改回去。
constexpr bool AP_DEBUG_SIMPLEST = false;

// 把 WiFi 驱动整个卸掉再重建，然后才开热点。
//
// 【为什么需要】ESP32 的 WiFi 驱动经历一串【失败的连接尝试】之后，状态机不一定干净：
// 现象是 softAP() 报"成功"、客户端却怎么都连不上。而这块板子**第一次上电**时
// （NVS 空的、压根没调用过 WiFi.begin()）同一个热点是能连上的 —— 差别就在这里。
//
// esp_wifi_deinit() 把驱动彻底卸载，之后的 WiFi.mode() 会从零重新初始化。
// 这比单纯 disconnect + mode(OFF) 干净得多。
void hardResetWiFi() {
  WiFi.scanDelete();
  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_OFF);
  delay(300);
  // 【去过一次 esp_wifi_deinit()，又拿掉了】
  // 原本想"把驱动彻底卸载再重建"来清干净状态。但在 Arduino 封装下，
  // deinit 之后 Arduino 的 WiFi 对象还留着指向已释放内存的指针，
  // 后续 WiFi.mode()/softAP() 有可能踩到 —— 属于没验证过的偏方，风险大于收益。
  // 现在就按最保守的做法：disconnect + 关射频 + 等一会儿，让驱动自己收敛。
}

void netPortalRun(const FrameConfig& cur, unsigned long timeoutMs) {
  Serial.println("[配网] 进入配网模式");
  hardResetWiFi();

  if (AP_DEBUG_SIMPLEST) {
    Serial.println("[配网] ⚠️ 极简调试模式：不扫描、无密码、信道 1");
    WiFi.mode(WIFI_AP);
    delay(200);
    const String ssid = "InkTime-" + String((uint32_t)ESP.getEfuseMac(), HEX).substring(4);
    const bool ok = WiFi.softAP(ssid.c_str(), nullptr, /*channel=*/1, /*hidden=*/0,
                                /*max_conn=*/4);
    delay(200);
    Serial.printf("[配网] 热点 %s 开启%s  信道=%d  IP=%s\n", ssid.c_str(),
                  ok ? "成功" : "失败", (int)WiFi.channel(),
                  WiFi.softAPIP().toString().c_str());
    g_pending = cur;
    ensureRoutes();
    server.begin();
    unsigned long lastBeat = millis();
    for (;;) {
      server.handleClient();
      delay(10);
      if (millis() - lastBeat > 5000) {
        lastBeat = millis();
        Serial.printf("[配网] 存活 %lu 秒 | 信道 %d | 已连客户端 %d\n",
                      (millis() - lastBeat) / 1000 + 5, (int)WiFi.channel(),
                      (int)WiFi.softAPgetStationNum());
      }
    }
  }

  // ── 第一步：用 STA 模式扫一遍热点，拼好下拉框，然后【就再也不扫了】 ──
  //
  // 为什么要这么绕：原来是 WiFi_AP_STA + 每次加载页面都扫一次。实测下来
  // 手机和电脑都连不上这个热点 —— 因为 AP_STA 模式下，STA 的每次扫描都会把
  // 射频从 AP 的信道拽走（要扫 1/6/11 各个信道），客户端关联/握手正好撞上就失败。
  // 现在改成：扫一次 → 切纯 AP → 固定信道，之后射频只服务 AP。
  WiFi.mode(WIFI_STA);
  delay(150);
  {
    const int n = WiFi.scanNetworks(/*async=*/false, /*show_hidden=*/true);
    Serial.printf("[配网] 扫到 %d 个热点\n", n);
    if (n > 0) {
      String opts;
      opts.reserve(2048);
      for (int i = 0; i < n; ++i) {
        const String s = WiFi.SSID(i);
        if (s.length() == 0) continue;
        opts += F("<option value='");
        opts += htmlEscape(s);
        opts += F("'>");
        opts += htmlEscape(s);
        opts += F(" (");
        opts += String((int)WiFi.RSSI(i));
        opts += F("dBm)</option>");
      }
      g_scanOptions = opts;
    } else {
      g_scanOptions = "";
    }
    WiFi.scanDelete();
  }
  WiFi.mode(WIFI_OFF);
  delay(100);

  // ── 第二步：纯 AP，固定信道 6 ──
  // 默认的 channel 1 常和周围路由器挤在一起；固定下来是为了稳定，不是为了避开谁。
  WiFi.mode(WIFI_AP);
  WiFi.setSleep(false);

  // 【曾经这里把发射功率压到 8.5dBm，后来撤掉了】
  // 当时怀疑"一连热点就重启"是供电欠压（发射瞬时电流太大），降功率是冲着那个去的。
  // 后来查到真因是【中断看门狗】（刷屏时没让出 CPU），跟功耗无关，所以恢复默认功率。
  // 默认 19.5dBm 覆盖更好，配网和每天拉那 384KB 都更稳。
  // （如果要省电可以降到 8.5dBm，参考项目对 STA 就是这么干的，但那会牺牲覆盖。）
  const String apSsid = "InkTime-" + String((uint32_t)ESP.getEfuseMac(), HEX).substring(4);
  const bool ok = WiFi.softAP(apSsid.c_str(), "12345678", /*channel=*/6, /*hidden=*/0,
                              /*max_conn=*/4);
  Serial.printf("[配网] 热点 %s 开启 %s（信道 6），地址 %s\n", apSsid.c_str(),
                ok ? "成功" : "失败", WiFi.softAPIP().toString().c_str());

  g_pending = cur;   // 表单默认值用当前配置

  ensureRoutes();
  server.begin();

  const unsigned long enterMs = millis();
  unsigned long lastBeat = millis();
  for (;;) {
    server.handleClient();
    delay(10);

    // 每 20 秒一行心跳：还活着、有几个客户端连着。
    // 排查"热点时有时无 / 连上又断"时，这行能立刻分清是板子的问题还是客户端的问题 ——
    // 心跳一直在打 = 板子好着呢，AP 也开着，那就是手机自己漫游走了。
    if (millis() - lastBeat > 20000) {
      lastBeat = millis();
      Serial.printf("[配网] 心跳：已运行 %lu 秒，当前连着 %d 个客户端\n",
                    (millis() - enterMs) / 1000, (int)WiFi.softAPgetStationNum());
    }

    // 没有有效配置时【永不超时】—— 超时就等于把人锁在门外，只能拔电池重来
    if (timeoutMs > 0 && cur.valid && (millis() - enterMs > timeoutMs)) {
      Serial.println("[配网] 超时，未保存任何配置，返回正常流程");
      server.stop();
      WiFi.mode(WIFI_OFF);
      return;
    }
  }
}
