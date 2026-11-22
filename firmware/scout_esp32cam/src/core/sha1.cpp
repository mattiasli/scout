#include "sha1.h"

#include <string.h>

namespace scout {

namespace {

uint32_t rol(uint32_t value, unsigned bits) { return (value << bits) | (value >> (32 - bits)); }

const char kAlphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

}  // namespace

Sha1::Sha1() : used_(0), total_(0) {
  h_[0] = 0x67452301;
  h_[1] = 0xEFCDAB89;
  h_[2] = 0x98BADCFE;
  h_[3] = 0x10325476;
  h_[4] = 0xC3D2E1F0;
}

void Sha1::block(const uint8_t* chunk) {
  uint32_t w[80];
  for (int i = 0; i < 16; ++i) {
    w[i] = (static_cast<uint32_t>(chunk[4 * i]) << 24) | (static_cast<uint32_t>(chunk[4 * i + 1]) << 16) |
           (static_cast<uint32_t>(chunk[4 * i + 2]) << 8) | static_cast<uint32_t>(chunk[4 * i + 3]);
  }
  for (int i = 16; i < 80; ++i) w[i] = rol(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);

  uint32_t a = h_[0], b = h_[1], c = h_[2], d = h_[3], e = h_[4];
  for (int i = 0; i < 80; ++i) {
    uint32_t f, k;
    if (i < 20) {
      f = (b & c) | (~b & d);
      k = 0x5A827999;
    } else if (i < 40) {
      f = b ^ c ^ d;
      k = 0x6ED9EBA1;
    } else if (i < 60) {
      f = (b & c) | (b & d) | (c & d);
      k = 0x8F1BBCDC;
    } else {
      f = b ^ c ^ d;
      k = 0xCA62C1D6;
    }
    uint32_t t = rol(a, 5) + f + e + k + w[i];
    e = d;
    d = c;
    c = rol(b, 30);
    b = a;
    a = t;
  }
  h_[0] += a;
  h_[1] += b;
  h_[2] += c;
  h_[3] += d;
  h_[4] += e;
}

void Sha1::update(const uint8_t* data, size_t len) {
  total_ += len;
  while (len > 0) {
    size_t take = 64 - used_;
    if (take > len) take = len;
    memcpy(buf_ + used_, data, take);
    used_ += take;
    data += take;
    len -= take;
    if (used_ == 64) {
      block(buf_);
      used_ = 0;
    }
  }
}

void Sha1::finish(uint8_t digest[20]) {
  uint64_t bits = total_ * 8;
  uint8_t pad = 0x80;
  update(&pad, 1);
  uint8_t zero = 0;
  while (used_ != 56) update(&zero, 1);
  uint8_t length[8];
  for (int i = 0; i < 8; ++i) length[i] = static_cast<uint8_t>(bits >> (56 - 8 * i));
  update(length, 8);
  for (int i = 0; i < 5; ++i) {
    digest[4 * i] = static_cast<uint8_t>(h_[i] >> 24);
    digest[4 * i + 1] = static_cast<uint8_t>(h_[i] >> 16);
    digest[4 * i + 2] = static_cast<uint8_t>(h_[i] >> 8);
    digest[4 * i + 3] = static_cast<uint8_t>(h_[i]);
  }
}

void sha1(const uint8_t* data, size_t len, uint8_t digest[20]) {
  Sha1 hash;
  hash.update(data, len);
  hash.finish(digest);
}

size_t base64Encode(const uint8_t* data, size_t len, char* out, size_t cap) {
  size_t need = ((len + 2) / 3) * 4;
  if (cap < need + 1) return 0;
  size_t n = 0;
  for (size_t i = 0; i < len; i += 3) {
    uint32_t chunk = static_cast<uint32_t>(data[i]) << 16;
    if (i + 1 < len) chunk |= static_cast<uint32_t>(data[i + 1]) << 8;
    if (i + 2 < len) chunk |= data[i + 2];
    out[n++] = kAlphabet[(chunk >> 18) & 0x3F];
    out[n++] = kAlphabet[(chunk >> 12) & 0x3F];
    out[n++] = (i + 1 < len) ? kAlphabet[(chunk >> 6) & 0x3F] : '=';
    out[n++] = (i + 2 < len) ? kAlphabet[chunk & 0x3F] : '=';
  }
  out[n] = '\0';
  return n;
}

}  // namespace scout
