// The real board: ATmega328P with the UNO-Cam-Shield-V01. board.cpp also defines the
// scout::hw functions from core/hardware.h.

#ifndef SCOUT_UNO_BOARD_H
#define SCOUT_UNO_BOARD_H

// Configures the pins, Timer1 (servo), the echo interrupt and the serial port. Leaves the
// 74HC595 outputs disabled; scout::Scout::begin() zeroes and then enables them.
void boardBegin();

#endif  // SCOUT_UNO_BOARD_H
