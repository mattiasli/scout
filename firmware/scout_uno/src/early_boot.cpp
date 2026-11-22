// Runs from the .init3 section: after the C runtime has set up the stack and the zero
// register, but before RAM is initialised, before any constructor and before setup().
//
// 1. Motor driver outputs off. OE# (D7) has a 10k pull-up on the shield, so the 74HC595
//    outputs are already disabled while the pins float; driving D7 high keeps them that
//    way until scout::Scout::begin() has latched zeros.
// 2. Read and clear MCUSR, then disable the watchdog. After a watchdog reset the WDT stays
//    on with its shortest timeout, so it must be switched off before anything slow runs or
//    the chip would reset again and again (optiboot does this too, older bootloaders don't).

#include "early_boot.h"

#include <avr/io.h>
#include <avr/wdt.h>

namespace {
uint8_t savedResetFlags __attribute__((section(".noinit")));
}

extern "C" void scoutEarlyBoot() __attribute__((naked, used, section(".init3")));

extern "C" void scoutEarlyBoot() {
  PORTD |= _BV(PORTD7);  // OE# high first ...
  DDRD |= _BV(DDD7);     // ... then make it an output
  savedResetFlags = MCUSR;
  MCUSR = 0;
  wdt_disable();
}

uint8_t resetFlags() { return savedResetFlags; }
