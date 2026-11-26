// Fake hardware and a test rig for the UNO core.
//
// The core calls the scout::hw functions (hardware.h); fake_board.cpp implements them on
// FakeBoard::active, which every rig points at its own board before it drives the car.

#ifndef SCOUT_UNO_TESTS_FAKE_BOARD_H
#define SCOUT_UNO_TESTS_FAKE_BOARD_H

#include <cstdio>
#include <map>
#include <string>
#include <vector>

#include "ScoutLink.h"
#include "config.h"
#include "scout.h"

// One frame the UNO sent, decoded.
struct Sent {
  std::string type;
  unsigned seq;
  std::vector<std::string> args;
  uint32_t at;  // fake time when it was sent

  std::string arg(size_t i) const { return i < args.size() ? args[i] : std::string("<none>"); }
  // "TYPE,seq,arg,..." for readable checks.
  std::string text() const {
    std::string s = type + "," + std::to_string(seq);
    for (const std::string& a : args) s += "," + a;
    return s;
  }
};

// A VL53L1X on the I2C bus. It keeps every register written, starts and stops measuring
// when told to, and has a new measurement ready every periodMs while measuring.
struct FakeLaser {
  bool present = false;  // nothing answers until a test fits one
  uint16_t modelId = 0xEACC;
  bool booted = true;
  uint16_t oscCalibrate = 0x0153;  // the sensor's clock calibration (low 10 bits count)
  uint16_t mm = 0;                 // what each measurement finds
  uint8_t status = 9;              // raw range status of each measurement (9 = good)
  uint32_t periodMs = 100;         // a measurement every this long while measuring
  int failNext = 0;                // the next n exchanges fail, as on a disturbed bus

  std::map<uint16_t, uint8_t> regs;  // everything written
  bool measuring = false;
  bool ready = false;
  uint32_t lastMeasureMs = 0;
  int starts = 0;           // times continuous measuring was started
  int exchanges = 0;        // reads and writes, failed ones too
  unsigned longestWrite = 0;
};

struct FakeBoard {
  static FakeBoard* active;

  // Every hardware call in order, e.g. "enable 0", "sr 163", "pwm 172 172", "servo 1472",
  // "tx BOOT". Pings and echoes aren't logged (they happen every 60 ms).
  std::vector<std::string> log;

  bool driverEnabled = false;
  uint8_t bits = 0xFF;  // whatever the 74HC595 holds at power-on
  uint8_t pwm5 = 0xFF;
  uint8_t pwm6 = 0xFF;
  uint16_t servoUs = 0;
  int pings = 0;
  uint16_t line[3] = {0, 0, 0};

  // Echo for the next pings: width in us, or -1 for no echo at all. The echo of a ping
  // is ready right after the ping.
  long echoUs = -1;
  bool echoReady = false;

  FakeLaser laser;

  std::vector<Sent> sent;
  uint32_t clock = 0;      // set by the rig so sent frames are time-stamped
  uint32_t badFrames = 0;  // send() calls that weren't exactly one valid frame
  scoutlink::Parser decoder;

  void setDistanceCm(long cm) { echoUs = cm < 0 ? -1 : cm * 58; }
};

inline std::string frameFor(const std::string& body) {
  const uint8_t crc = scoutlink::crc8(reinterpret_cast<const uint8_t*>(body.data()), body.size());
  char hex[3];
  std::snprintf(hex, sizeof hex, "%02X", crc);
  return "$" + body + "*" + hex + "\n";
}

// A car on the bench with a fake clock, booted unless asked not to. tick() advances 1 ms
// at a time, like a loop() that runs at least once per millisecond.
struct Rig {
  FakeBoard hw;
  scout::Scout car;
  uint32_t now = 0;

  explicit Rig(bool boot = true, uint8_t resetFlags = 0) {
    use();
    if (boot) car.begin(now, resetFlags);
  }
  Rig(const Rig&) = delete;
  Rig& operator=(const Rig&) = delete;

  void use() {
    FakeBoard::active = &hw;
    hw.clock = now;
  }

  void rx(const std::string& body) { rxRaw(frameFor(body)); }
  void rxRaw(const std::string& bytes) {
    use();
    for (char c : bytes) car.receive(static_cast<uint8_t>(c), now);
  }

  void tick(uint32_t ms = 1) {
    for (uint32_t i = 0; i < ms; ++i) {
      ++now;
      use();
      car.update(now);
    }
  }

  // Advances `ms`, sending a PING every `every` ms so the link watchdog stays quiet.
  void tickWithPings(uint32_t ms, uint32_t every = 250) {
    for (uint32_t i = 0; i < ms; ++i) {
      tick();
      if (now % every == 0) rx("PING,60000");
    }
  }

  // Lets the ranger pick up the current fake distance (one full ping period).
  void settleRange() { tick(scout::kRangePeriodMs + 1); }

  // Frames sent since the last take(), without TEL and PONG unless asked for.
  std::vector<Sent> take(bool withTelemetry = false) {
    std::vector<Sent> out;
    for (const Sent& s : hw.sent) {
      if (!withTelemetry && (s.type == "TEL" || s.type == "PONG")) continue;
      out.push_back(s);
    }
    hw.sent.clear();
    return out;
  }

  std::vector<std::string> takeText(bool withTelemetry = false) {
    std::vector<std::string> out;
    for (const Sent& s : take(withTelemetry)) out.push_back(s.text());
    return out;
  }

  std::vector<Sent> ofType(const std::string& type) const {
    std::vector<Sent> out;
    for (const Sent& s : hw.sent) {
      if (s.type == type) out.push_back(s);
    }
    return out;
  }
};

// Joins lines for readable CHECK_EQ failures.
inline std::string joined(const std::vector<std::string>& lines) {
  std::string out;
  for (const std::string& line : lines) out += (out.empty() ? "" : " | ") + line;
  return out;
}

#endif  // SCOUT_UNO_TESTS_FAKE_BOARD_H
