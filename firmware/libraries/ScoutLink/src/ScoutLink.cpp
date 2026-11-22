#include "ScoutLink.h"

#include <string.h>

namespace scoutlink {

namespace {

bool isBodyChar(uint8_t c) { return c >= 0x20 && c <= 0x7E && c != '$' && c != '*'; }

int hexValue(uint8_t c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}

bool validType(const char* text) {
  size_t n = 0;
  for (; text[n] != '\0'; ++n) {
    if (text[n] < 'A' || text[n] > 'Z' || n >= 8) return false;
  }
  return n >= 1;
}

// "0" or [1-9][0-9]{0,4}, value <= 65535.
bool parseSeq(const char* text, uint16_t* out) {
  size_t n = strlen(text);
  if (n == 0 || n > 5 || (n > 1 && text[0] == '0')) return false;
  uint32_t value = 0;
  for (size_t i = 0; i < n; ++i) {
    if (text[i] < '0' || text[i] > '9') return false;
    value = value * 10 + static_cast<uint32_t>(text[i] - '0');
  }
  if (value > 65535) return false;
  *out = static_cast<uint16_t>(value);
  return true;
}

const char kHex[] = "0123456789ABCDEF";

}  // namespace

uint8_t crc8(const uint8_t* data, size_t len) {
  uint8_t crc = 0;
  for (size_t i = 0; i < len; ++i) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc & 0x80) ? static_cast<uint8_t>((crc << 1) ^ 0x07) : static_cast<uint8_t>(crc << 1);
    }
  }
  return crc;
}

bool parseInt(const char* text, int32_t* out) {
  bool negative = false;
  if (*text == '-') {
    negative = true;
    ++text;
  }
  size_t n = strlen(text);
  if (n == 0 || n > 10 || (n > 1 && text[0] == '0')) return false;
  const uint32_t limit = negative ? 2147483648UL : 2147483647UL;
  uint32_t value = 0;
  for (size_t i = 0; i < n; ++i) {
    if (text[i] < '0' || text[i] > '9') return false;
    uint32_t digit = static_cast<uint32_t>(text[i] - '0');
    if (value > (limit - digit) / 10) return false;
    value = value * 10 + digit;
  }
  if (negative) {
    *out = (value == 0) ? 0 : -static_cast<int32_t>(value - 1) - 1;
  } else {
    *out = static_cast<int32_t>(value);
  }
  return true;
}

// ---------------------------------------------------------------- Builder

Builder::Builder(const char* type, uint16_t seq) : len_(0), args_(0), ok_(validType(type)) {
  body_[0] = '\0';
  appendText(type);
  add(static_cast<int32_t>(seq));
  args_ = 0;  // the seq doesn't count as an arg
}

void Builder::append(char c) {
  if (!ok_) return;
  if (len_ >= kMaxBody) {
    ok_ = false;
    return;
  }
  body_[len_++] = c;
  body_[len_] = '\0';
}

void Builder::appendText(const char* text) {
  for (; *text != '\0'; ++text) append(*text);
}

Builder& Builder::add(int32_t value) {
  if (++args_ > kMaxArgs) ok_ = false;  // the constructor adds seq this way, then resets args_
  append(',');
  char digits[11];
  uint8_t n = 0;
  uint32_t magnitude;
  if (value < 0) {
    append('-');
    magnitude = static_cast<uint32_t>(-(value + 1)) + 1;
  } else {
    magnitude = static_cast<uint32_t>(value);
  }
  do {
    digits[n++] = static_cast<char>('0' + magnitude % 10);
    magnitude /= 10;
  } while (magnitude != 0);
  while (n > 0) append(digits[--n]);
  return *this;
}

Builder& Builder::add(const char* text) {
  if (++args_ > kMaxArgs) ok_ = false;
  if (*text == '\0') ok_ = false;
  append(',');
  for (; *text != '\0'; ++text) {
    if (!isBodyChar(static_cast<uint8_t>(*text)) || *text == ',') ok_ = false;
    append(*text);
  }
  return *this;
}

size_t Builder::finish(char* out, size_t cap) const {
  if (!ok_ || len_ == 0 || cap < static_cast<size_t>(len_) + 6) return 0;
  uint8_t crc = crc8(reinterpret_cast<const uint8_t*>(body_), len_);
  size_t n = 0;
  out[n++] = '$';
  memcpy(out + n, body_, len_);
  n += len_;
  out[n++] = '*';
  out[n++] = kHex[crc >> 4];
  out[n++] = kHex[crc & 0x0F];
  out[n++] = '\n';
  out[n] = '\0';
  return n;
}

// ---------------------------------------------------------------- Parser

Parser::Parser()
    : state_(kIdle), len_(0), crcDigits_(0), crcValue_(0), argc_(0), seq_(0), rxErrors_(0) {
  buf_[0] = '\0';
  for (uint8_t i = 0; i < 2 + kMaxArgs; ++i) fields_[i] = buf_;
}

bool Parser::feed(uint8_t byte) {
  if (byte == '$') {
    if (state_ != kIdle) ++rxErrors_;
    state_ = kBody;
    len_ = 0;
    crcDigits_ = 0;
    crcValue_ = 0;
    return false;
  }
  switch (state_) {
    case kIdle:
      return false;
    case kBody:
      if (byte == '*') {
        state_ = kCrc;
      } else if (byte >= 0x20 && byte <= 0x7E && len_ < kMaxBody) {
        buf_[len_++] = static_cast<char>(byte);
      } else {
        ++rxErrors_;
        state_ = kIdle;
      }
      return false;
    case kCrc: {
      int value = hexValue(byte);
      if (value < 0) {
        ++rxErrors_;
        state_ = kIdle;
        return false;
      }
      crcValue_ = static_cast<uint8_t>((crcValue_ << 4) | value);
      if (++crcDigits_ < 2) return false;
      state_ = kIdle;
      if (crcValue_ != crc8(reinterpret_cast<const uint8_t*>(buf_), len_)) {
        ++rxErrors_;
        return false;
      }
      buf_[len_] = '\0';
      if (!split()) {
        ++rxErrors_;
        return false;
      }
      return true;
    }
  }
  return false;
}

bool Parser::split() {
  if (len_ == 0) return false;
  uint8_t count = 0;
  fields_[count++] = buf_;
  for (uint8_t i = 0; i < len_; ++i) {
    if (buf_[i] == ',') {
      if (count >= 2 + kMaxArgs) return false;
      buf_[i] = '\0';
      fields_[count++] = buf_ + i + 1;
    }
  }
  if (count < 2 || !validType(fields_[0]) || !parseSeq(fields_[1], &seq_)) return false;
  argc_ = static_cast<uint8_t>(count - 2);
  return true;
}

const char* Parser::arg(uint8_t index) const {
  return index < argc_ ? fields_[2 + index] : "";
}

bool Parser::argInt(uint8_t index, int32_t* out) const {
  return index < argc_ && parseInt(fields_[2 + index], out);
}

bool Parser::argIs(uint8_t index, const char* text) const {
  return index < argc_ && strcmp(fields_[2 + index], text) == 0;
}

bool Parser::is(const char* type) const { return strcmp(fields_[0], type) == 0; }

}  // namespace scoutlink
