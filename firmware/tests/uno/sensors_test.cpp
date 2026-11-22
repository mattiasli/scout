// Ultrasonic ranging (non-blocking pings, timeouts, implausible echoes) and tilt mapping.

#include "config.h"
#include "fake_board.h"
#include "ranger.h"
#include "testing.h"
#include "tilt.h"

namespace {

struct RangerRig {
  FakeBoard hw;
  scout::Ranger ranger;
  uint32_t now = 0;
  void tick(uint32_t ms = 1) {
    FakeBoard::active = &hw;
    for (uint32_t i = 0; i < ms; ++i) ranger.update(++now);
  }
};

}  // namespace

TEST(ranger_pings_at_once_and_then_every_60_ms) {
  RangerRig r;
  r.tick();
  CHECK_EQ(r.hw.pings, 1);
  r.tick(59);
  CHECK_EQ(r.hw.pings, 1);
  r.tick();
  CHECK_EQ(r.hw.pings, 2);
  r.tick(600);
  CHECK_EQ(r.hw.pings, 12);
}

TEST(ranger_converts_echo_width_to_cm) {
  RangerRig r;
  CHECK_EQ(r.ranger.cm(), -1);  // nothing measured yet
  r.hw.echoUs = 580;            // 10 cm
  r.tick(2);
  CHECK_EQ(r.ranger.cm(), 10);
  r.hw.echoUs = 58 * 123 + 28;  // rounds down
  r.tick(60);
  CHECK_EQ(r.ranger.cm(), 123);
  r.hw.echoUs = 58 * 123 + 29;  // rounds up
  r.tick(60);
  CHECK_EQ(r.ranger.cm(), 124);
  r.hw.echoUs = 20;  // closer than the module can really see: still a (very near) obstacle
  r.tick(60);
  CHECK_EQ(r.ranger.cm(), 0);
}

TEST(ranger_timeout_gives_minus_one) {
  RangerRig r;
  r.hw.setDistanceCm(50);
  r.tick(2);
  CHECK_EQ(r.ranger.cm(), 50);
  r.hw.setDistanceCm(-1);  // the next ping gets no echo
  r.tick(59);              // t = 61: second ping is out, but not yet timed out
  CHECK_EQ(r.ranger.cm(), 50);
  r.tick(60);  // t = 121: third ping; the second never came back
  CHECK_EQ(r.ranger.cm(), -1);
}

TEST(ranger_accepts_400_cm_and_discards_longer) {
  RangerRig r;
  r.hw.setDistanceCm(400);
  r.tick(2);
  CHECK_EQ(r.ranger.cm(), 400);

  r.hw.setDistanceCm(80);
  r.tick(60);
  CHECK_EQ(r.ranger.cm(), 80);

  // An implausible echo (a 38 ms "nothing found" pulse, say) is dropped: the last good
  // reading stays ...
  r.hw.echoUs = 38000;
  r.tick(60);
  CHECK_EQ(r.ranger.cm(), 80);
  r.hw.setDistanceCm(401);
  r.tick(60);
  CHECK_EQ(r.ranger.cm(), 80);
  // ... but not for ever: kRangeHoldMs after the last good reading it becomes -1.
  r.tick(scout::kRangeHoldMs);
  CHECK_EQ(r.ranger.cm(), -1);

  // A good echo brings it back.
  r.hw.setDistanceCm(33);
  r.tick(61);
  CHECK_EQ(r.ranger.cm(), 33);
}

TEST(tilt_is_clamped_to_the_safe_range) {
  CHECK_EQ(static_cast<int>(scout::clampTilt(0)), 0);
  CHECK_EQ(static_cast<int>(scout::clampTilt(-20)), -20);
  CHECK_EQ(static_cast<int>(scout::clampTilt(40)), 40);
  CHECK_EQ(static_cast<int>(scout::clampTilt(41)), 40);
  CHECK_EQ(static_cast<int>(scout::clampTilt(-41)), -40);
  CHECK_EQ(static_cast<int>(scout::clampTilt(2147483647)), 40);
  CHECK_EQ(static_cast<int>(scout::clampTilt(-2147483647 - 1)), -40);
}

TEST(tilt_maps_to_servo_pulse) {
  // Servo angle = 90 + tilt; 544..2400 us for 0..180 degrees, like the Servo library.
  CHECK_EQ(static_cast<int>(scout::tiltPulseUs(0)), 1472);
  CHECK_EQ(static_cast<int>(scout::tiltPulseUs(-40)), 544 + (50 * 1856 + 90) / 180);
  CHECK_EQ(static_cast<int>(scout::tiltPulseUs(40)), 544 + (130 * 1856 + 90) / 180);
  CHECK(scout::tiltPulseUs(10) > scout::tiltPulseUs(0));  // up = larger angle
  CHECK(scout::tiltPulseUs(-10) < scout::tiltPulseUs(0));
  CHECK_EQ(scout::tiltPulseUs(100), scout::tiltPulseUs(40));  // clamped here too
}
