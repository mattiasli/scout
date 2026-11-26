// Pin map of the "UNO-Cam-Shield-V01" on the AD176 car.

#ifndef SCOUT_UNO_PINS_H
#define SCOUT_UNO_PINS_H

#include <stdint.h>

const uint8_t kPinShiftClock = 2;    // 74HC595 SHCP
const uint8_t kPinLatch = 4;         // 74HC595 STCP
const uint8_t kPinPwm5 = 5;          // speed PWM (Timer0, OC0B)
const uint8_t kPinPwm6 = 6;          // speed PWM (Timer0, OC0A)
const uint8_t kPinOutputEnable = 7;  // 74HC595 OE#, active low, 10k pull-up on the shield
const uint8_t kPinShiftData = 8;     // 74HC595 DS
const uint8_t kPinServo = 9;         // tilt servo on OC1A; Timer1 only, never digitalWrite()
const uint8_t kPinTrigger = 12;      // HC-SR04 trig
const uint8_t kPinEcho = 13;         // HC-SR04 echo: PB5, pin-change interrupt PCINT5
// Line sensors: A0, A1, A2 (left, centre, right according to one vendor sketch; unverified).
// A4/A5: I2C (SDA, SCL) to the VL53L1X laser distance sensor. The shield labels that
// header for a gyroscope; any other I2C device there needs an address other than 0x29.
// Spare: D10, D11. Keep free: A3 (battery sensing).

#endif  // SCOUT_UNO_PINS_H
