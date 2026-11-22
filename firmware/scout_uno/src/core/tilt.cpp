#include "tilt.h"

#include "config.h"

namespace scout {

int8_t clampTilt(int32_t deg) {
  if (deg < kTiltMinDeg) return kTiltMinDeg;
  if (deg > kTiltMaxDeg) return kTiltMaxDeg;
  return static_cast<int8_t>(deg);
}

uint16_t tiltPulseUs(int8_t deg) {
  int16_t angle = static_cast<int16_t>(kServoLevelAngle + clampTilt(deg));
  const uint32_t span = kServoPulseAt180Us - kServoPulseAt0Us;
  return static_cast<uint16_t>(kServoPulseAt0Us + (static_cast<uint32_t>(angle) * span + 90) / 180);
}

}  // namespace scout
