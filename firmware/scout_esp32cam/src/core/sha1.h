// SHA-1 (FIPS 180-4) and base64 (RFC 4648), just enough for the WebSocket handshake.
//
// SHA-1 is only used to check the server's Sec-WebSocket-Accept header, which is what
// RFC 6455 prescribes. It is not used for anything that needs to be secure.

#ifndef SCOUT_CORE_SHA1_H
#define SCOUT_CORE_SHA1_H

#include <stddef.h>
#include <stdint.h>

namespace scout {

class Sha1 {
 public:
  Sha1();
  void update(const uint8_t* data, size_t len);
  void finish(uint8_t digest[20]);

 private:
  void block(const uint8_t* chunk);
  uint32_t h_[5];
  uint8_t buf_[64];
  size_t used_;
  uint64_t total_;
};

void sha1(const uint8_t* data, size_t len, uint8_t digest[20]);

// Writes the base64 text of `len` bytes plus a NUL. Returns the text length, or 0 if
// `cap` is too small (it needs 4 * ceil(len / 3) + 1 bytes).
size_t base64Encode(const uint8_t* data, size_t len, char* out, size_t cap);

}  // namespace scout

#endif  // SCOUT_CORE_SHA1_H
