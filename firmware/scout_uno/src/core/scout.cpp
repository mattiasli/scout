#include "scout.h"

#include "config.h"
#include "hardware.h"
#include "tilt.h"

namespace scout {

namespace {

// Message words. Each is one array, shared where the same word has two uses, so the
// strings exist once in RAM.
const char kPing[] = "PING";
const char kPong[] = "PONG";
const char kMove[] = "MOVE";
const char kStop[] = "STOP";  // request type and DONE reason
const char kTilt[] = "TILT";
const char kConf[] = "CONF";
const char kTest[] = "TEST";
const char kBoot[] = "BOOT";
const char kAck[] = "ACK";
const char kErr[] = "ERR";
const char kDone[] = "DONE";
const char kTel[] = "TEL";
const char kErrType[] = "TYPE";
const char kErrArgs[] = "ARGS";
const char kErrRange[] = "RANGE";
const char kTime[] = "TIME";
const char kObst[] = "OBST";  // DONE reason and CONF key
const char kLink[] = "LINK";
const char kNew[] = "NEW";
const char kMinPwm[] = "MINPWM";
const char kLinkMs[] = "LINKMS";

bool inRange(int32_t value, int32_t low, int32_t high) { return value >= low && value <= high; }

// Unsigned counters go out as int32 args; saturate instead of wrapping negative.
int32_t toArg(uint32_t value) {
  return value > 0x7FFFFFFFUL ? static_cast<int32_t>(0x7FFFFFFFL) : static_cast<int32_t>(value);
}

}  // namespace

Scout::Scout()
    : obstacleCm_(kObstacleDefaultCm),
      minPwm_(kMinPwmDefault),
      linkMs_(kLinkDefaultMs),
      tilt_(0),
      lastRxMs_(0),
      nextTelMs_(0) {
  motion_.seq = 0;
  motion_.watchObstacle = false;
  motion_.durationMs = 0;
  motion_.startMs = 0;
  motion_.cmStart = -1;
}

void Scout::begin(uint32_t now, uint8_t resetFlags) {
  // Outputs off first, then zeros into the shift register, and only then outputs on:
  // from here the L293DD inputs are provably low.
  hw::setDriverEnabled(false);
  hw::writePwm(0, 0);
  hw::writeShiftRegister(0);
  hw::setDriverEnabled(true);
  motion_.seq = 0;

  setTilt(0);
  lastRxMs_ = now;
  nextTelMs_ = now + kTelemetryPeriodMs;

  scoutlink::Builder boot(kBoot, 0);
  boot.add(kFirmwareVersion).add(static_cast<int32_t>(resetFlags));
  send(boot);
}

void Scout::receive(uint8_t byte, uint32_t now) {
  if (parser_.feed(byte)) dispatch(now);
}

void Scout::update(uint32_t now) {
  ranger_.update(now);

  if (motion_.seq != 0) {
    const char* reason = 0;
    if (now - motion_.startMs >= motion_.durationMs) {
      reason = kTime;
    } else if (motion_.watchObstacle && obstacleAt(ranger_.cm())) {
      reason = kObst;
    } else if (now - lastRxMs_ >= linkMs_) {
      reason = kLink;
    }
    if (reason != 0) reportEnd(halt(), reason, now);
  }

  if (static_cast<int32_t>(now - nextTelMs_) >= 0) {
    sendTelemetry();
    nextTelMs_ += kTelemetryPeriodMs;
    // After a long stall, start counting again from now instead of sending a burst.
    if (static_cast<int32_t>(now - nextTelMs_) >= 0) nextTelMs_ = now + kTelemetryPeriodMs;
  }
}

// ---------------------------------------------------------------- requests

void Scout::dispatch(uint32_t now) {
  const uint16_t seq = parser_.seq();
  // Seq 0 marks unprompted UNO messages, so it is never a host request. Ignoring it
  // (no reply, no watchdog reset) also means a TX line looped back to RX can't keep the
  // link watchdog alive with the UNO's own telemetry.
  if (seq == 0) return;
  lastRxMs_ = now;

  if (parser_.is(kPing)) {
    onPing(seq, now);
  } else if (parser_.is(kMove)) {
    onMove(seq, now);
  } else if (parser_.is(kStop)) {
    onStop(seq, now);
  } else if (parser_.is(kTilt)) {
    onTilt(seq);
  } else if (parser_.is(kConf)) {
    onConf(seq);
  } else if (parser_.is(kTest)) {
    onTest(seq, now);
  } else {
    sendErr(seq, kErrType);
  }
}

bool Scout::intArgs(int32_t* values, uint8_t count) const {
  if (parser_.argCount() != count) return false;
  for (uint8_t i = 0; i < count; ++i) {
    if (!parser_.argInt(i, &values[i])) return false;
  }
  return true;
}

void Scout::onPing(uint16_t seq, uint32_t now) {
  if (parser_.argCount() != 0) {
    sendErr(seq, kErrArgs);
    return;
  }
  scoutlink::Builder pong(kPong, seq);
  pong.add(kFirmwareVersion).add(toArg(now / 1000));
  send(pong);
}

void Scout::onMove(uint16_t seq, uint32_t now) {
  int32_t a[4];  // vx, vy, wz, ms
  if (!intArgs(a, 4)) {
    sendErr(seq, kErrArgs);
    return;
  }
  if (!inRange(a[0], -kSpeedMax, kSpeedMax) || !inRange(a[1], -kSpeedMax, kSpeedMax) ||
      !inRange(a[2], -kSpeedMax, kSpeedMax) || !inRange(a[3], 1, kMoveMaxMs)) {
    sendErr(seq, kErrRange);
    return;
  }
  const MotorOutput out = mecanumOutput(static_cast<int8_t>(a[0]), static_cast<int8_t>(a[1]),
                                        static_cast<int8_t>(a[2]), minPwm_, kPwmLayout);
  start(seq, out, static_cast<uint16_t>(a[3]), a[0] > 0, now);
}

void Scout::onTest(uint16_t seq, uint32_t now) {
  int32_t a[4];  // bits, pwm5, pwm6, ms
  if (!intArgs(a, 4)) {
    sendErr(seq, kErrArgs);
    return;
  }
  if (!inRange(a[0], 0, 255) || !inRange(a[1], 0, 255) || !inRange(a[2], 0, 255) ||
      !inRange(a[3], 1, kTestMaxMs)) {
    sendErr(seq, kErrRange);
    return;
  }
  MotorOutput out;
  out.bits = static_cast<uint8_t>(a[0]);
  out.pwm5 = static_cast<uint8_t>(a[1]);
  out.pwm6 = static_cast<uint8_t>(a[2]);
  start(seq, out, static_cast<uint16_t>(a[3]), false, now);
}

void Scout::onStop(uint16_t seq, uint32_t now) {
  if (parser_.argCount() != 0) {
    sendErr(seq, kErrArgs);
    return;
  }
  const Motion ended = halt();
  sendAck(seq);
  reportEnd(ended, kStop, now);
}

void Scout::onTilt(uint16_t seq) {
  int32_t deg;
  if (!intArgs(&deg, 1)) {
    sendErr(seq, kErrArgs);
    return;
  }
  setTilt(clampTilt(deg));
  sendAck(seq, tilt_);
}

void Scout::onConf(uint16_t seq) {
  int32_t value;
  if (parser_.argCount() != 2 || !parser_.argInt(1, &value)) {
    sendErr(seq, kErrArgs);
    return;
  }
  if (parser_.argIs(0, kObst)) {
    if (!inRange(value, kObstacleMinCm, kObstacleMaxCm)) {
      sendErr(seq, kErrRange);
      return;
    }
    obstacleCm_ = static_cast<uint8_t>(value);
  } else if (parser_.argIs(0, kMinPwm)) {
    if (!inRange(value, kMinPwmMin, kMinPwmMax)) {
      sendErr(seq, kErrRange);
      return;
    }
    minPwm_ = static_cast<uint8_t>(value);
  } else if (parser_.argIs(0, kLinkMs)) {
    if (!inRange(value, kLinkMinMs, kLinkMaxMs)) {
      sendErr(seq, kErrRange);
      return;
    }
    linkMs_ = static_cast<uint16_t>(value);
  } else {
    sendErr(seq, kErrArgs);  // unknown key
    return;
  }
  sendAck(seq);
}

// ---------------------------------------------------------------- motion

// Starts a MOVE or TEST, replacing whatever runs. The motors change before any reply
// goes out; then ACK, then DONE NEW for the replaced motion, then DONE OBST if a forward
// move was blocked from the start.
void Scout::start(uint16_t seq, const MotorOutput& out, uint16_t durationMs, bool watchObstacle,
                  uint32_t now) {
  const Motion replaced = halt();
  const int16_t cm = ranger_.cm();
  const bool blocked = watchObstacle && obstacleAt(cm);
  if (!blocked) {
    output(out);
    motion_.seq = seq;
    motion_.watchObstacle = watchObstacle;
    motion_.durationMs = durationMs;
    motion_.startMs = now;
    motion_.cmStart = cm;
  }
  sendAck(seq);
  reportEnd(replaced, kNew, now);
  if (blocked) sendDone(seq, kObst, cm, cm, 0);
}

// Stops the motors and returns what was running (seq 0 if nothing).
Scout::Motion Scout::halt() {
  output(kMotorsOff);
  const Motion ended = motion_;
  motion_.seq = 0;
  return ended;
}

void Scout::reportEnd(const Motion& ended, const char* reason, uint32_t now) {
  if (ended.seq == 0) return;
  sendDone(ended.seq, reason, ended.cmStart, ranger_.cm(), now - ended.startMs);
}

void Scout::output(const MotorOutput& out) {
  hw::writeShiftRegister(out.bits);
  hw::writePwm(out.pwm5, out.pwm6);
}

bool Scout::obstacleAt(int16_t cm) const { return cm >= 0 && cm < obstacleCm_; }

void Scout::setTilt(int8_t deg) {
  tilt_ = deg;
  hw::writeServo(tiltPulseUs(deg));
}

// ---------------------------------------------------------------- replies

void Scout::sendAck(uint16_t seq) { send(scoutlink::Builder(kAck, seq)); }

void Scout::sendAck(uint16_t seq, int32_t value) {
  scoutlink::Builder ack(kAck, seq);
  ack.add(value);
  send(ack);
}

void Scout::sendErr(uint16_t seq, const char* code) {
  scoutlink::Builder err(kErr, seq);
  err.add(code);
  send(err);
}

void Scout::sendDone(uint16_t seq, const char* reason, int16_t cmStart, int16_t cmEnd,
                     uint32_t ranMs) {
  scoutlink::Builder done(kDone, seq);
  done.add(reason)
      .add(static_cast<int32_t>(cmStart))
      .add(static_cast<int32_t>(cmEnd))
      .add(toArg(ranMs));
  send(done);
}

void Scout::sendTelemetry() {
  uint16_t left;
  uint16_t center;
  uint16_t right;
  hw::readLine(&left, &center, &right);
  scoutlink::Builder tel(kTel, 0);
  tel.add(static_cast<int32_t>(ranger_.cm()))
      .add(static_cast<int32_t>(left))
      .add(static_cast<int32_t>(center))
      .add(static_cast<int32_t>(right))
      .add(static_cast<int32_t>(tilt_))
      .add(static_cast<int32_t>(motion_.seq))
      .add(toArg(parser_.rxErrors()));
  send(tel);
}

void Scout::send(const scoutlink::Builder& frame) {
  char buffer[scoutlink::kMaxFrame];
  const size_t length = frame.finish(buffer, sizeof buffer);
  if (length > 0) hw::send(buffer, static_cast<uint8_t>(length));
}

}  // namespace scout
