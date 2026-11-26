// Compile-time settings for the Scout UNO firmware. Plain C++11 with no Arduino headers,
// so the host tests see exactly the values the board uses.

#ifndef SCOUT_UNO_CONFIG_H
#define SCOUT_UNO_CONFIG_H

#include <stdint.h>

// PWM wiring. The shield has two speed pins (D5, D6) for four wheels. Measured on the
// AD176 (`tools/bench.py pwmmap`): D5 drives the two right wheels and D6 the two left ones.
//   0: shared. Both pins carry the same duty, that of the fastest wheel.
//   1: split. Each pin drives one side of the car; SCOUT_PWM5_LEFT says which side D5 is.
#ifndef SCOUT_PWM_SPLIT
#define SCOUT_PWM_SPLIT 1
#endif
#ifndef SCOUT_PWM5_LEFT
#define SCOUT_PWM5_LEFT 0  // only used when SCOUT_PWM_SPLIT is 1: 1 = D5 left, 0 = D5 right
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

// Tilt servo (KS-9GMS, SG90 class). Servo angle = kServoLevelAngle + tilt; a lower angle
// tilts the camera down. On the AD176 the camera points about 9 degrees up at servo angle
// 90, so level is 81 (measured on the bench).
const int8_t kTiltMinDeg = -40;
const int8_t kTiltMaxDeg = 40;
const int16_t kServoLevelAngle = 81;
// Pulse widths for 0 and 180 degrees: the same mapping as the Arduino Servo library, so
// "90" puts the horn where the vendor's sketches put it.
const uint16_t kServoPulseAt0Us = 544;
const uint16_t kServoPulseAt180Us = 2400;

// HC-SR04 ultrasonic sensor.
const uint16_t kRangePeriodMs = 60;  // time between pings; an echo not back by then is a timeout
const uint16_t kUsPerCm = 58;        // echo width per cm of distance (out and back)
const int16_t kRangeMaxCm = 400;     // longer readings are implausible and discarded
const uint16_t kRangeHoldMs = 200;   // a reading older than this (only discards since) becomes -1

// VL53L1X laser distance sensor (laser.h). Each measurement has a 100 ms timing budget
// in long distance mode, so measurements can't come more often than that.
const uint16_t kLaserPeriodMs = 100;  // time between measurements
const uint16_t kLaserPollMs = 10;     // how often to ask whether a measurement is ready
const uint16_t kLaserHoldMs = 250;    // a reading older than this (only failed ones since) becomes -1
const uint16_t kLaserStallMs = 500;   // no measurement for this long: start the sensor again
const uint16_t kLaserRetryMs = 2000;  // time between looks for a missing sensor
const uint8_t kLaserMaxFailures = 3;  // failed exchanges in a row before the sensor counts as gone

}  // namespace scout

#endif  // SCOUT_UNO_CONFIG_H
