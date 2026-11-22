#include "leds.h"

#include <esp_timer.h>

#include "config.h"

namespace scout {

void Light::begin() {
  // Low before the PWM takes over, so the LED never flashes at boot.
  pinMode(cfg::kLightPin, OUTPUT);
  digitalWrite(cfg::kLightPin, LOW);
  attached_ = ledcAttach(cfg::kLightPin, cfg::kLightFreqHz, cfg::kLightResolutionBits);
  set(0);
}

void Light::set(int32_t level) {
  if (level < 0) level = 0;
  if (level > 100) level = 100;
  level_ = level;
  uint32_t duty = lightDuty(level, cfg::kLightMaxDuty);
  if (attached_) {
    ledcWrite(cfg::kLightPin, duty);
  } else {
    digitalWrite(cfg::kLightPin, LOW);  // no PWM: never drive the LED at full power
  }
}

void StatusLed::begin() {
  pinMode(cfg::kStatusLedPin, OUTPUT);
  digitalWrite(cfg::kStatusLedPin, cfg::kStatusLedActiveLow ? HIGH : LOW);
  esp_timer_create_args_t args = {};
  args.callback = &StatusLed::tick;
  args.arg = this;
  args.dispatch_method = ESP_TIMER_TASK;
  args.name = "status_led";
  esp_timer_handle_t timer = nullptr;
  if (esp_timer_create(&args, &timer) == ESP_OK) {
    esp_timer_start_periodic(timer, static_cast<uint64_t>(cfg::kStatusLedTickMs) * 1000);
  }
}

void StatusLed::tick(void* self) {
  StatusLed* led = static_cast<StatusLed*>(self);
  bool on = ledOn(led->mode_, static_cast<uint32_t>(esp_timer_get_time() / 1000));
  if (on == led->lastOn_) return;
  led->lastOn_ = on;
  bool level = cfg::kStatusLedActiveLow ? !on : on;
  digitalWrite(cfg::kStatusLedPin, level ? HIGH : LOW);
}

}  // namespace scout
