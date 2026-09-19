#pragma once

#include <Arduino.h>
#include <stdint.h>

#include "config_store.h"

// 服务器某个方向目录的清单（对应 output/<屏>/<方向>/manifest.json）
struct DailyManifest {
  bool    ok;
  String  orientation;      // "portrait" | "landscape"
  int     canvasW, canvasH;
  int     count;            // 【该方向实际张数】，不保证是 5，序号 0..count-1 连续
  int     slotX, slotY, slotW, slotH;   // 电量图标槽（服务器留的白底区）
  String  stamp;            // generated_at。"换一张"后靠它判断新图是否真的渲染好了
  char    err[128];
};

// 依次试 portrait → landscape，返回当天有图的第一个方向。
// 优先竖用：服务器自己的顶层同步也是这个顺序，相框默认竖摆。
//
// ⚠️ 两个方向都拿不到时，未必是"今天没出图"——下载密钥写错时服务器同样返回 404，
//    而且和"文件不存在"是同一个码，光看状态码分不出来。所以这里会把两种可能
//    都写进 err，别让用户对着"没有图"排查半天。
bool fetchManifest(const FrameConfig& cfg, DailyManifest& out);

// 下载该方向第 idx 张 bin 到 buf。bufLen 必须 >= canvasW*canvasH。
// 会校验实际字节数，少一个字节都算失败（半张图刷上去比不刷更糟）。
bool fetchPhotoBin(const FrameConfig& cfg, const DailyManifest& m, int idx,
                   uint8_t* buf, size_t bufLen);

// 请服务器重渲染一批新图（POST /api/next）。
//
// ⚠️ 这是【异步】的：返回 true 只代表服务器把渲染进程拉起来了，新图还没出来，
//    调用方得轮询 manifest 的修改时间等它变。另外它没有鉴权，且在服务器关掉
//    网页控制台（ENABLE_REVIEW_WEBUI=False）时会返回 404。
bool requestNextBatch(const FrameConfig& cfg);

// 向服务器上报相框自身状态（电量 / 电压 / 是否在充电 / 当前方向 / 固件版本）。
//
// 纯锦上添花：服务器设置页会多显示这几行，但不上报它照样正常供图。
// 所以这个函数【从不返回失败】——调用方不需要为它做任何分支。
// soc < 0 表示电量计没读出数，会如实报成 soc=null 而不是假装 0%。
void reportDeviceStatus(const FrameConfig& cfg, int soc, float voltage, bool charging,
                        bool full, const char* orientation);
