#include "util.h"

#include <cmath>
#include <string.h>

namespace scout {

namespace {

int hexValue(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

const char kHexUpper[] = "0123456789ABCDEF";

}  // namespace

bool parseSha256Hex(const char* hex, uint8_t out[32]) {
  if (hex == nullptr || strlen(hex) != 64) return false;
  for (int i = 0; i < 32; ++i) {
    int hi = hexValue(hex[2 * i]);
    int lo = hexValue(hex[2 * i + 1]);
    if (hi < 0 || lo < 0) return false;
    out[i] = static_cast<uint8_t>((hi << 4) | lo);
  }
  return true;
}

void formatMac(const uint8_t mac[6], char out[18]) {
  size_t n = 0;
  for (int i = 0; i < 6; ++i) {
    if (i > 0) out[n++] = ':';
    out[n++] = kHexUpper[mac[i] >> 4];
    out[n++] = kHexUpper[mac[i] & 0x0F];
  }
  out[n] = '\0';
}

const char* resetReasonName(int reason) {
  static const char* const kNames[] = {
      "unknown",  "poweron", "external", "software", "panic", "int_wdt", "task_wdt",     "wdt",
      "deepsleep", "brownout", "sdio",   "usb",      "jtag",  "efuse",   "power_glitch", "cpu_lockup",
  };
  if (reason < 0 || reason >= static_cast<int>(sizeof kNames / sizeof kNames[0])) return "unknown";
  return kNames[reason];
}

bool resetButtonPressed(int reason, bool unoAnswered, uint32_t unoUptimeS, uint32_t ownUptimeMs,
                        uint32_t marginMs) {
  // ESP_RST_POWERON (1) is what the ESP32 reports for its reset button (EN pin) as well as
  // for power-on; ESP_RST_EXT (2) is listed for completeness.
  if (reason != 1 && reason != 2) return false;
  if (!unoAnswered) return false;
  return static_cast<uint64_t>(unoUptimeS) * 1000 >= static_cast<uint64_t>(ownUptimeMs) + marginMs;
}

uint32_t lightDuty(int32_t level, uint32_t maxDuty) {
  if (level <= 0) return 0;
  if (level >= 100) return maxDuty;
  return static_cast<uint32_t>((static_cast<uint64_t>(level) * maxDuty + 50) / 100);
}

bool ledOn(LedMode mode, uint32_t ms) {
  switch (mode) {
    case LedMode::kOff:
      return false;
    case LedMode::kSetup: {  // two short blinks every 2 s
      uint32_t t = ms % 2000;
      return t < 100 || (t >= 250 && t < 350);
    }
    case LedMode::kConnecting:  // 4 Hz
      return ms % 250 < 125;
    case LedMode::kConnected:  // steady
      return true;
    case LedMode::kUpdating:  // 10 Hz
      return ms % 100 < 50;
  }
  return false;
}

void writeImageHeader(uint8_t out[8], bool stream, uint32_t id) {
  memcpy(out, stream ? "SCST" : "SCIM", 4);
  out[4] = static_cast<uint8_t>(id);
  out[5] = static_cast<uint8_t>(id >> 8);
  out[6] = static_cast<uint8_t>(id >> 16);
  out[7] = static_cast<uint8_t>(id >> 24);
}

bool numberToInt(double value, int32_t lo, int32_t hi, int32_t* out) {
  if (!std::isfinite(value)) return false;
  double rounded = std::floor(value + 0.5);
  if (rounded < static_cast<double>(lo) || rounded > static_cast<double>(hi)) return false;
  *out = static_cast<int32_t>(rounded);
  return true;
}

bool numberToId(double value, uint32_t* out) {
  if (!std::isfinite(value) || value < 1.0 || value > 4294967295.0 || std::floor(value) != value) return false;
  *out = static_cast<uint32_t>(value);
  return true;
}

size_t htmlEscape(const char* in, char* out, size_t cap) {
  if (cap == 0) return 0;
  size_t n = 0;
  for (; in != nullptr && *in != '\0'; ++in) {
    const char* rep = nullptr;
    switch (*in) {
      case '&': rep = "&amp;"; break;
      case '<': rep = "&lt;"; break;
      case '>': rep = "&gt;"; break;
      case '"': rep = "&quot;"; break;
      case '\'': rep = "&#39;"; break;
      default: break;
    }
    size_t len = rep != nullptr ? strlen(rep) : 1;
    if (n + len >= cap) break;
    if (rep != nullptr) {
      memcpy(out + n, rep, len);
    } else {
      out[n] = *in;
    }
    n += len;
  }
  out[n] = '\0';
  return n;
}

bool validCarKey(const char* key) {
  if (key == nullptr) return false;
  size_t n = strlen(key);
  if (n == 0 || n > 128) return false;
  for (size_t i = 0; i < n; ++i) {
    if (key[i] < 0x21 || key[i] > 0x7E) return false;
  }
  return true;
}

bool validSetupPassword(const char* pass) {
  if (pass == nullptr) return false;
  size_t n = strlen(pass);
  if (n < 8 || n > 63) return false;
  for (size_t i = 0; i < n; ++i) {
    if (pass[i] < 0x20 || pass[i] > 0x7E) return false;
  }
  return true;
}

int percentOf(uint32_t done, uint32_t total) {
  if (total == 0) return 0;
  uint64_t pct = static_cast<uint64_t>(done) * 100 / total;
  return pct > 100 ? 100 : static_cast<int>(pct);
}

}  // namespace scout
