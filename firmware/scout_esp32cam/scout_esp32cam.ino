// Scout: ESP32-CAM firmware for the KUONGSHUN AD176 mecanum car.
//
// Joins WiFi, keeps one WebSocket open to the Scout server and forwards its requests to
// the UNO over the serial link, sends camera pictures, and runs setup mode when there
// is no usable WiFi. Everything lives in src/; see README.md.

#include "src/app.h"

// Keep a new firmware image "pending" after an update until it has reached the Scout
// server (see src/ota.h). Without this the core would confirm it straight away. The
// core declares it in C, hence extern "C".
extern "C" bool verifyRollbackLater() { return true; }

void setup() { scout::appSetup(); }

void loop() { scout::appLoop(); }
