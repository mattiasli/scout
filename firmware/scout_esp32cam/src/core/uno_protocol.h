// The host side of the serial link to the UNO (protocol/uart.md): a copyable frame,
// parsers for the UNO's messages, and the mapping of UNO words to car-link words.

#ifndef SCOUT_CORE_UNO_PROTOCOL_H
#define SCOUT_CORE_UNO_PROTOCOL_H

#include <stddef.h>
#include <stdint.h>

#include "ScoutLink.h"

namespace scout {

// A received frame, copied out of the ScoutLink parser so it can be queued from the
// serial task to the main loop.
class UnoFrame {
 public:
  UnoFrame();
  bool assign(const scoutlink::Parser& parser);

  const char* type() const { return text_; }
  uint16_t seq() const { return seq_; }
  uint8_t argCount() const { return argc_; }
  const char* arg(uint8_t index) const;            // "" when out of range
  bool argInt(uint8_t index, int32_t* out) const;  // false when missing or not an integer
  bool is(const char* type) const;

 private:
  char text_[scoutlink::kMaxBody + 2];  // TYPE, NUL, then each arg and its NUL
  uint8_t argOffset_[scoutlink::kMaxArgs];
  uint8_t argc_;
  uint16_t seq_;
};

// Host request numbers: 1..65535, wrapping past 65535 back to 1 (never 0).
class SeqCounter {
 public:
  SeqCounter() : last_(0) {}
  explicit SeqCounter(uint16_t last) : last_(last) {}
  uint16_t next() {
    last_ = (last_ == 65535) ? 1 : static_cast<uint16_t>(last_ + 1);
    return last_;
  }

 private:
  uint16_t last_;
};

// TEL,0,<cm>,<line_l>,<line_c>,<line_r>,<tilt>,<move_seq>,<rx_errors>[,<laser>]
struct UnoTelemetry {
  int32_t cm;  // -1 = no echo
  int32_t line[3];
  int32_t tilt;
  uint16_t moveSeq;  // 0 = nothing running
  uint32_t rxErrors;
  bool hasLaser;  // false when the UNO firmware is older than the laser sensor
  int32_t laser;  // cm; -1 no reading, -2 no sensor answers, -3 not a VL53L1X
};
bool parseTelemetry(const UnoFrame& frame, UnoTelemetry* out);

// DONE,seq,<reason>,<cm_start>,<cm_end>,<ms_ran>
struct UnoDone {
  const char* reason;  // points into the frame
  int32_t cmStart;
  int32_t cmEnd;
  int32_t ms;
};
bool parseDone(const UnoFrame& frame, UnoDone* out);

// UART DONE reason -> car-link done reason. Anything unknown becomes "error".
const char* doneReasonFromUno(const char* reason);

// UART ERR code -> car-link err code. RANGE and ARGS mean the request was out of range
// ("args"); TYPE or anything else means the UNO didn't understand us ("uno").
const char* errCodeFromUno(const char* code);

}  // namespace scout

#endif  // SCOUT_CORE_UNO_PROTOCOL_H
