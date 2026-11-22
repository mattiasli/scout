// The earliest code after reset (see early_boot.cpp).

#ifndef SCOUT_UNO_EARLY_BOOT_H
#define SCOUT_UNO_EARLY_BOOT_H

#include <stdint.h>

// MCUSR as it was at reset, before it was cleared. Note that the stock UNO bootloader
// (optiboot 4.4) clears MCUSR itself before starting the sketch, so with it this is
// usually 0; a board without a bootloader, or with a newer one, reports the real flags.
uint8_t resetFlags();

#endif  // SCOUT_UNO_EARLY_BOOT_H
