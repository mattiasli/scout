// HC-SR04 ultrasonic ranging without blocking: a ping every kRangePeriodMs, the echo is
// timed by the board layer (pin-change interrupt), and this turns it into centimetres.

#ifndef SCOUT_UNO_RANGER_H
#define SCOUT_UNO_RANGER_H

#include <stdint.h>

namespace scout {

class Ranger {
 public:
  Ranger();

  // Call every loop. Collects a finished echo and sends the next ping when it's due.
  void update(uint32_t now);

  // Front distance in cm, or -1 when there is no usable echo.
  int16_t cm() const { return cm_; }

 private:
  int16_t cm_;
  bool pending_;  // a ping is out and its echo hasn't come back
  bool started_;
  uint32_t lastPingMs_;
  uint32_t lastValidMs_;
};

}  // namespace scout

#endif  // SCOUT_UNO_RANGER_H
