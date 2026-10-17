#include "led.h"

#include <Arduino.h>

#include "board.h"

namespace {

constexpr uint16_t BLINK_ON_MS  = 90;
constexpr uint16_t BLINK_OFF_MS = 90;

inline void writeRed(bool on)   { digitalWrite(PIN_LED_RED,   on ? HIGH : LOW); }
inline void writeGreen(bool on) { digitalWrite(PIN_LED_GREEN, on ? HIGH : LOW); }

}  // namespace

void ledBegin() {
  pinMode(PIN_LED_RED, OUTPUT);
  pinMode(PIN_LED_GREEN, OUTPUT);
  ledAllOff();
}

void ledSet(bool red, bool green) {
  writeRed(red);
  writeGreen(green);
}

void ledAllOff() {
  writeRed(false);
  writeGreen(false);
}

void ledFlashGreen(int times) {
  for (int i = 0; i < times; ++i) {
    writeRed(false);
    writeGreen(true);
    delay(BLINK_ON_MS);
    writeGreen(false);
    delay(BLINK_OFF_MS);
  }
}

void ledFlashRed(int times) {
  for (int i = 0; i < times; ++i) {
    writeRed(true);
    writeGreen(false);
    delay(BLINK_ON_MS);
    writeRed(false);
    delay(BLINK_OFF_MS);
  }
}
