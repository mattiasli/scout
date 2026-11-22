// Compile-time settings for the Scout UNO firmware. Plain C++11 with no Arduino headers,
// so the host tests see exactly the values the board uses.

#ifndef SCOUT_UNO_CONFIG_H
#define SCOUT_UNO_CONFIG_H

#include <stdint.h>

// PWM wiring. The shield has two speed pins (D5, D6) for four wheels, and which wheels
// each pin drives hasn't been measured yet (run `tools/bench.py pwmmap`).
//   0: shared. Both pins carry the same duty, that of the fastest wheel.
//   1: split. Each pin drives one side of the car; SCOUT_PWM5_LEFT says which side D5 is.
#ifndef SCOUT_PWM_SPLIT
#define SCOUT_PWM_SPLIT 0
#endif
#ifndef SCOUT_PWM5_LEFT
#define SCOUT_PWM5_LEFT 1  // only used when SCOUT_PWM_SPLIT is 1: 1 = D5 left, 0 = D5 right
#endif

namespace scout {

const char kFirmwareVersion[] = "0.1.0";

// Timing.
const uint16_t kTelemetryPeriodMs = 200;

// MOVE and TEST limits (protocol/uart.md).
const int32_t kSpeedMax = 100;  // percent, for vx, vy and wz
const int32_t kMoveMaxMs = 5000;
const int32_t kTestMaxMs = 2000;

// CONF settings: defaults and allowed ranges. Settings live in RAM and reset at boot.
const uint8_t kObstacleDefaultCm = 15;
const int32_t kObstacleMinCm = 5;
const int32_t kObstacleMaxCm = 100;
const uint8_t kMinPwmDefault = 90;
const int32_t kMinPwmMin = 0;
const int32_t kMinPwmMax = 255;
const uint16_t kLinkDefaultMs = 1000;
const int32_t kLinkMinMs = 300;
const int32_t kLinkMaxMs = 5000;

// A wheel whose speed is below 1/kCoastDivisor of the fastest wheel on its PWM pin is switched off
// (both direction lines low; with the driver enabled that brakes it). 4 = 25 %.
const uint8_t kCoastDivisor = 4;

// Tilt servo (KS-9GMS, SG90 class). Servo angle = 90 + tilt; 90 is level and a lower
// angle tilts the camera down. The mechanical limits haven't been checked on the car,
// so the range is kept conservative.
const int8_t kTiltMinDeg = -40;
const int8_t kTiltMaxDeg = 40;
const int16_t kServoLevelAngle = 90;
// Pulse widths for 0 and 180 degrees: the same mapping as the Arduino Servo library, so
// "90" puts the horn where the vendor's sketches put it.
const uint16_t kServoPulseAt0Us = 544;
const uint16_t kServoPulseAt180Us = 2400;

// HC-SR04 ultrasonic sensor.
const uint16_t kRangePeriodMs = 60;  // time between pings; an echo not back by then is a timeout
const uint16_t kUsPerCm = 58;        // echo width per cm of distance (out and back)
const int16_t kRangeMaxCm = 400;     // longer readings are implausible and discarded
const uint16_t kRangeHoldMs = 200;   // a reading older than this (only discards since) becomes -1

}  // namespace scout

#endif  // SCOUT_UNO_CONFIG_H
