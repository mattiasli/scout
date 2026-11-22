// The HTTP/1.1 upgrade that opens the car link (RFC 6455, section 4), plus parsing of
// the server address the owner enters on the setup page.

#ifndef SCOUT_CORE_WS_HANDSHAKE_H
#define SCOUT_CORE_WS_HANDSHAKE_H

#include <stddef.h>
#include <stdint.h>

namespace scout {
namespace ws {

// Sec-WebSocket-Key from 16 random bytes: 24 characters plus a NUL.
bool makeKey(const uint8_t nonce[16], char out[25]);

// The Sec-WebSocket-Accept value the server must answer for `key`: 28 characters + NUL.
bool acceptFor(const char* key, char out[29]);

struct RequestParams {
  const char* host;
  uint16_t port;
  bool secure;
  const char* path;      // starts with '/'
  const char* key;       // from makeKey()
  const char* bearer;    // car key, or nullptr/"" to leave out the Authorization header
  const char* firmware;  // X-Scout-Firmware
};

// Writes the upgrade request. Returns its length, or 0 if it doesn't fit or a field
// contains characters that can't go into a header (CR, LF, controls).
size_t buildRequest(char* out, size_t cap, const RequestParams& params);

// Reads the server's answer to the upgrade one byte at a time, so it stops exactly at
// the end of the headers and the bytes after it belong to the first WebSocket frame.
class ResponseParser {
 public:
  enum Result { kMore, kDone, kError };
  static const size_t kMaxHeaderBytes = 8192;

  explicit ResponseParser(const char* expectedAccept);
  Result feed(uint8_t byte);

  int status() const { return status_; }
  // True once kDone was returned and the response is a valid upgrade for our key.
  bool accepted() const;
  // A short reason when accepted() is false, for logs and the setup page.
  const char* failure() const;

 private:
  void line();
  char expected_[29];
  char line_[200];
  size_t lineLen_;
  bool lineTruncated_;
  size_t total_;
  bool sawStatus_;
  bool done_;
  bool malformed_;
  int status_;
  bool upgrade_;
  bool connection_;
  bool accept_;
  bool sawAccept_;
};

// A car-link address such as "wss://scout.example.com/car/ws".
struct ServerUrl {
  bool secure;
  char host[128];
  uint16_t port;
  char path[160];
};

// Accepts ws:// and wss:// (and http:// / https://, read as ws:// / wss://). The scheme
// is case-insensitive. A missing path, or "/", becomes "/car/ws". Returns false for
// anything else, including user info, IPv6 literals and spaces.
bool parseServerUrl(const char* text, ServerUrl* out);

}  // namespace ws
}  // namespace scout

#endif  // SCOUT_CORE_WS_HANDSHAKE_H
