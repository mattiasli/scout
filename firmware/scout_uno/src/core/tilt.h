// Camera tilt: degrees from level (positive = up) -> servo pulse width.

#ifndef SCOUT_UNO_TILT_H
#define SCOUT_UNO_TILT_H

#include <stdint.h>

namespace scout {

// Clamps a requested tilt to kTiltMinDeg..kTiltMaxDeg.
int8_t clampTilt(int32_t deg);

// Servo pulse in microseconds for a tilt already within range (servo angle = 90 + deg).
uint16_t tiltPulseUs(int8_t deg);

}  // namespace scout

#endif  // SCOUT_UNO_TILT_H
