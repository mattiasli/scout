// scout::hw (firmware/scout_uno/src/core/hardware.h) on top of FakeBoard::active.

#include "fake_board.h"

#include "hardware.h"

FakeBoard* FakeBoard::active = nullptr;

namespace {

FakeBoard& board() {
  static FakeBoard spare;  // used if a test drives the core without a rig
  return FakeBoard::active != nullptr ? *FakeBoard::active : spare;
}

bool laserAnswers(FakeLaser& l) {
  ++l.exchanges;
  if (!l.present) return false;
  if (l.failNext > 0) {
    --l.failNext;
    return false;
  }
  return true;
}

uint8_t laserRegister(const FakeLaser& l, uint16_t reg) {
  switch (reg) {
    case 0x0031:
      return l.ready ? 0x03 : 0x02;  // bit 0: a measurement is ready
    case 0x0089:
      return l.status;
    case 0x0096:
      return static_cast<uint8_t>(l.mm >> 8);
    case 0x0097:
      return static_cast<uint8_t>(l.mm);
    case 0x00DE:
      return static_cast<uint8_t>(l.oscCalibrate >> 8);
    case 0x00DF:
      return static_cast<uint8_t>(l.oscCalibrate);
    case 0x00E5:
      return l.booted ? 1 : 0;
    case 0x010F:
      return static_cast<uint8_t>(l.modelId >> 8);
    case 0x0110:
      return static_cast<uint8_t>(l.modelId);
    default: {
      const auto it = l.regs.find(reg);
      return it == l.regs.end() ? 0 : it->second;
    }
  }
}

}  // namespace

namespace scout {
namespace hw {

void setDriverEnabled(bool enabled) {
  board().driverEnabled = enabled;
  board().log.push_back(std::string("enable ") + (enabled ? "1" : "0"));
}

void writeShiftRegister(uint8_t bits) {
  board().bits = bits;
  board().log.push_back("sr " + std::to_string(bits));
}

void writePwm(uint8_t pwm5, uint8_t pwm6) {
  board().pwm5 = pwm5;
  board().pwm6 = pwm6;
  board().log.push_back("pwm " + std::to_string(pwm5) + " " + std::to_string(pwm6));
}

void writeServo(uint16_t pulseUs) {
  board().servoUs = pulseUs;
  board().log.push_back("servo " + std::to_string(pulseUs));
}

void triggerRanger() {
  ++board().pings;
  board().echoReady = board().echoUs >= 0;
}

bool takeEcho(uint32_t* widthUs) {
  if (!board().echoReady) return false;
  board().echoReady = false;
  *widthUs = static_cast<uint32_t>(board().echoUs);
  return true;
}

void readLine(uint16_t* a0, uint16_t* a1, uint16_t* a2) {
  *a0 = board().line[0];
  *a1 = board().line[1];
  *a2 = board().line[2];
}

bool laserWrite(uint16_t reg, const uint8_t* data, uint8_t length) {
  FakeBoard& b = board();
  FakeLaser& l = b.laser;
  if (!laserAnswers(l)) return false;
  if (length > l.longestWrite) l.longestWrite = length;
  for (uint8_t i = 0; i < length; ++i) {
    const uint16_t r = static_cast<uint16_t>(reg + i);
    l.regs[r] = data[i];
    if (r == 0x0086 && (data[i] & 1)) l.ready = false;  // interrupt cleared
    if (r == 0x0087) {
      l.measuring = data[i] == 0x40;
      l.ready = false;
      l.lastMeasureMs = b.clock;
      if (l.measuring) ++l.starts;
    }
  }
  return true;
}

bool laserRead(uint16_t reg, uint8_t* data, uint8_t length) {
  FakeBoard& b = board();
  FakeLaser& l = b.laser;
  if (!laserAnswers(l)) return false;
  if (l.measuring && !l.ready && b.clock - l.lastMeasureMs >= l.periodMs) {
    l.ready = true;
    l.lastMeasureMs = b.clock;
  }
  for (uint8_t i = 0; i < length; ++i) data[i] = laserRegister(l, static_cast<uint16_t>(reg + i));
  return true;
}

void send(const char* data, uint8_t length) {
  FakeBoard& b = board();
  const uint32_t errorsBefore = b.decoder.rxErrors();
  int frames = 0;
  for (uint8_t i = 0; i < length; ++i) {
    if (!b.decoder.feed(static_cast<uint8_t>(data[i]))) continue;
    ++frames;
    Sent s;
    s.type = b.decoder.type();
    s.seq = b.decoder.seq();
    for (uint8_t a = 0; a < b.decoder.argCount(); ++a) s.args.push_back(b.decoder.arg(a));
    s.at = b.clock;
    b.sent.push_back(s);
    b.log.push_back("tx " + s.type);
  }
  // Each send() must be exactly one valid frame ending in '\n'.
  if (frames != 1 || b.decoder.rxErrors() != errorsBefore || length == 0 ||
      data[length - 1] != '\n') {
    ++b.badFrames;
  }
}

}  // namespace hw
}  // namespace scout
