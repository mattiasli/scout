// Small pieces of car logic that need neither the hardware nor JSON.

#ifndef SCOUT_CORE_UTIL_H
#define SCOUT_CORE_UTIL_H

#include <stddef.h>
#include <stdint.h>

namespace scout {

// 64 hex digits (either case) -> 32 bytes.
bool parseSha256Hex(const char* hex, uint8_t out[32]);

// "0A:BC:DE:F0:12:34"
void formatMac(const uint8_t mac[6], char out[18]);

// esp_reset_reason_t value -> the car-link "reset" word ("poweron", "software", ...).
const char* resetReasonName(int reason);
// True when this boot is someone pressing the ESP32-CAM's reset button. The chip reports
// a power-on both for that button (it pulls the enable pin) and for the power switch, so
// the UNO tells them apart: the button doesn't touch it, so it has been running for at
// least `marginMs` longer than this board. The power switch restarts both together.
// False when the UNO didn't answer, and for restarts, crashes and brownouts.
bool resetButtonPressed(int reason, bool unoAnswered, uint32_t unoUptimeS, uint32_t ownUptimeMs,
                        uint32_t marginMs);

// Light level 0..100 -> LEDC duty, where level 100 gets `maxDuty`.
uint32_t lightDuty(int32_t level, uint32_t maxDuty);

// The status LED's blink patterns.
enum class LedMode : uint8_t { kOff, kSetup, kConnecting, kConnected, kUpdating };
bool ledOn(LedMode mode, uint32_t ms);

// The 8-byte header of a binary picture frame: "SCIM" (snapshot) or "SCST" (stream
// frame), then the request id as uint32 little-endian.
void writeImageHeader(uint8_t out[8], bool stream, uint32_t id);

// Converts a JSON number to an integer in [lo, hi], rounding to the nearest integer.
// False for NaN, infinities and values outside the range.
bool numberToInt(double value, int32_t lo, int32_t hi, int32_t* out);
// A request id: a positive integer that fits in uint32 (the binary frame header).
bool numberToId(double value, uint32_t* out);

// HTML-escapes `in` into `out` (always NUL-terminated, truncated to fit). Returns the
// length written.
size_t htmlEscape(const char* in, char* out, size_t cap);

// The car key goes into an HTTP header: 1..128 visible ASCII characters, no spaces.
bool validCarKey(const char* key);
// The setup access point's WPA2 password: 8..63 printable ASCII characters.
bool validSetupPassword(const char* pass);

// done/total as a whole percentage, clamped to 0..100.
int percentOf(uint32_t done, uint32_t total);

}  // namespace scout

#endif  // SCOUT_CORE_UTIL_H
