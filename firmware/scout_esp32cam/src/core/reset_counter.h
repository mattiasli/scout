// "Reset the car 3 times within 10 s to enter setup mode."
//
// The record lives in memory that survives a reset (RTC_NOINIT on the car), so it can
// hold garbage after a power cut: a magic number and a check word guard it. Each
// countable boot adds one; the time the firmware runs is added up across those boots,
// and once that total passes the window the count starts over. Boots that aren't
// countable (software restarts, crashes, brownouts) clear the record, so only a person
// pressing reset (or switching power) can trigger setup mode.

#ifndef SCOUT_CORE_RESET_COUNTER_H
#define SCOUT_CORE_RESET_COUNTER_H

#include <stdint.h>

namespace scout {

struct ResetRecord {
  uint32_t magic;
  uint32_t count;
  uint32_t elapsedMs;  // run time added up over the boots counted so far
  uint32_t check;
};

class ResetCounter {
 public:
  ResetCounter(uint32_t needed, uint32_t windowMs);

  // Call once at boot. Returns true when this boot completes the sequence.
  bool onBoot(ResetRecord* record, bool countable);

  // Call every so often with the time since boot.
  void onUptime(ResetRecord* record, uint32_t uptimeMs);

  bool watching() const { return watching_; }

  static bool valid(const ResetRecord& record);

 private:
  static void store(ResetRecord* record, uint32_t count, uint32_t elapsedMs);
  uint32_t needed_;
  uint32_t window_;
  uint32_t base_;
  bool watching_;
};

}  // namespace scout

#endif  // SCOUT_CORE_RESET_COUNTER_H
