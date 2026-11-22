// Scout UNO firmware: the motors, the tilt servo and the sensors of the AD176 mecanum car.
//
// The serial protocol is protocol/uart.md. All decisions are made in src/core (built and
// tested on a PC as well); this file and the rest of src/ only connect it to the board.
// See README.md for building, flashing and the safety rules.

#include <ScoutLink.h>
#include <avr/wdt.h>

#include "src/board.h"
#include "src/core/scout.h"
#include "src/early_boot.h"

namespace {

// If loop() stops running for this long, the AVR watchdog resets the chip and the boot
// sequence turns the motors off.
const uint8_t kWatchdogTimeout = WDTO_250MS;

scout::Scout car;

}  // namespace

void setup() {
  boardBegin();                        // pins, servo timer, echo interrupt, serial
  car.begin(millis(), resetFlags());   // motors provably off, then BOOT
  wdt_enable(kWatchdogTimeout);
}

void loop() {
  wdt_reset();
  const uint32_t now = millis();
  for (int pending = Serial.available(); pending > 0; --pending) {
    car.receive(static_cast<uint8_t>(Serial.read()), now);
  }
  car.update(millis());
}
