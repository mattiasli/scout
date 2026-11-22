// Framing for the serial link between the UNO and its host (the ESP32-CAM, or a PC for
// bench tests). The format is described in protocol/uart.md.
//
// Plain C++11 with no Arduino or STL dependencies, so the same code builds for the
// ATmega328P, the ESP32 and the host-side unit tests.

#ifndef SCOUTLINK_H
#define SCOUTLINK_H

#include <stddef.h>
#include <stdint.h>

namespace scoutlink {

const uint8_t kMaxBody = 64;            // bytes between '$' and '*'
const uint8_t kMaxArgs = 8;             // fields after TYPE and seq
const uint8_t kMaxFrame = kMaxBody + 6; // '$' body '*' CC '\n' and a terminating NUL

// CRC-8/SMBUS (poly 0x07, init 0, no reflection, no final XOR).
uint8_t crc8(const uint8_t* data, size_t len);

// Strict decimal parse: optional '-', no '+', no leading zeros, must fit in int32.
bool parseInt(const char* text, int32_t* out);

// Builds one frame:  Builder b("DONE", 12); b.add("OBST").add(40); b.finish(buf, sizeof buf);
// finish() writes "$DONE,12,OBST,40*CC\n" plus a NUL and returns the length without the
// NUL, or 0 if anything was invalid or didn't fit.
class Builder {
 public:
  Builder(const char* type, uint16_t seq);
  Builder& add(int32_t value);
  Builder& add(const char* text);
  size_t finish(char* out, size_t cap) const;

 private:
  void append(char c);
  void appendText(const char* text);
  char body_[kMaxBody + 1];
  uint8_t len_;
  uint8_t args_;
  bool ok_;
};

// Streaming parser. Feed it every received byte. feed() returns true when a complete,
// CRC-valid, well-formed frame has arrived; its fields stay readable until the next
// call to feed(). Everything else is skipped and invalid frames are counted.
class Parser {
 public:
  Parser();
  bool feed(uint8_t byte);

  const char* type() const { return fields_[0]; }
  uint16_t seq() const { return seq_; }
  uint8_t argCount() const { return argc_; }
  const char* arg(uint8_t index) const;                // "" when out of range
  bool argInt(uint8_t index, int32_t* out) const;      // false when missing or not an integer
  bool argIs(uint8_t index, const char* text) const;   // exact string match
  bool is(const char* type) const;                     // exact TYPE match
  uint32_t rxErrors() const { return rxErrors_; }

 private:
  enum State { kIdle, kBody, kCrc };
  bool split();

  State state_;
  char buf_[kMaxBody + 1];
  uint8_t len_;
  uint8_t crcDigits_;
  uint8_t crcValue_;
  const char* fields_[2 + kMaxArgs];
  uint8_t argc_;
  uint16_t seq_;
  uint32_t rxErrors_;
};

}  // namespace scoutlink

#endif  // SCOUTLINK_H
