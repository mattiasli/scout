// Mecanum mixing: (vx, vy, wz) in percent -> the 74HC595 direction byte and the two PWM
// duties. The formulas and the bit map are in protocol/uart.md.

#ifndef SCOUT_UNO_MECANUM_H
#define SCOUT_UNO_MECANUM_H

#include <stdint.h>

#include "config.h"

namespace scout {

// Wheel order used everywhere: M1 front-left, M2 rear-left, M3 front-right, M4 rear-right.
enum Wheel { kFrontLeft = 0, kRearLeft = 1, kFrontRight = 2, kRearRight = 3 };
const uint8_t kWheelCount = 4;

// Wheel sets, one bit per wheel (bit 0 = M1 ... bit 3 = M4).
const uint8_t kWheelsAll = 0x0F;
const uint8_t kWheelsLeft = (1 << kFrontLeft) | (1 << kRearLeft);
const uint8_t kWheelsRight = (1 << kFrontRight) | (1 << kRearRight);

// Which wheels each speed pin drives. A wheel in neither set can't be driven and coasts.
struct PwmLayout {
  uint8_t pwm5Wheels;
  uint8_t pwm6Wheels;
};

const PwmLayout kPwmShared = {kWheelsAll, kWheelsAll};
const PwmLayout kPwmLeftRight = {kWheelsLeft, kWheelsRight};
const PwmLayout kPwmRightLeft = {kWheelsRight, kWheelsLeft};

#if SCOUT_PWM_SPLIT
#if SCOUT_PWM5_LEFT
const PwmLayout kPwmLayout = kPwmLeftRight;
#else
const PwmLayout kPwmLayout = kPwmRightLeft;
#endif
#else
const PwmLayout kPwmLayout = kPwmShared;
#endif

struct MotorOutput {
  uint8_t bits;  // 74HC595 byte
  uint8_t pwm5;
  uint8_t pwm6;
};

const MotorOutput kMotorsOff = {0, 0, 0};

// 74HC595 bits that drive each wheel forward and back.
uint8_t forwardBit(uint8_t wheel);
uint8_t backBit(uint8_t wheel);

// Wheel speeds in percent (-100..100) for a move, indexed by Wheel. Inputs must be within
// -100..100. If any wheel would exceed 100 %, all four are scaled down together.
void mixWheels(int8_t vx, int8_t vy, int8_t wz, int8_t speeds[kWheelCount]);

// 0 -> 0; 1..100 -> minPwm..255, linearly.
uint8_t percentToPwm(uint8_t percent, uint8_t minPwm);

// The full mix. Each pin gets the duty of the fastest wheel it drives, and a wheel below
// 25 % of that wheel's speed coasts, because its pin can't run it any slower.
MotorOutput mecanumOutput(int8_t vx, int8_t vy, int8_t wz, uint8_t minPwm, const PwmLayout& layout);

}  // namespace scout

#endif  // SCOUT_UNO_MECANUM_H
