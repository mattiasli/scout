// Reconnect backoff for the car link (protocol/car-link.md, "Connecting"):
// 2 s, doubling, capped at 60 s, with +-20 % jitter; it starts over once a connection
// has stayed up for 60 s.

#ifndef SCOUT_CORE_BACKOFF_H
#define SCOUT_CORE_BACKOFF_H

#include <stdint.h>

namespace scout {

class Backoff {
 public:
  Backoff(uint32_t baseMs, uint32_t capMs, uint32_t jitterPercent, uint32_t stableMs);

  // Delay before the next attempt; each call doubles the next one. `random` is any
  // uniformly distributed 32-bit value (esp_random() on the car).
  uint32_t nextDelay(uint32_t random);

  // A connection opened at `now`.
  void connected(uint32_t now);
  // The connection that opened with connected() closed at `now`. If it was up for at
  // least `stableMs`, the backoff starts again from the base delay.
  void disconnected(uint32_t now);

  void reset() { attempts_ = 0; }
  uint32_t attempts() const { return attempts_; }

 private:
  uint32_t base_;
  uint32_t cap_;
  uint32_t jitter_;
  uint32_t stable_;
  uint32_t attempts_;
  uint32_t upSince_;
  bool up_;
};

}  // namespace scout

#endif  // SCOUT_CORE_BACKOFF_H
