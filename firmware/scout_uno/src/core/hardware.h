// The hardware the board-independent logic drives.
//
// These are plain functions resolved at link time: src/board.cpp defines them for the
// real board, and the host tests define them with a fake (firmware/tests/uno). No virtual
// calls, so nothing extra in RAM on the UNO.

#ifndef SCOUT_UNO_HARDWARE_H
#define SCOUT_UNO_HARDWARE_H

#include <stdint.h>

namespace scout {
namespace hw {

// 74HC595 output enable (OE#, D7). false = outputs disabled, so every L293DD input line
// is released and no motor can be driven.
void setDriverEnabled(bool enabled);

// Shifts `bits` into the 74HC595, MSB first, and latches it onto the outputs.
void writeShiftRegister(uint8_t bits);

// Speed PWM duty on D5 and D6, 0..255.
void writePwm(uint8_t pwm5, uint8_t pwm6);

// Tilt servo pulse width in microseconds (the pulse repeats every 20 ms).
void writeServo(uint16_t pulseUs);

// Sends an ultrasonic trigger pulse and arms the echo timer. Any echo still being timed
// from an earlier trigger is forgotten.
void triggerRanger();

// True once, when the echo of the last trigger has ended. *widthUs = echo pulse width.
bool takeEcho(uint32_t* widthUs);

// Raw readings of the three line sensors on A0, A1 and A2 (0..1023).
void readLine(uint16_t* a0, uint16_t* a1, uint16_t* a2);

// I2C exchanges with the laser distance sensor (VL53L1X at address 0x29). Register
// addresses are 16 bits. laserWrite() writes `length` bytes (at most 16) to the registers
// from `reg` on; laserRead() reads `length` bytes from `reg` on. Both return false when
// the sensor doesn't acknowledge or the bus times out.
bool laserWrite(uint16_t reg, const uint8_t* data, uint8_t length);
bool laserRead(uint16_t reg, uint8_t* data, uint8_t length);

// Writes bytes to the host.
void send(const char* data, uint8_t length);

}  // namespace hw
}  // namespace scout

#endif  // SCOUT_UNO_HARDWARE_H
