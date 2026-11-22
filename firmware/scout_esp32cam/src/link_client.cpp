#include "link_client.h"

#include <esp_random.h>

namespace scout {

LinkClient::LinkClient()
    : io_(nullptr),
      open_(false),
      lastHeardMs_(0),
      handler_(nullptr),
      context_(nullptr),
      parser_(rxMessage_, cfg::kLinkMaxMessageBytes) {
  closeReason_[0] = '\0';
}

void LinkClient::setHandler(TextHandler handler, void* context) {
  handler_ = handler;
  context_ = context;
}

void LinkClient::setReason(const char* reason) {
  strncpy(closeReason_, reason, sizeof closeReason_ - 1);
  closeReason_[sizeof closeReason_ - 1] = '\0';
}

bool LinkClient::connect(const ws::ServerUrl& url, const char* carKey, const char* firmware, char* why,
                         size_t whyCap) {
  drop();
  parser_.reset();
  closeReason_[0] = '\0';
  // Two steps in separate stack frames: the TLS handshake needs a deep stack, the
  // upgrade a few buffers, and the loop task only has 8 KB.
  if (!openTransport(url, why, whyCap)) return false;
  if (!upgrade(url, carKey, firmware, why, whyCap)) {
    drop();
    return false;
  }
  lastHeardMs_ = millis();
  return true;
}

bool LinkClient::openTransport(const ws::ServerUrl& url, char* why, size_t whyCap) {
  bool ok;
  if (url.secure) {
    // Check the server against the CA bundle compiled into the core (the Mozilla list
    // that ESP-IDF ships). Never setInsecure().
    tls_.useBuiltinCACertBundle();
    tls_.setHandshakeTimeout(cfg::kLinkTlsHandshakeTimeoutS);
    io_ = &tls_;
    ok = tls_.connect(url.host, url.port, static_cast<int32_t>(cfg::kLinkConnectTimeoutMs)) == 1;
    if (!ok) {
      char tlsError[64] = "";
      tls_.lastError(tlsError, sizeof tlsError);
      snprintf(why, whyCap, "TLS connect to %s failed: %s", url.host, tlsError[0] ? tlsError : "no answer");
    }
  } else {
    io_ = &tcp_;
    ok = tcp_.connect(url.host, url.port, static_cast<int32_t>(cfg::kLinkConnectTimeoutMs)) == 1;
    if (!ok) snprintf(why, whyCap, "TCP connect to %s:%u failed", url.host, static_cast<unsigned>(url.port));
  }
  if (!ok) {
    io_->stop();
    io_ = nullptr;
    return false;
  }
  io_->setNoDelay(true);  // small JSON replies shouldn't wait behind Nagle
  open_ = true;           // writeAll() needs it
  return true;
}

bool LinkClient::upgrade(const ws::ServerUrl& url, const char* carKey, const char* firmware, char* why,
                         size_t whyCap) {
  uint8_t nonce[16];
  esp_fill_random(nonce, sizeof nonce);
  char key[25];
  char accept[29];
  ws::makeKey(nonce, key);
  ws::acceptFor(key, accept);
  ws::RequestParams params;
  params.host = url.host;
  params.port = url.port;
  params.secure = url.secure;
  params.path = url.path;
  params.key = key;
  params.bearer = carKey;
  params.firmware = firmware;
  // The send buffer is free until the link is up.
  char* request = reinterpret_cast<char*>(tx_);
  size_t len = ws::buildRequest(request, sizeof tx_, params);
  if (len == 0) {
    snprintf(why, whyCap, "bad server address or car key");
    return false;
  }
  if (!writeAll(tx_, len)) {
    snprintf(why, whyCap, "sending the upgrade failed");
    return false;
  }

  // Read the answer byte by byte so nothing after the headers is swallowed.
  ws::ResponseParser response(accept);
  ws::ResponseParser::Result result = ws::ResponseParser::kMore;
  uint32_t start = millis();
  while (result == ws::ResponseParser::kMore && io_ != nullptr) {
    if (io_->available() > 0) {
      int byte = io_->read();
      if (byte >= 0) result = response.feed(static_cast<uint8_t>(byte));
      continue;
    }
    if (!io_->connected()) break;
    if (millis() - start > cfg::kLinkUpgradeTimeoutMs) break;
    delay(5);
  }
  if (response.accepted()) return true;
  if (response.status() != 0 && response.status() != 101) {
    snprintf(why, whyCap, "%s (HTTP %d)", response.failure(), response.status());
  } else {
    snprintf(why, whyCap, "%s", response.failure());
  }
  return false;
}

bool LinkClient::poll() {
  if (!open_) return false;
  size_t budget = cfg::kLinkReadBudgetBytes;
  while (open_ && budget > 0) {
    int avail = io_->available();
    if (avail <= 0) break;
    size_t want = sizeof rxChunk_;
    if (want > static_cast<size_t>(avail)) want = static_cast<size_t>(avail);
    if (want > budget) want = budget;
    int got = io_->read(rxChunk_, want);
    if (got <= 0) break;
    budget -= static_cast<size_t>(got);
    lastHeardMs_ = millis();
    size_t pos = 0;
    while (open_ && pos < static_cast<size_t>(got)) {
      size_t used = 0;
      ws::FrameParser::Event event = parser_.feed(rxChunk_ + pos, static_cast<size_t>(got) - pos, &used);
      pos += used;
      if (event != ws::FrameParser::kNone && !handleEvent(event)) break;
      if (used == 0) break;
    }
  }
  if (open_ && !io_->connected()) {
    setReason("connection lost");
    drop();
  }
  return open_;
}

bool LinkClient::handleEvent(ws::FrameParser::Event event) {
  switch (event) {
    case ws::FrameParser::kText:
      if (handler_ != nullptr) handler_(context_, reinterpret_cast<char*>(rxMessage_), parser_.size());
      return open_;
    case ws::FrameParser::kBinary:
    case ws::FrameParser::kPong:
    case ws::FrameParser::kNone:
      return true;
    case ws::FrameParser::kPing:
      return sendFrame(ws::kOpPong, nullptr, 0, parser_.data(), parser_.size());
    case ws::FrameParser::kClose: {
      uint16_t code = parser_.closeCode();
      char reason[48];
      snprintf(reason, sizeof reason, "server closed the link (%u)", static_cast<unsigned>(code));
      if (code == ws::kCloseNoStatus) {
        sendFrame(ws::kOpClose, nullptr, 0, nullptr, 0);
      } else {
        uint8_t payload[2] = {static_cast<uint8_t>(code >> 8), static_cast<uint8_t>(code)};
        sendFrame(ws::kOpClose, nullptr, 0, payload, 2);
      }
      drop();
      setReason(reason);
      return false;
    }
    case ws::FrameParser::kError: {
      bool tooBig = parser_.error() == ws::FrameParser::kTooBig;
      close(parser_.errorCloseCode());
      setReason(tooBig ? "server message too big" : "WebSocket protocol error");
      return false;
    }
  }
  return true;
}

bool LinkClient::writeAll(const uint8_t* data, size_t len) {
  if (!open_ || io_ == nullptr) return false;
  size_t written = io_->write(data, len);
  if (written != len) {
    setReason("write failed");
    drop();
    return false;
  }
  return true;
}

bool LinkClient::sendFrame(uint8_t opcode, const uint8_t* prefix, size_t prefixLen, const uint8_t* data,
                           size_t len) {
  if (!open_) return false;
  uint8_t mask[4];
  esp_fill_random(mask, sizeof mask);
  size_t n = ws::encodeHeader(tx_, opcode, true, static_cast<uint64_t>(prefixLen) + len, mask);
  size_t offset = 0;  // position within the payload, for the mask
  const uint8_t* parts[2] = {prefix, data};
  const size_t lens[2] = {prefixLen, len};
  for (int k = 0; k < 2; ++k) {
    size_t pos = 0;
    while (pos < lens[k]) {
      size_t take = sizeof tx_ - n;
      if (take > lens[k] - pos) take = lens[k] - pos;
      ws::maskCopy(tx_ + n, parts[k] + pos, take, mask, offset);
      n += take;
      pos += take;
      offset += take;
      if (n == sizeof tx_) {
        if (!writeAll(tx_, n)) return false;
        n = 0;
      }
    }
  }
  return n == 0 || writeAll(tx_, n);
}

bool LinkClient::sendText(const char* text, size_t len) {
  return sendFrame(ws::kOpText, nullptr, 0, reinterpret_cast<const uint8_t*>(text), len);
}

bool LinkClient::sendImage(const uint8_t header[8], const uint8_t* jpeg, size_t len) {
  return sendFrame(ws::kOpBinary, header, 8, jpeg, len);
}

void LinkClient::close(uint16_t code) {
  if (open_) {
    uint8_t payload[2] = {static_cast<uint8_t>(code >> 8), static_cast<uint8_t>(code)};
    sendFrame(ws::kOpClose, nullptr, 0, payload, 2);
  }
  drop();
}

void LinkClient::drop() {
  if (io_ != nullptr) io_->stop();
  io_ = nullptr;
  open_ = false;
  parser_.reset();
}

}  // namespace scout
