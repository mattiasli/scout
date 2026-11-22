// A minimal RFC 6455 WebSocket client for the car link (protocol/car-link.md).
//
// wss:// runs over NetworkClientSecure with the server certificate checked against the
// CA bundle built into the core; ws:// (plain NetworkClient) is for a development
// server only. The framing, upgrade and URL logic live in core/ and are unit-tested on
// the PC; this file only moves bytes.
//
// All calls come from the main loop.

#ifndef SCOUT_LINK_CLIENT_H
#define SCOUT_LINK_CLIENT_H

#include <Arduino.h>
#include <NetworkClient.h>
#include <NetworkClientSecure.h>

#include "config.h"
#include "core/ws_codec.h"
#include "core/ws_handshake.h"

namespace scout {

class LinkClient {
 public:
  // Called for every complete text message. `text` is NUL-terminated and may be
  // modified; it is only valid during the call.
  typedef void (*TextHandler)(void* context, char* text, size_t len);

  LinkClient();
  void setHandler(TextHandler handler, void* context);

  // Opens the connection and performs the upgrade. Blocks for up to about 30 s.
  // On failure `why` holds a short reason.
  bool connect(const ws::ServerUrl& url, const char* carKey, const char* firmware, char* why, size_t whyCap);

  // Reads whatever arrived, answers pings and delivers messages. Returns false once
  // the connection is gone (closed by either side, or a protocol error).
  bool poll();

  bool connected() const { return open_; }
  uint32_t lastHeardMs() const { return lastHeardMs_; }

  bool sendText(const char* text, size_t len);
  // One binary frame: the 8-byte picture header, then `len` bytes of JPEG straight from
  // the frame buffer (masked in small pieces, never copied whole).
  bool sendImage(const uint8_t header[8], const uint8_t* jpeg, size_t len);

  // Sends a close frame (best effort) and drops the connection.
  void close(uint16_t code);
  // Drops the connection without a close frame.
  void drop();

  const char* closeReason() const { return closeReason_; }

 private:
  __attribute__((noinline)) bool openTransport(const ws::ServerUrl& url, char* why, size_t whyCap);
  __attribute__((noinline)) bool upgrade(const ws::ServerUrl& url, const char* carKey, const char* firmware, char* why,
                                         size_t whyCap);
  bool sendFrame(uint8_t opcode, const uint8_t* prefix, size_t prefixLen, const uint8_t* data, size_t len);
  bool writeAll(const uint8_t* data, size_t len);
  bool handleEvent(ws::FrameParser::Event event);
  void setReason(const char* reason);

  NetworkClient tcp_;
  NetworkClientSecure tls_;
  NetworkClient* io_;
  bool open_;
  uint32_t lastHeardMs_;
  TextHandler handler_;
  void* context_;
  uint8_t rxMessage_[cfg::kLinkMaxMessageBytes + 1];
  ws::FrameParser parser_;
  uint8_t rxChunk_[512];
  uint8_t tx_[ws::kMaxHeader + cfg::kLinkSendChunkBytes];
  char closeReason_[48];
};

}  // namespace scout

#endif  // SCOUT_LINK_CLIENT_H
