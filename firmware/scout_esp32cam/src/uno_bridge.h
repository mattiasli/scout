// The serial link to the UNO (protocol/uart.md), host side.
//
// A small FreeRTOS task owns UART0: it PINGs the UNO every 250 ms and parses everything
// that comes back. Running this outside the main loop means a slow network write (a big
// picture over weak WiFi, a TLS reconnect) never delays the PINGs, so the UNO's link
// watchdog doesn't cut a move that is still wanted.
//
// Telemetry, the UNO's firmware version and "is it answering" are kept as state. Replies
// (ACK, ERR, DONE) and BOOT go through a queue to the main loop, in order.
//
// Nothing else may write to UART0: the UNO reads every byte on it.

#ifndef SCOUT_UNO_BRIDGE_H
#define SCOUT_UNO_BRIDGE_H

#include <Arduino.h>

#include "core/uno_protocol.h"

namespace scout {

class UnoBridge {
 public:
  // Creates the queue and lock. Serial must already be open. Until startPinging()
  // nothing is sent except what the caller sends (setup mode never starts it).
  bool begin();
  bool startPinging();

  // Before startPinging() only (it reads UART0 on the caller's task): PINGs the UNO and
  // waits up to `waitMs` for its uptime in seconds, from PONG. A BOOT on the way means it
  // has only just started: 0. False when it doesn't answer.
  bool askUptime(uint32_t waitMs, uint32_t* uptimeS);

  // Sends one request and returns the seq it used, or 0 if it couldn't be built.
  uint16_t send(const char* type, const int32_t* args, uint8_t argCount);
  uint16_t sendConf(const char* key, int32_t value);
  uint16_t sendStop() { return send("STOP", nullptr, 0); }

  // An ACK, ERR, DONE or BOOT frame, oldest first. False when none is waiting.
  bool takeFrame(UnoFrame* out);

  bool present() const;  // a valid frame arrived within the last second
  // The latest TEL, if one arrived within the last second.
  bool telemetry(UnoTelemetry* out) const;
  // The version from the last PONG or BOOT; false until the UNO answered once.
  bool firmware(char* out, size_t cap) const;

  uint32_t droppedFrames() const { return dropped_; }
  uint32_t rxErrors() const { return parser_.rxErrors(); }

 private:
  static void taskEntry(void* self);
  void run();
  void handle(const UnoFrame& frame);
  uint16_t transmit(const scoutlink::Builder& builder, uint16_t seq);  // caller holds txLock_

  SemaphoreHandle_t txLock_ = nullptr;
  QueueHandle_t queue_ = nullptr;
  mutable portMUX_TYPE stateLock_ = portMUX_INITIALIZER_UNLOCKED;
  SeqCounter seq_;
  scoutlink::Parser parser_;
  UnoFrame frame_;
  volatile uint32_t lastRxMs_ = 0;
  volatile bool everRx_ = false;
  volatile uint32_t dropped_ = 0;
  UnoTelemetry tel_ = {};
  uint32_t telMs_ = 0;
  bool haveTel_ = false;
  char fw_[16] = "";
};

}  // namespace scout

#endif  // SCOUT_UNO_BRIDGE_H
