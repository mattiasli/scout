#include "backoff.h"

namespace scout {

Backoff::Backoff(uint32_t baseMs, uint32_t capMs, uint32_t jitterPercent, uint32_t stableMs)
    : base_(baseMs),
      cap_(capMs),
      jitter_(jitterPercent > 100 ? 100 : jitterPercent),
      stable_(stableMs),
      attempts_(0),
      upSince_(0),
      up_(false) {}

uint32_t Backoff::nextDelay(uint32_t random) {
  uint64_t delay = base_;
  for (uint32_t i = 0; i < attempts_ && delay < cap_; ++i) delay *= 2;
  if (delay > cap_) delay = cap_;
  if (attempts_ < 32) ++attempts_;

  // Scale by a factor in [100 - jitter, 100 + jitter] percent.
  uint32_t span = 2 * jitter_ + 1;
  uint32_t percent = 100 - jitter_ + random % span;
  return static_cast<uint32_t>(delay * percent / 100);
}

void Backoff::connected(uint32_t now) {
  up_ = true;
  upSince_ = now;
}

void Backoff::disconnected(uint32_t now) {
  if (up_ && now - upSince_ >= stable_) attempts_ = 0;
  up_ = false;
}

}  // namespace scout
