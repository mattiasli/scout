#include "ws_codec.h"

#include <string.h>

namespace scout {
namespace ws {

size_t encodeHeader(uint8_t* out, uint8_t opcode, bool fin, uint64_t payloadLen, const uint8_t mask[4]) {
  size_t n = 0;
  out[n++] = static_cast<uint8_t>((fin ? 0x80 : 0x00) | (opcode & 0x0F));
  if (payloadLen < 126) {
    out[n++] = static_cast<uint8_t>(0x80 | payloadLen);
  } else if (payloadLen <= 0xFFFF) {
    out[n++] = 0x80 | 126;
    out[n++] = static_cast<uint8_t>(payloadLen >> 8);
    out[n++] = static_cast<uint8_t>(payloadLen);
  } else {
    out[n++] = 0x80 | 127;
    for (int shift = 56; shift >= 0; shift -= 8) out[n++] = static_cast<uint8_t>(payloadLen >> shift);
  }
  memcpy(out + n, mask, 4);
  return n + 4;
}

void applyMask(uint8_t* data, size_t len, const uint8_t mask[4], size_t offset) {
  for (size_t i = 0; i < len; ++i) data[i] ^= mask[(offset + i) & 3];
}

void maskCopy(uint8_t* dst, const uint8_t* src, size_t len, const uint8_t mask[4], size_t offset) {
  size_t i = 0;
  // Byte-wise until the mask lines up, then four bytes at a time.
  while (i < len && ((offset + i) & 3) != 0) {
    dst[i] = src[i] ^ mask[(offset + i) & 3];
    ++i;
  }
  uint32_t word;
  memcpy(&word, mask, 4);
  for (; i + 4 <= len; i += 4) {
    uint32_t chunk;
    memcpy(&chunk, src + i, 4);
    chunk ^= word;
    memcpy(dst + i, &chunk, 4);
  }
  for (; i < len; ++i) dst[i] = src[i] ^ mask[(offset + i) & 3];
}

FrameParser::FrameParser(uint8_t* buffer, size_t capacity) : msg_(buffer), capacity_(capacity) { reset(); }

void FrameParser::reset() {
  msgLen_ = 0;
  inMessage_ = false;
  msgOpcode_ = kOpText;
  messageReady_ = false;
  state_ = kHead0;
  fin_ = false;
  opcode_ = 0;
  extNeeded_ = 0;
  extGot_ = 0;
  frameLen_ = 0;
  frameGot_ = 0;
  ctrlLen_ = 0;
  ctrl_[0] = 0;
  lastEvent_ = kNone;
  closeCode_ = 0;
  error_ = kOk;
  msg_[0] = 0;
}

const uint8_t* FrameParser::data() const {
  return (lastEvent_ == kText || lastEvent_ == kBinary) ? msg_ : ctrl_;
}

size_t FrameParser::size() const {
  if (lastEvent_ == kText || lastEvent_ == kBinary) return msgLen_;
  if (lastEvent_ == kPing || lastEvent_ == kPong || lastEvent_ == kClose) return ctrlLen_;
  return 0;
}

FrameParser::Event FrameParser::fail(Error error) {
  error_ = error;
  state_ = kFailed;
  inMessage_ = false;
  return kError;
}

FrameParser::Event FrameParser::headerDone() {
  if (extNeeded_ == 8 && (frameLen_ >> 63) != 0) return fail(kProtocol);
  frameGot_ = 0;
  if (opcode_ >= 0x8) {
    if (opcode_ != kOpClose && opcode_ != kOpPing && opcode_ != kOpPong) return fail(kProtocol);
    if (!fin_ || frameLen_ > kMaxControlPayload) return fail(kProtocol);
    ctrlLen_ = 0;
  } else if (opcode_ == kOpContinuation) {
    if (!inMessage_) return fail(kProtocol);
    if (frameLen_ > capacity_ - msgLen_) return fail(kTooBig);
  } else if (opcode_ == kOpText || opcode_ == kOpBinary) {
    if (inMessage_) return fail(kProtocol);
    inMessage_ = true;
    msgOpcode_ = opcode_;
    msgLen_ = 0;
    if (frameLen_ > capacity_) return fail(kTooBig);
  } else {
    return fail(kProtocol);
  }
  state_ = kPayload;
  if (frameLen_ == 0) return frameDone();
  return kNone;
}

FrameParser::Event FrameParser::frameDone() {
  state_ = kHead0;
  if (opcode_ >= 0x8) {
    ctrl_[ctrlLen_] = 0;
    if (opcode_ == kOpPing) return kPing;
    if (opcode_ == kOpPong) return kPong;
    if (ctrlLen_ == 1) return fail(kProtocol);
    closeCode_ = ctrlLen_ >= 2 ? static_cast<uint16_t>((ctrl_[0] << 8) | ctrl_[1]) : kCloseNoStatus;
    return kClose;
  }
  if (!fin_) return kNone;
  inMessage_ = false;
  msg_[msgLen_] = 0;
  messageReady_ = true;
  return msgOpcode_ == kOpText ? kText : kBinary;
}

FrameParser::Event FrameParser::feed(const uint8_t* data, size_t len, size_t* consumed) {
  *consumed = 0;
  if (state_ == kFailed) return kError;
  if (messageReady_) {
    msgLen_ = 0;
    msg_[0] = 0;
    messageReady_ = false;
  }
  size_t i = 0;
  Event event = kNone;
  while (i < len && event == kNone) {
    uint8_t byte = data[i];
    switch (state_) {
      case kHead0:
        ++i;
        if (byte & 0x70) {
          event = fail(kProtocol);
          break;
        }
        fin_ = (byte & 0x80) != 0;
        opcode_ = byte & 0x0F;
        state_ = kHead1;
        break;
      case kHead1: {
        ++i;
        if (byte & 0x80) {  // servers must not mask
          event = fail(kProtocol);
          break;
        }
        uint8_t len7 = byte & 0x7F;
        frameLen_ = 0;
        extGot_ = 0;
        if (len7 < 126) {
          extNeeded_ = 0;
          frameLen_ = len7;
          event = headerDone();
        } else {
          extNeeded_ = (len7 == 126) ? 2 : 8;
          state_ = kExtLen;
        }
        break;
      }
      case kExtLen:
        ++i;
        frameLen_ = (frameLen_ << 8) | byte;
        if (++extGot_ == extNeeded_) event = headerDone();
        break;
      case kPayload: {
        uint64_t remaining = frameLen_ - frameGot_;
        size_t take = len - i;
        if (take > remaining) take = static_cast<size_t>(remaining);
        if (opcode_ >= 0x8) {
          memcpy(ctrl_ + ctrlLen_, data + i, take);
          ctrlLen_ += take;
        } else {
          memcpy(msg_ + msgLen_, data + i, take);
          msgLen_ += take;
        }
        i += take;
        frameGot_ += take;
        if (frameGot_ == frameLen_) event = frameDone();
        break;
      }
      case kFailed:
        event = kError;
        break;
    }
  }
  *consumed = i;
  lastEvent_ = event;
  return event;
}

}  // namespace ws
}  // namespace scout
