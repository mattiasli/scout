// WebSocket framing (RFC 6455, section 5) for the client side of the car link.
//
// The car only ever sends masked frames and only ever receives unmasked ones. No
// extensions are negotiated, so any RSV bit is a protocol error.

#ifndef SCOUT_CORE_WS_CODEC_H
#define SCOUT_CORE_WS_CODEC_H

#include <stddef.h>
#include <stdint.h>

namespace scout {
namespace ws {

enum Opcode : uint8_t {
  kOpContinuation = 0x0,
  kOpText = 0x1,
  kOpBinary = 0x2,
  kOpClose = 0x8,
  kOpPing = 0x9,
  kOpPong = 0xA,
};

// Close codes used by the car.
const uint16_t kCloseNormal = 1000;
const uint16_t kCloseGoingAway = 1001;
const uint16_t kCloseProtocolError = 1002;
const uint16_t kCloseTooBig = 1009;
const uint16_t kCloseNoStatus = 1005;  // reported when a close frame carries no code

const size_t kMaxHeader = 14;       // 2 + 8 (64-bit length) + 4 (mask key)
const size_t kMaxControlPayload = 125;

// Writes a masked client frame header into `out` (at least kMaxHeader bytes) and
// returns its length.
size_t encodeHeader(uint8_t* out, uint8_t opcode, bool fin, uint64_t payloadLen, const uint8_t mask[4]);

// XORs `len` bytes with the mask key. `offset` is the position of data[0] within the
// frame payload, so a long payload can be masked in pieces.
void applyMask(uint8_t* data, size_t len, const uint8_t mask[4], size_t offset);

// Copies `len` bytes from src to dst, masking them on the way.
void maskCopy(uint8_t* dst, const uint8_t* src, size_t len, const uint8_t mask[4], size_t offset);

// Incremental parser for frames from the server. Data messages (possibly fragmented)
// are collected into the caller's buffer up to `capacity` bytes; anything longer is an
// error, so the car never holds more than that. Control frames may arrive between the
// fragments of a message without disturbing it.
class FrameParser {
 public:
  enum Event { kNone, kText, kBinary, kPing, kPong, kClose, kError };
  enum Error { kOk, kProtocol, kTooBig };

  // `buffer` must hold capacity + 1 bytes: a complete text message is NUL-terminated.
  FrameParser(uint8_t* buffer, size_t capacity);

  // Consumes bytes until one event is complete (or the input runs out). *consumed says
  // how many bytes were used; call again with the rest. After kError every call
  // returns kError until reset().
  Event feed(const uint8_t* data, size_t len, size_t* consumed);

  // The message (kText, kBinary) or the control payload (kPing, kPong, kClose).
  const uint8_t* data() const;
  size_t size() const;

  uint16_t closeCode() const { return closeCode_; }
  Error error() const { return error_; }
  // The close code the car should send back after an error.
  uint16_t errorCloseCode() const { return error_ == kTooBig ? kCloseTooBig : kCloseProtocolError; }

  void reset();

 private:
  enum State { kHead0, kHead1, kExtLen, kPayload, kFailed };
  Event fail(Error error);
  Event headerDone();
  Event frameDone();

  uint8_t* msg_;
  size_t capacity_;
  size_t msgLen_;
  bool inMessage_;       // a fragmented data message is in progress
  uint8_t msgOpcode_;
  bool messageReady_;    // the previous event delivered the message; clear it on next feed

  State state_;
  bool fin_;
  uint8_t opcode_;
  uint8_t extNeeded_;
  uint8_t extGot_;
  uint64_t frameLen_;
  uint64_t frameGot_;

  uint8_t ctrl_[kMaxControlPayload + 1];
  size_t ctrlLen_;
  Event lastEvent_;
  uint16_t closeCode_;
  Error error_;
};

}  // namespace ws
}  // namespace scout

#endif  // SCOUT_CORE_WS_CODEC_H
