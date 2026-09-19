#pragma once

#include "overlay.h"   // BatteryState

// 初始化 I2C 与 MAX17048。芯片没应答（没装电池 / I2C 不通）返回 false —— 不致命，
// 只是电量会显示 "--"，充电状态照常能读。
bool batteryBegin();

// 读一次状态。
//
// 充电状态来自 TP4056 的两个开漏脚（IO2=CHRG 低即充电中、IO1=STDBY 低即充满），
// 走的是普通 GPIO，不经过 I2C —— 所以就算电量计挂了，充电指示依然是对的。
BatteryState batteryRead();
