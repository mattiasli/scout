#include "reset_counter.h"

namespace scout {

namespace {

const uint32_t kMagic = 0x53435254;  // "SCRT"

uint32_t checkWord(uint32_t count, uint32_t elapsedMs) {
  return (kMagic ^ 0xA5A5A5A5u) ^ (count * 2654435761u) ^ (elapsedMs + 0x9E3779B9u);
}

}  // namespace

ResetCounter::ResetCounter(uint32_t needed, uint32_t windowMs)
    : needed_(needed), window_(windowMs), base_(0), watching_(false) {}

bool ResetCounter::valid(const ResetRecord& r) {
  return r.magic == kMagic && r.check == checkWord(r.count, r.elapsedMs);
}

void ResetCounter::store(ResetRecord* r, uint32_t count, uint32_t elapsedMs) {
  r->magic = kMagic;
  r->count = count;
  r->elapsedMs = elapsedMs;
  r->check = checkWord(count, elapsedMs);
}

bool ResetCounter::onBoot(ResetRecord* r, bool countable) {
  watching_ = false;
  uint32_t count = 0;
  uint32_t elapsed = 0;
  if (valid(*r) && r->elapsedMs < window_) {
    count = r->count;
    elapsed = r->elapsedMs;
  }
  if (!countable) {
    store(r, 0, 0);
    return false;
  }
  ++count;
  if (count >= needed_) {
    store(r, 0, 0);
    return true;
  }
  store(r, count, elapsed);
  base_ = elapsed;
  watching_ = true;
  return false;
}

void ResetCounter::onUptime(ResetRecord* r, uint32_t uptimeMs) {
  if (!watching_) return;
  uint32_t total = base_ + uptimeMs;
  if (total >= window_ || total < base_) {
    store(r, 0, 0);
    watching_ = false;
    return;
  }
  store(r, valid(*r) ? r->count : 0, total);
}

}  // namespace scout
