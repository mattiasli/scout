// The UNO side of protocol/uart.md: command handling, the move/test state machine, the
// obstacle stop, the link watchdog and telemetry. Board-independent; it touches the
// hardware only through scout::hw (hardware.h) and gets the time passed in.

#ifndef SCOUT_UNO_SCOUT_H
#define SCOUT_UNO_SCOUT_H

#include <ScoutLink.h>
#include <stdint.h>

#include "mecanum.h"
#include "ranger.h"

namespace scout {

class Scout {
 public:
  Scout();

  // Turns the motors provably off (outputs disabled, zeros latched, outputs enabled),
  // levels the camera and sends BOOT. resetFlags is the MCUSR value read at reset.
  void begin(uint32_t now, uint8_t resetFlags);

  // Feed every byte received from the host.
  void receive(uint8_t byte, uint32_t now);

  // Call every loop: ranging, end of moves, obstacle stop, link watchdog, telemetry.
  void update(uint32_t now);

  // Current state, for tests and diagnostics.
  uint16_t runningSeq() const { return motion_.seq; }  // 0 = nothing running
  int8_t tiltDeg() const { return tilt_; }
  int16_t distanceCm() const { return ranger_.cm(); }
  uint8_t obstacleCm() const { return obstacleCm_; }
  uint8_t minPwm() const { return minPwm_; }
  uint16_t linkMs() const { return linkMs_; }
  uint32_t rxErrors() const { return parser_.rxErrors(); }

 private:
  struct Motion {
    uint16_t seq;         // 0 when nothing runs (hosts number requests from 1)
    bool watchObstacle;   // a MOVE with vx > 0
    uint16_t durationMs;
    uint32_t startMs;
    int16_t cmStart;
  };

  void dispatch(uint32_t now);
  void onPing(uint16_t seq, uint32_t now);
  void onMove(uint16_t seq, uint32_t now);
  void onTest(uint16_t seq, uint32_t now);
  void onStop(uint16_t seq, uint32_t now);
  void onTilt(uint16_t seq);
  void onConf(uint16_t seq);
  bool intArgs(int32_t* values, uint8_t count) const;

  void start(uint16_t seq, const MotorOutput& out, uint16_t durationMs, bool watchObstacle,
             uint32_t now);
  Motion halt();
  void reportEnd(const Motion& ended, const char* reason, uint32_t now);
  void output(const MotorOutput& out);
  bool obstacleAt(int16_t cm) const;
  void setTilt(int8_t deg);

  void sendAck(uint16_t seq);
  void sendAck(uint16_t seq, int32_t value);
  void sendErr(uint16_t seq, const char* code);
  void sendDone(uint16_t seq, const char* reason, int16_t cmStart, int16_t cmEnd, uint32_t ranMs);
  void sendTelemetry();
  void send(const scoutlink::Builder& frame);

  scoutlink::Parser parser_;
  Ranger ranger_;
  Motion motion_;
  uint8_t obstacleCm_;
  uint8_t minPwm_;
  uint16_t linkMs_;
  int8_t tilt_;
  uint32_t lastRxMs_;   // last valid frame from the host
  uint32_t nextTelMs_;
};

}  // namespace scout

#endif  // SCOUT_UNO_SCOUT_H
