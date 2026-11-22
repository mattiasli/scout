#include "ranger.h"

#include "config.h"
#include "hardware.h"

namespace scout {

Ranger::Ranger() : cm_(-1), pending_(false), started_(false), lastPingMs_(0), lastValidMs_(0) {}

void Ranger::update(uint32_t now) {
  uint32_t widthUs;
  if (pending_ && hw::takeEcho(&widthUs)) {
    pending_ = false;
    const uint32_t cm = (widthUs + kUsPerCm / 2) / kUsPerCm;
    if (cm <= static_cast<uint32_t>(kRangeMaxCm)) {
      cm_ = static_cast<int16_t>(cm);
      lastValidMs_ = now;
    }
    // Longer echoes are implausible (the module reports "nothing found" that way, and
    // glitches look the same), so they are discarded rather than reported.
  }

  // Don't hold on to an old reading when only implausible echoes have come since.
  if (cm_ >= 0 && now - lastValidMs_ >= kRangeHoldMs) cm_ = -1;

  if (!started_ || now - lastPingMs_ >= kRangePeriodMs) {
    if (pending_) cm_ = -1;  // timeout: no echo came back for the last ping
    hw::triggerRanger();
    pending_ = true;
    started_ = true;
    lastPingMs_ = now;
  }
}

}  // namespace scout
