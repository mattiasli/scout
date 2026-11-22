#include "ws_handshake.h"

#include <stdio.h>
#include <string.h>

#include "sha1.h"

namespace scout {
namespace ws {

namespace {

const char kGuid[] = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

char lower(char c) { return (c >= 'A' && c <= 'Z') ? static_cast<char>(c - 'A' + 'a') : c; }

bool equalsIgnoreCase(const char* a, const char* b) {
  for (; *a != '\0' && *b != '\0'; ++a, ++b) {
    if (lower(*a) != lower(*b)) return false;
  }
  return *a == *b;
}

bool startsWithIgnoreCase(const char* text, const char* prefix) {
  for (; *prefix != '\0'; ++text, ++prefix) {
    if (*text == '\0' || lower(*text) != lower(*prefix)) return false;
  }
  return true;
}

// Visible ASCII only: what can safely go into a request line or header value.
bool headerSafe(const char* text) {
  for (; *text != '\0'; ++text) {
    if (*text < 0x21 || *text > 0x7E) return false;
  }
  return true;
}

class Writer {
 public:
  Writer(char* out, size_t cap) : out_(out), cap_(cap), len_(0), ok_(cap > 0) {
    if (ok_) out_[0] = '\0';
  }
  Writer& add(const char* text) {
    for (; ok_ && *text != '\0'; ++text) {
      if (len_ + 1 >= cap_) {
        ok_ = false;
        break;
      }
      out_[len_++] = *text;
    }
    if (ok_) out_[len_] = '\0';
    return *this;
  }
  Writer& add(unsigned value) {
    char digits[12];
    snprintf(digits, sizeof digits, "%u", value);
    return add(digits);
  }
  size_t finish() const { return ok_ ? len_ : 0; }

 private:
  char* out_;
  size_t cap_;
  size_t len_;
  bool ok_;
};

bool hostChar(char c) {
  return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '.' || c == '-' ||
         c == '_';
}

}  // namespace

bool makeKey(const uint8_t nonce[16], char out[25]) { return base64Encode(nonce, 16, out, 25) == 24; }

bool acceptFor(const char* key, char out[29]) {
  Sha1 hash;
  hash.update(reinterpret_cast<const uint8_t*>(key), strlen(key));
  hash.update(reinterpret_cast<const uint8_t*>(kGuid), sizeof kGuid - 1);
  uint8_t digest[20];
  hash.finish(digest);
  return base64Encode(digest, sizeof digest, out, 29) == 28;
}

size_t buildRequest(char* out, size_t cap, const RequestParams& p) {
  if (p.host == nullptr || p.path == nullptr || p.key == nullptr || p.firmware == nullptr) return 0;
  if (p.host[0] == '\0' || p.path[0] != '/') return 0;
  if (!headerSafe(p.host) || !headerSafe(p.path) || !headerSafe(p.key) || !headerSafe(p.firmware)) return 0;
  bool withAuth = p.bearer != nullptr && p.bearer[0] != '\0';
  if (withAuth && !headerSafe(p.bearer)) return 0;

  Writer w(out, cap);
  w.add("GET ").add(p.path).add(" HTTP/1.1\r\n");
  w.add("Host: ").add(p.host);
  if (p.port != (p.secure ? 443 : 80)) w.add(":").add(static_cast<unsigned>(p.port));
  w.add("\r\n");
  w.add("Upgrade: websocket\r\n");
  w.add("Connection: Upgrade\r\n");
  w.add("Sec-WebSocket-Key: ").add(p.key).add("\r\n");
  w.add("Sec-WebSocket-Version: 13\r\n");
  if (withAuth) w.add("Authorization: Bearer ").add(p.bearer).add("\r\n");
  w.add("X-Scout-Firmware: ").add(p.firmware).add("\r\n");
  w.add("User-Agent: scout-car/").add(p.firmware).add("\r\n");
  w.add("\r\n");
  return w.finish();
}

// ---------------------------------------------------------------- ResponseParser

ResponseParser::ResponseParser(const char* expectedAccept)
    : lineLen_(0),
      lineTruncated_(false),
      total_(0),
      sawStatus_(false),
      done_(false),
      malformed_(false),
      status_(0),
      upgrade_(false),
      connection_(false),
      accept_(false),
      sawAccept_(false) {
  strncpy(expected_, expectedAccept, sizeof expected_ - 1);
  expected_[sizeof expected_ - 1] = '\0';
  line_[0] = '\0';
}

ResponseParser::Result ResponseParser::feed(uint8_t byte) {
  if (done_) return kDone;
  if (malformed_) return kError;
  if (++total_ > kMaxHeaderBytes) {
    malformed_ = true;
    return kError;
  }
  if (byte != '\n') {
    if (lineLen_ + 1 < sizeof line_) {
      line_[lineLen_++] = static_cast<char>(byte);
    } else {
      lineTruncated_ = true;
    }
    return kMore;
  }
  if (lineLen_ > 0 && line_[lineLen_ - 1] == '\r') --lineLen_;
  line_[lineLen_] = '\0';
  if (lineLen_ == 0 && !lineTruncated_) {
    if (!sawStatus_) {
      malformed_ = true;
      return kError;
    }
    done_ = true;
    return kDone;
  }
  line();
  lineLen_ = 0;
  lineTruncated_ = false;
  return malformed_ ? kError : kMore;
}

void ResponseParser::line() {
  if (!sawStatus_) {
    // "HTTP/1.1 101 Switching Protocols"
    if (!startsWithIgnoreCase(line_, "HTTP/1.") || lineLen_ < 12 || line_[8] != ' ') {
      malformed_ = true;
      return;
    }
    int code = 0;
    for (int i = 9; i < 12; ++i) {
      if (line_[i] < '0' || line_[i] > '9') {
        malformed_ = true;
        return;
      }
      code = code * 10 + (line_[i] - '0');
    }
    status_ = code;
    sawStatus_ = true;
    return;
  }
  if (lineTruncated_) return;  // none of the headers we check are that long
  char* colon = strchr(line_, ':');
  if (colon == nullptr) return;
  *colon = '\0';
  char* value = colon + 1;
  while (*value == ' ' || *value == '\t') ++value;
  char* end = value + strlen(value);
  while (end > value && (end[-1] == ' ' || end[-1] == '\t')) *--end = '\0';

  if (equalsIgnoreCase(line_, "upgrade")) {
    upgrade_ = equalsIgnoreCase(value, "websocket");
  } else if (equalsIgnoreCase(line_, "connection")) {
    // A token list such as "keep-alive, Upgrade".
    char* token = value;
    while (token != nullptr) {
      char* comma = strchr(token, ',');
      if (comma != nullptr) *comma = '\0';
      while (*token == ' ' || *token == '\t') ++token;
      char* tokenEnd = token + strlen(token);
      while (tokenEnd > token && (tokenEnd[-1] == ' ' || tokenEnd[-1] == '\t')) *--tokenEnd = '\0';
      if (equalsIgnoreCase(token, "upgrade")) connection_ = true;
      token = comma != nullptr ? comma + 1 : nullptr;
    }
  } else if (equalsIgnoreCase(line_, "sec-websocket-accept")) {
    sawAccept_ = true;
    accept_ = strcmp(value, expected_) == 0;
  }
}

bool ResponseParser::accepted() const {
  return done_ && !malformed_ && status_ == 101 && upgrade_ && connection_ && accept_;
}

const char* ResponseParser::failure() const {
  if (malformed_) return "malformed response";
  if (!done_) return "incomplete response";
  if (status_ == 401 || status_ == 403) return "car key refused";
  if (status_ != 101) return "server did not upgrade";
  if (!upgrade_ || !connection_) return "missing upgrade headers";
  if (!sawAccept_) return "missing accept header";
  if (!accept_) return "wrong accept header";
  return "";
}

// ---------------------------------------------------------------- ServerUrl

bool parseServerUrl(const char* text, ServerUrl* out) {
  if (text == nullptr) return false;
  while (*text == ' ' || *text == '\t') ++text;

  bool secure;
  if (startsWithIgnoreCase(text, "wss://")) {
    secure = true;
    text += 6;
  } else if (startsWithIgnoreCase(text, "https://")) {
    secure = true;
    text += 8;
  } else if (startsWithIgnoreCase(text, "ws://")) {
    secure = false;
    text += 5;
  } else if (startsWithIgnoreCase(text, "http://")) {
    secure = false;
    text += 7;
  } else {
    return false;
  }

  size_t hostLen = 0;
  while (hostChar(text[hostLen])) ++hostLen;
  if (hostLen == 0 || hostLen >= sizeof out->host) return false;
  const char* rest = text + hostLen;

  uint32_t port = secure ? 443 : 80;
  if (*rest == ':') {
    ++rest;
    uint32_t value = 0;
    size_t digits = 0;
    while (*rest >= '0' && *rest <= '9') {
      value = value * 10 + static_cast<uint32_t>(*rest - '0');
      if (++digits > 5) return false;
      ++rest;
    }
    if (digits == 0 || value == 0 || value > 65535) return false;
    port = value;
  }

  // Trailing whitespace is forgiven; anything else must be a path or a query.
  const char* end = rest + strlen(rest);
  while (end > rest && (end[-1] == ' ' || end[-1] == '\t' || end[-1] == '\r' || end[-1] == '\n')) --end;
  size_t pathLen = static_cast<size_t>(end - rest);
  if (pathLen > 0 && rest[0] != '/' && rest[0] != '?') return false;
  for (size_t i = 0; i < pathLen; ++i) {
    if (rest[i] < 0x21 || rest[i] > 0x7E || rest[i] == '#') return false;
  }

  out->secure = secure;
  memcpy(out->host, text, hostLen);
  out->host[hostLen] = '\0';
  out->port = static_cast<uint16_t>(port);
  // No path (or just "/") means the standard car endpoint; "?x" becomes "/car/ws?x".
  static const char kDefaultPath[] = "/car/ws";
  size_t prefixLen = 0;
  if (pathLen == 0 || (pathLen == 1 && rest[0] == '/')) {
    pathLen = 0;
    prefixLen = sizeof kDefaultPath - 1;
  } else if (rest[0] == '?') {
    prefixLen = sizeof kDefaultPath - 1;
  }
  if (prefixLen + pathLen >= sizeof out->path) return false;
  memcpy(out->path, kDefaultPath, prefixLen);
  memcpy(out->path + prefixLen, rest, pathLen);
  out->path[prefixLen + pathLen] = '\0';
  return true;
}

}  // namespace ws
}  // namespace scout
