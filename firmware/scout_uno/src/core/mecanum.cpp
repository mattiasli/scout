#include "mecanum.h"

namespace scout {

namespace {

// protocol/uart.md: bit 7 M1+, 6 M1-, 5 M2+, 4 M2-, 3 M4-, 2 M3-, 1 M3+, 0 M4+.
const uint8_t kForwardBits[kWheelCount] = {0x80, 0x20, 0x02, 0x01};
const uint8_t kBackBits[kWheelCount] = {0x40, 0x10, 0x04, 0x08};

uint8_t magnitude(int16_t value) { return static_cast<uint8_t>(value < 0 ? -value : value); }

}  // namespace

uint8_t forwardBit(uint8_t wheel) { return wheel < kWheelCount ? kForwardBits[wheel] : 0; }

uint8_t backBit(uint8_t wheel) { return wheel < kWheelCount ? kBackBits[wheel] : 0; }

void mixWheels(int8_t vx, int8_t vy, int8_t wz, int8_t speeds[kWheelCount]) {
  int16_t raw[kWheelCount];
  raw[kFrontLeft] = static_cast<int16_t>(vx + vy + wz);
  raw[kRearLeft] = static_cast<int16_t>(vx - vy + wz);
  raw[kFrontRight] = static_cast<int16_t>(vx - vy - wz);
  raw[kRearRight] = static_cast<int16_t>(vx + vy - wz);

  int16_t peak = 0;
  for (uint8_t i = 0; i < kWheelCount; ++i) {
    int16_t m = raw[i] < 0 ? static_cast<int16_t>(-raw[i]) : raw[i];
    if (m > peak) peak = m;
  }
  for (uint8_t i = 0; i < kWheelCount; ++i) {
    int32_t v = raw[i];
    if (peak > kSpeedMax) v = v * kSpeedMax / peak;  // truncates toward zero
    speeds[i] = static_cast<int8_t>(v);
  }
}

uint8_t percentToPwm(uint8_t percent, uint8_t minPwm) {
  if (percent == 0) return 0;
  if (percent >= 100) return 255;
  const uint16_t span = static_cast<uint16_t>(255 - minPwm);
  return static_cast<uint8_t>(minPwm + (static_cast<uint16_t>(percent - 1) * span + 49) / 99);
}

MotorOutput mecanumOutput(int8_t vx, int8_t vy, int8_t wz, uint8_t minPwm, const PwmLayout& layout) {
  int8_t speeds[kWheelCount];
  mixWheels(vx, vy, wz, speeds);

  uint8_t peak5 = 0;
  uint8_t peak6 = 0;
  for (uint8_t i = 0; i < kWheelCount; ++i) {
    const uint8_t m = magnitude(speeds[i]);
    const uint8_t mask = static_cast<uint8_t>(1 << i);
    if ((layout.pwm5Wheels & mask) && m > peak5) peak5 = m;
    if ((layout.pwm6Wheels & mask) && m > peak6) peak6 = m;
  }

  MotorOutput out;
  out.bits = 0;
  out.pwm5 = percentToPwm(peak5, minPwm);
  out.pwm6 = percentToPwm(peak6, minPwm);
  for (uint8_t i = 0; i < kWheelCount; ++i) {
    const uint8_t mask = static_cast<uint8_t>(1 << i);
    const uint8_t reference = (layout.pwm5Wheels & mask) ? peak5 : (layout.pwm6Wheels & mask) ? peak6 : 0;
    const uint8_t m = magnitude(speeds[i]);
    if (m == 0 || reference == 0 || static_cast<uint16_t>(m) * kCoastDivisor < reference) continue;
    out.bits = static_cast<uint8_t>(out.bits | (speeds[i] > 0 ? kForwardBits[i] : kBackBits[i]));
  }
  return out;
}

}  // namespace scout
