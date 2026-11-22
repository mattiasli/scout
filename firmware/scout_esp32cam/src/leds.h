// The white light (GPIO4, LEDC PWM) and the red status LED (GPIO33).

#ifndef SCOUT_LEDS_H
#define SCOUT_LEDS_H

#include <Arduino.h>

#include "core/util.h"

namespace scout {

class Light {
 public:
  // Call after the camera is initialised (the camera takes its LEDC timer first).
  void begin();
  // Level 0..100 (clamped). Level 100 is capped at cfg::kLightMaxDuty; see config.h.
  void set(int32_t level);
  int32_t level() const { return level_; }

 private:
  bool attached_ = false;
  int32_t level_ = 0;
};

// Blinks the status LED from an esp_timer callback, so the pattern keeps going while
// the main loop is busy connecting.
class StatusLed {
 public:
  void begin();
  void setMode(LedMode mode) { mode_ = mode; }
  LedMode mode() const { return mode_; }

 private:
  static void tick(void* self);
  volatile LedMode mode_ = LedMode::kConnecting;
  bool lastOn_ = false;
};

}  // namespace scout

#endif  // SCOUT_LEDS_H
