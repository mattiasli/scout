// The UNO side of protocol/uart.md, driven through a fake board and a fake clock.

#include <string>
#include <vector>

#include "config.h"
#include "fake_board.h"
#include "mecanum.h"
#include "testing.h"
#include "tilt.h"

namespace {

// Compares everything sent since the last take() (minus TEL and PONG) with a list.
#define CHECK_SENT(rig, ...) \
  CHECK_EQ(joined((rig).takeText()), joined(std::vector<std::string>{__VA_ARGS__}))

// True if anything other than "all off" reached the motor outputs.
bool motorsDriven(const FakeBoard& hw) {
  for (const std::string& entry : hw.log) {
    if (entry.compare(0, 3, "sr ") == 0 && entry != "sr 0") return true;
    if (entry.compare(0, 4, "pwm ") == 0 && entry != "pwm 0 0") return true;
  }
  return false;
}

// Number of writes to the motor outputs (shift register or PWM), whatever the value.
int motorWrites(const FakeBoard& hw) {
  int count = 0;
  for (const std::string& entry : hw.log) {
    if (entry.compare(0, 3, "sr ") == 0 || entry.compare(0, 4, "pwm ") == 0) ++count;
  }
  return count;
}

// Index of the first log entry equal to `entry`, or -1.
int logIndex(const FakeBoard& hw, const std::string& entry) {
  for (size_t i = 0; i < hw.log.size(); ++i) {
    if (hw.log[i] == entry) return static_cast<int>(i);
  }
  return -1;
}

// A booted rig with the BOOT frame and the boot log already cleared.
struct Booted : Rig {
  Booted() : Rig(true) {
    take();
    hw.log.clear();
  }
};

int pwmFor(int percent, int minPwm = 90) {
  return scout::percentToPwm(static_cast<uint8_t>(percent), static_cast<uint8_t>(minPwm));
}

}  // namespace

// ---------------------------------------------------------------- boot

TEST(nothing_touches_the_hardware_before_begin) {
  Rig r(false);
  CHECK(r.hw.log.empty());
}

TEST(boot_disables_outputs_then_latches_zeros_then_enables_then_sends_boot) {
  Rig r(false);
  r.car.begin(0, 0);
  CHECK_EQ(joined(r.hw.log), "enable 0 | pwm 0 0 | sr 0 | enable 1 | servo 1379 | tx BOOT");
  CHECK_EQ(static_cast<int>(r.hw.bits), 0);
  CHECK(r.hw.driverEnabled);
  CHECK_SENT(r, "BOOT,0,0.1.0,0");
  CHECK_EQ(r.hw.badFrames, 0u);
}

TEST(boot_reports_the_reset_flags) {
  Rig r(true, 0x08);  // WDRF
  CHECK_SENT(r, "BOOT,0,0.1.0,8");
  Rig s(true, 0x0F);
  CHECK_SENT(s, "BOOT,0,0.1.0,15");
}

TEST(motors_stay_off_after_boot_without_requests) {
  Booted r;
  r.hw.setDistanceCm(30);
  r.tick(3000);
  CHECK(!motorsDriven(r.hw));
  CHECK_EQ(r.car.runningSeq(), 0u);
  CHECK_SENT(r);  // nothing but telemetry
}

// ---------------------------------------------------------------- PING and errors

TEST(ping_answers_with_version_and_uptime) {
  Booted r;
  r.rx("PING,1");
  CHECK_EQ(joined(r.takeText(true)), "PONG,1,0.1.0,0");
  r.tick(42999);
  r.take(true);
  r.rx("PING,65535");
  CHECK_EQ(joined(r.takeText(true)), "PONG,65535,0.1.0,42");
}

TEST(unknown_types_get_err_type) {
  Booted r;
  r.rx("JUMP,3");
  r.rx("ACK,4");               // UNO-to-host types aren't requests
  r.rx("PONG,5,0.1.0,1");
  r.rx("ABCDEFGH,6,1,2,3,4,5,6,7,8");
  CHECK_SENT(r, "ERR,3,TYPE", "ERR,4,TYPE", "ERR,5,TYPE", "ERR,6,TYPE");
  CHECK(!motorsDriven(r.hw));
}

TEST(malformed_args_get_err_args) {
  Booted r;
  const char* bodies[] = {
      "PING,1,0",
      "STOP,2,1",
      "MOVE,3",
      "MOVE,4,10,0,0",
      "MOVE,5,10,0,0,100,1",
      "MOVE,6,abc,0,0,100",
      "MOVE,7,10,0,0,1.5",
      "MOVE,8,+10,0,0,100",
      "MOVE,9,010,0,0,100",
      "MOVE,10,10,,0,100",
      "MOVE,11,99999999999,0,0,100",
      "MOVE,12,10,0,0,-",
      "TILT,13",
      "TILT,14,up",
      "TILT,15,1,2",
      "CONF,16,OBST",
      "CONF,17,OBST,ten",
      "CONF,18,SPEED,10",
      "CONF,19,obst,10",
      "CONF,20,OBST,10,1",
      "CONF,21",
      "TEST,22,163,100,100",
      "TEST,23,163,100,100,x",
      "TEST,24,163,100,100,500,1",
  };
  std::vector<std::string> expected;
  for (const char* body : bodies) {
    r.rx(body);
    std::string text(body);
    size_t first = text.find(',');
    size_t second = text.find(',', first + 1);
    expected.push_back("ERR," + text.substr(first + 1, second - first - 1) + ",ARGS");
  }
  CHECK_EQ(joined(r.takeText()), joined(expected));
  CHECK(!motorsDriven(r.hw));
  CHECK_EQ(r.car.obstacleCm(), 15);
  CHECK_EQ(r.car.tiltDeg(), 0);
  CHECK_EQ(r.hw.badFrames, 0u);
}

TEST(out_of_range_values_get_err_range) {
  Booted r;
  const char* bodies[] = {
      "MOVE,1,101,0,0,100",     "MOVE,2,-101,0,0,100",   "MOVE,3,0,101,0,100",
      "MOVE,4,0,-101,0,100",    "MOVE,5,0,0,101,100",    "MOVE,6,0,0,-101,100",
      "MOVE,7,10,0,0,0",        "MOVE,8,10,0,0,5001",    "MOVE,9,10,0,0,-1",
      "MOVE,10,-2147483648,0,0,100",
      "TEST,11,256,0,0,100",    "TEST,12,-1,0,0,100",    "TEST,13,163,256,0,100",
      "TEST,14,163,0,-1,100",   "TEST,15,163,0,0,0",     "TEST,16,163,0,0,2001",
      "CONF,17,OBST,4",         "CONF,18,OBST,101",      "CONF,19,MINPWM,-1",
      "CONF,20,MINPWM,256",     "CONF,21,LINKMS,299",    "CONF,22,LINKMS,5001",
  };
  std::vector<std::string> expected;
  for (const char* body : bodies) {
    r.rx(body);
    std::string text(body);
    size_t first = text.find(',');
    size_t second = text.find(',', first + 1);
    expected.push_back("ERR," + text.substr(first + 1, second - first - 1) + ",RANGE");
  }
  CHECK_EQ(joined(r.takeText()), joined(expected));
  CHECK(!motorsDriven(r.hw));
  CHECK_EQ(r.car.obstacleCm(), 15);
  CHECK_EQ(r.car.minPwm(), 90);
  CHECK_EQ(r.car.linkMs(), 1000);
}

TEST(range_limits_themselves_are_accepted) {
  Booted r;
  r.rx("MOVE,1,100,0,0,1");
  r.rx("MOVE,2,-100,-100,-100,5000");
  r.rx("TEST,3,255,255,255,2000");
  r.rx("TEST,4,0,0,0,1");
  r.rx("CONF,5,OBST,5");
  r.rx("CONF,6,OBST,100");
  r.rx("CONF,7,MINPWM,0");
  r.rx("CONF,8,MINPWM,255");
  r.rx("CONF,9,LINKMS,300");
  r.rx("CONF,10,LINKMS,5000");
  std::vector<std::string> sent = r.takeText();
  int acks = 0;
  for (const std::string& s : sent) {
    if (s.compare(0, 4, "ACK,") == 0) ++acks;
    CHECK(s.compare(0, 4, "ERR,") != 0);
  }
  CHECK_EQ(acks, 10);
}

TEST(a_rejected_request_changes_nothing) {
  Booted r;
  r.rx("MOVE,1,50,0,0,2000");
  r.take();
  r.hw.log.clear();
  r.rx("MOVE,2,500,0,0,2000");
  r.rx("TEST,3,163,0,0,9999");
  r.rx("STOP,4,now");
  CHECK_SENT(r, "ERR,2,RANGE", "ERR,3,RANGE", "ERR,4,ARGS");
  CHECK_EQ(motorWrites(r.hw), 0);  // not a single write to the motor outputs
  CHECK_EQ(r.car.runningSeq(), 1u);
}

TEST(bad_frames_are_dropped_and_counted) {
  Booted r;
  r.rxRaw("$MOVE,1,100,0,0,5000*00\n");  // wrong CRC
  r.rxRaw(frameFor("move,2,100,0,0,5000"));  // right CRC, but a lowercase type
  r.rxRaw("$MOVE,3,100,0,0,5000");        // cut off by the next '$'
  r.rx("PING,4");
  CHECK_EQ(joined(r.takeText(true)), "PONG,4,0.1.0,0");
  CHECK(!motorsDriven(r.hw));
  CHECK_EQ(r.car.rxErrors(), 3u);
}

TEST(noise_between_frames_is_ignored) {
  Booted r;
  r.rxRaw("ets Jun  8 2016 00:22:57\r\nrst:0x1 (POWERON_RESET),boot:0x13\r\n\x01\xff");
  r.rx("STOP,5");
  r.rxRaw("\r\nload:0x3fff0018,len:4\r\n");
  CHECK_SENT(r, "ACK,5");
  CHECK_EQ(r.car.rxErrors(), 0u);
}

TEST(seq_zero_is_not_a_request) {
  Booted r;
  r.rx("PING,0");
  r.rx("MOVE,0,50,0,0,1000");
  r.rx("JUMP,0");
  CHECK_EQ(joined(r.takeText(true)), "");
  CHECK(!motorsDriven(r.hw));
}

// ---------------------------------------------------------------- MOVE lifecycle

TEST(move_runs_for_its_time_then_reports_done_time) {
  Booted r;
  r.hw.setDistanceCm(95);
  r.settleRange();
  r.take();
  r.hw.log.clear();
  const uint32_t start = r.now;
  r.rx("MOVE,5,60,0,0,800");
  CHECK_SENT(r, "ACK,5");
  CHECK_EQ(static_cast<int>(r.hw.bits), 163);
  CHECK_EQ(static_cast<int>(r.hw.pwm5), pwmFor(60));
  CHECK_EQ(static_cast<int>(r.hw.pwm6), pwmFor(60));
  CHECK_EQ(r.car.runningSeq(), 5u);
  // The motors were set before the ACK went out.
  CHECK(logIndex(r.hw, "sr 163") >= 0);
  CHECK(logIndex(r.hw, "sr 163") < logIndex(r.hw, "tx ACK"));

  r.hw.setDistanceCm(71);
  r.tick(799);
  CHECK_SENT(r);
  CHECK_EQ(r.car.runningSeq(), 5u);
  r.tick();
  std::vector<Sent> done = r.take();
  CHECK_EQ(done.size(), 1u);
  if (!done.empty()) {
    CHECK_EQ(done[0].text(), "DONE,5,TIME,95,71,800");
    CHECK_EQ(done[0].at, start + 800);
  }
  CHECK_EQ(static_cast<int>(r.hw.bits), 0);
  CHECK_EQ(static_cast<int>(r.hw.pwm5), 0);
  CHECK_EQ(static_cast<int>(r.hw.pwm6), 0);
  CHECK_EQ(r.car.runningSeq(), 0u);
  CHECK_EQ(r.hw.badFrames, 0u);
}

TEST(each_direction_reaches_the_shift_register) {
  struct Case {
    const char* body;
    int bits;
  };
  const Case cases[] = {
      {"MOVE,1,50,0,0,100", 163},   {"MOVE,2,-50,0,0,100", 92},  {"MOVE,3,0,-50,0,100", 106},
      {"MOVE,4,0,50,0,100", 149},   {"MOVE,5,50,-50,0,100", 34}, {"MOVE,6,50,50,0,100", 129},
      {"MOVE,7,-50,-50,0,100", 72}, {"MOVE,8,-50,50,0,100", 20}, {"MOVE,9,0,0,50,100", 172},
      {"MOVE,10,0,0,-50,100", 83},  {"MOVE,11,0,0,0,100", 0},
  };
  for (const Case& c : cases) {
    Booted r;
    r.rx(c.body);
    CHECK_EQ(static_cast<int>(r.hw.bits), c.bits);
  }
}

TEST(minpwm_sets_the_bottom_of_the_speed_scale) {
  Booted r;
  r.rx("MOVE,1,1,0,0,100");
  CHECK_EQ(static_cast<int>(r.hw.pwm5), 90);  // default MINPWM
  r.rx("CONF,2,MINPWM,200");
  CHECK_EQ(r.car.minPwm(), 200);
  r.rx("MOVE,3,1,0,0,100");
  CHECK_EQ(static_cast<int>(r.hw.pwm5), 200);
  CHECK_EQ(static_cast<int>(r.hw.pwm6), 200);
  r.rx("MOVE,4,100,0,0,100");
  CHECK_EQ(static_cast<int>(r.hw.pwm5), 255);
  r.rx("MOVE,5,50,0,0,100");
  CHECK_EQ(static_cast<int>(r.hw.pwm5), pwmFor(50, 200));
}

TEST(forward_move_into_an_obstacle_never_starts) {
  Booted r;
  r.hw.setDistanceCm(10);
  r.settleRange();
  r.take();
  r.hw.log.clear();
  r.rx("MOVE,6,30,0,0,1000");
  CHECK_SENT(r, "ACK,6", "DONE,6,OBST,10,10,0");
  CHECK(!motorsDriven(r.hw));
  CHECK_EQ(r.car.runningSeq(), 0u);
  r.tick(1500);
  CHECK_SENT(r);  // no second DONE
}

TEST(forward_move_stops_when_something_comes_close) {
  Booted r;
  r.hw.setDistanceCm(50);
  r.settleRange();
  r.take();
  const uint32_t start = r.now;
  r.rx("MOVE,7,40,0,0,3000");
  r.tick(100);
  CHECK_EQ(r.car.runningSeq(), 7u);
  r.hw.setDistanceCm(12);
  r.tick(scout::kRangePeriodMs + 1);
  std::vector<Sent> sent = r.take();
  CHECK_EQ(sent.size(), 2u);
  if (sent.size() == 2) {
    CHECK_EQ(sent[0].text(), "ACK,7");
    CHECK_EQ(sent[1].type, "DONE");
    CHECK_EQ(sent[1].arg(0), "OBST");
    CHECK_EQ(sent[1].arg(1), "50");
    CHECK_EQ(sent[1].arg(2), "12");
    CHECK_EQ(sent[1].arg(3), std::to_string(sent[1].at - start));
    CHECK(sent[1].at - start > 100 && sent[1].at - start <= 100 + scout::kRangePeriodMs + 1);
  }
  CHECK_EQ(static_cast<int>(r.hw.bits), 0);
  CHECK_EQ(r.car.runningSeq(), 0u);
}

TEST(obstacle_threshold_is_strictly_below_obst) {
  {
    Booted r;
    r.hw.setDistanceCm(15);  // default OBST is 15: not closer, so it runs
    r.settleRange();
    r.take();
    r.rx("MOVE,1,30,0,0,500");
    CHECK_SENT(r, "ACK,1");
    CHECK_EQ(r.car.runningSeq(), 1u);
  }
  {
    Booted r;
    r.hw.setDistanceCm(14);
    r.settleRange();
    r.take();
    r.rx("MOVE,1,30,0,0,500");
    CHECK_SENT(r, "ACK,1", "DONE,1,OBST,14,14,0");
  }
  {
    Booted r;  // no echo means nothing seen: the move runs
    r.settleRange();
    r.take();
    r.rx("MOVE,1,30,0,0,500");
    CHECK_SENT(r, "ACK,1");
    CHECK_EQ(r.car.runningSeq(), 1u);
  }
}

TEST(conf_obst_moves_the_threshold) {
  Booted r;
  r.hw.setDistanceCm(25);
  r.settleRange();
  r.rx("CONF,1,OBST,30");
  CHECK_EQ(r.car.obstacleCm(), 30);
  r.take();
  r.rx("MOVE,2,30,0,0,500");
  CHECK_SENT(r, "ACK,2", "DONE,2,OBST,25,25,0");
  r.rx("CONF,3,OBST,20");
  r.rx("MOVE,4,30,0,0,500");
  CHECK_SENT(r, "ACK,3", "ACK,4");
  CHECK_EQ(r.car.runningSeq(), 4u);
}

TEST(obstacle_is_only_checked_while_vx_is_positive) {
  const char* moves[] = {"MOVE,1,-50,0,0,500", "MOVE,1,0,50,0,500", "MOVE,1,0,-50,0,500",
                         "MOVE,1,0,0,50,500", "MOVE,1,0,0,0,500"};
  for (const char* body : moves) {
    Booted r;
    r.hw.setDistanceCm(5);
    r.settleRange();
    r.take();
    r.rx(body);
    r.tick(499);
    CHECK_SENT(r, "ACK,1");
    r.tick();
    CHECK_SENT(r, "DONE,1,TIME,5,5,500");
  }
}

TEST(stop_halts_at_once_and_reports_the_running_move) {
  Booted r;
  r.rx("MOVE,8,50,0,0,2000");
  r.tick(300);
  r.take();
  r.hw.log.clear();
  r.rx("STOP,9");
  CHECK_SENT(r, "ACK,9", "DONE,8,STOP,-1,-1,300");
  CHECK_EQ(static_cast<int>(r.hw.bits), 0);
  CHECK_EQ(static_cast<int>(r.hw.pwm5), 0);
  CHECK(logIndex(r.hw, "sr 0") < logIndex(r.hw, "tx ACK"));
  CHECK_EQ(r.car.runningSeq(), 0u);
  r.tick(3000);
  CHECK_SENT(r);
}

TEST(stop_when_idle_still_writes_motors_off) {
  Booted r;
  r.rx("STOP,10");
  CHECK_SENT(r, "ACK,10");
  CHECK(logIndex(r.hw, "sr 0") >= 0);
  CHECK(logIndex(r.hw, "pwm 0 0") >= 0);
}

TEST(silent_host_stops_the_move_with_done_link) {
  Booted r;
  const uint32_t start = r.now;
  r.rx("MOVE,11,50,0,0,3000");
  r.tick(999);
  CHECK_EQ(r.car.runningSeq(), 11u);
  r.tick();
  std::vector<Sent> sent = r.take();
  CHECK_EQ(sent.size(), 2u);
  if (sent.size() == 2) {
    CHECK_EQ(sent[1].text(), "DONE,11,LINK,-1,-1,1000");
    CHECK_EQ(sent[1].at, start + 1000);
  }
  CHECK_EQ(static_cast<int>(r.hw.bits), 0);
}

TEST(pings_keep_a_long_move_alive) {
  Booted r;
  r.rx("MOVE,12,50,0,0,3000");
  r.tickWithPings(2999);
  CHECK_EQ(r.car.runningSeq(), 12u);
  r.tickWithPings(1);
  CHECK_SENT(r, "ACK,12", "DONE,12,TIME,-1,-1,3000");
}

TEST(bad_frames_and_seq_zero_frames_do_not_feed_the_link_watchdog) {
  Booted r;
  r.rx("MOVE,13,50,0,0,3000");
  for (int i = 0; i < 9; ++i) {
    r.tick(100);
    r.rxRaw("$PING,14*00\n");  // bad CRC
    r.rx("PING,0");            // seq 0
  }
  r.tick(100);
  CHECK_SENT(r, "ACK,13", "DONE,13,LINK,-1,-1,1000");
}

TEST(any_valid_request_feeds_the_link_watchdog_even_if_rejected) {
  Booted r;
  r.rx("MOVE,15,50,0,0,2000");
  for (int i = 0; i < 7; ++i) {
    r.tick(250);
    r.rx("JUMP,16");  // ERR TYPE, but the host is clearly there
  }
  CHECK_EQ(r.car.runningSeq(), 15u);
  r.tick(250);
  std::vector<std::string> sent = r.takeText();
  CHECK(!sent.empty() && sent.back() == "DONE,15,TIME,-1,-1,2000");
}

TEST(linkms_sets_the_watchdog) {
  {
    Booted r;
    r.rx("CONF,1,LINKMS,300");
    r.rx("MOVE,2,50,0,0,1000");
    r.tick(299);
    CHECK_EQ(r.car.runningSeq(), 2u);
    r.tick();
    CHECK_SENT(r, "ACK,1", "ACK,2", "DONE,2,LINK,-1,-1,300");
  }
  {
    Booted r;  // a 5 s watchdog lets a 5 s move finish without pings
    r.rx("CONF,1,LINKMS,5000");
    r.rx("MOVE,2,50,0,0,5000");
    r.tick(5000);
    CHECK_SENT(r, "ACK,1", "ACK,2", "DONE,2,TIME,-1,-1,5000");
  }
}

TEST(the_watchdog_only_matters_while_moving) {
  Booted r;
  r.tick(10000);  // no host at all
  r.rx("MOVE,1,50,0,0,500");
  r.tick(500);
  CHECK_SENT(r, "ACK,1", "DONE,1,TIME,-1,-1,500");
}

TEST(a_new_move_replaces_the_running_one) {
  Booted r;
  r.rx("MOVE,20,50,0,0,2000");
  r.tick(200);
  r.take();
  r.rx("MOVE,21,0,50,0,1000");
  CHECK_SENT(r, "ACK,21", "DONE,20,NEW,-1,-1,200");
  CHECK_EQ(static_cast<int>(r.hw.bits), 149);
  CHECK_EQ(r.car.runningSeq(), 21u);

  r.tick(100);
  r.rx("TEST,22,128,100,0,1000");  // TEST replaces a MOVE ...
  CHECK_SENT(r, "ACK,22", "DONE,21,NEW,-1,-1,100");
  CHECK_EQ(static_cast<int>(r.hw.bits), 128);

  r.tick(50);
  r.rx("MOVE,23,-30,0,0,400");  // ... and a MOVE replaces a TEST
  CHECK_SENT(r, "ACK,23", "DONE,22,NEW,-1,-1,50");
  CHECK_EQ(static_cast<int>(r.hw.bits), 92);

  r.tick(400);
  CHECK_SENT(r, "DONE,23,TIME,-1,-1,400");
}

TEST(a_blocked_new_move_still_ends_the_old_one) {
  Booted r;
  r.hw.setDistanceCm(10);
  r.settleRange();
  r.rx("MOVE,30,-50,0,0,2000");  // backing away is allowed
  r.tick(100);
  r.take();
  r.rx("MOVE,31,50,0,0,1000");
  CHECK_SENT(r, "ACK,31", "DONE,30,NEW,10,10,100", "DONE,31,OBST,10,10,0");
  CHECK_EQ(static_cast<int>(r.hw.bits), 0);
  CHECK_EQ(r.car.runningSeq(), 0u);
}

// ---------------------------------------------------------------- TEST

TEST(test_drives_the_raw_outputs_for_its_time) {
  Booted r;
  r.rx("TEST,40,163,200,0,500");
  CHECK_SENT(r, "ACK,40");
  CHECK_EQ(static_cast<int>(r.hw.bits), 163);
  CHECK_EQ(static_cast<int>(r.hw.pwm5), 200);
  CHECK_EQ(static_cast<int>(r.hw.pwm6), 0);
  CHECK_EQ(r.car.runningSeq(), 40u);
  r.tick(499);
  CHECK_SENT(r);
  r.tick();
  CHECK_SENT(r, "DONE,40,TIME,-1,-1,500");
  CHECK_EQ(static_cast<int>(r.hw.bits), 0);
}

TEST(test_has_no_obstacle_check) {
  Booted r;
  r.hw.setDistanceCm(3);
  r.settleRange();
  r.take();
  r.rx("TEST,41,163,150,150,300");
  r.tick(300);
  CHECK_SENT(r, "ACK,41", "DONE,41,TIME,3,3,300");
}

TEST(test_is_bounded_by_the_link_watchdog_and_stop) {
  {
    Booted r;
    r.rx("TEST,42,163,150,150,2000");
    r.tick(1000);
    CHECK_SENT(r, "ACK,42", "DONE,42,LINK,-1,-1,1000");
  }
  {
    Booted r;
    r.rx("TEST,43,163,150,150,2000");
    r.tick(10);
    r.rx("STOP,44");
    CHECK_SENT(r, "ACK,43", "ACK,44", "DONE,43,STOP,-1,-1,10");
  }
}

// ---------------------------------------------------------------- CONF and TILT

TEST(conf_defaults_and_updates) {
  Booted r;
  CHECK_EQ(r.car.obstacleCm(), scout::kObstacleDefaultCm);
  CHECK_EQ(r.car.minPwm(), scout::kMinPwmDefault);
  CHECK_EQ(r.car.linkMs(), scout::kLinkDefaultMs);
  CHECK_EQ(r.car.obstacleCm(), 15);
  CHECK_EQ(r.car.minPwm(), 90);
  CHECK_EQ(r.car.linkMs(), 1000);

  r.rx("CONF,1,OBST,40");
  r.rx("CONF,2,MINPWM,120");
  r.rx("CONF,3,LINKMS,2500");
  CHECK_SENT(r, "ACK,1", "ACK,2", "ACK,3");
  CHECK_EQ(r.car.obstacleCm(), 40);
  CHECK_EQ(r.car.minPwm(), 120);
  CHECK_EQ(r.car.linkMs(), 2500);

  Rig rebooted;  // settings live in RAM: a reset brings the defaults back
  CHECK_EQ(rebooted.car.obstacleCm(), 15);
  CHECK_EQ(rebooted.car.minPwm(), 90);
  CHECK_EQ(rebooted.car.linkMs(), 1000);
}

TEST(tilt_is_clamped_and_acknowledged_with_the_applied_angle) {
  Booted r;
  r.rx("TILT,1,-20");
  CHECK_SENT(r, "ACK,1,-20");
  CHECK_EQ(r.hw.servoUs, scout::tiltPulseUs(-20));
  CHECK_EQ(r.car.tiltDeg(), -20);
  r.rx("TILT,2,90");
  r.rx("TILT,3,-90");
  r.rx("TILT,4,2147483647");
  r.rx("TILT,5,0");
  CHECK_SENT(r, "ACK,2,40", "ACK,3,-40", "ACK,4,40", "ACK,5,0");
  CHECK_EQ(r.hw.servoUs, 1379);
  CHECK(!motorsDriven(r.hw));
}

TEST(tilt_works_while_moving) {
  Booted r;
  r.rx("MOVE,1,50,0,0,1000");
  r.tick(100);
  r.rx("TILT,2,15");
  CHECK_EQ(r.car.runningSeq(), 1u);
  CHECK_EQ(static_cast<int>(r.hw.bits), 163);
  CHECK_SENT(r, "ACK,1", "ACK,2,15");
}

// ---------------------------------------------------------------- telemetry

TEST(telemetry_every_200_ms_with_its_fields) {
  Booted r;
  r.hw.line[0] = 512;
  r.hw.line[1] = 600;
  r.hw.line[2] = 1023;
  r.tick(199);
  CHECK(r.ofType("TEL").empty());
  r.tick();
  std::vector<Sent> tel = r.ofType("TEL");
  CHECK_EQ(tel.size(), 1u);
  if (!tel.empty()) {
    CHECK_EQ(tel[0].text(), "TEL,0,-1,512,600,1023,0,0,0,-2");  // -2: no laser sensor fitted
    CHECK_EQ(tel[0].at, 200u);
  }
  r.tick(800);
  tel = r.ofType("TEL");
  CHECK_EQ(tel.size(), 5u);
  for (size_t i = 0; i < tel.size(); ++i) CHECK_EQ(tel[i].at, 200u * (i + 1));
}

TEST(telemetry_shows_distance_tilt_move_and_rx_errors) {
  Booted r;
  r.hw.setDistanceCm(42);
  r.rx("TILT,1,-30");
  r.rxRaw("$PING,2*00\n");
  r.rxRaw("$PING,3*00\n");
  r.rx("MOVE,77,-40,0,0,1000");
  r.take(true);
  r.tick(200);
  std::vector<Sent> tel = r.ofType("TEL");
  CHECK_EQ(tel.size(), 1u);
  if (!tel.empty()) CHECK_EQ(tel[0].text(), "TEL,0,42,0,0,0,-30,77,2,-2");
}

TEST(telemetry_ends_with_the_laser_distance) {
  Booted r;
  r.tick(200);
  r.hw.laser.present = true;  // fitted after the first look for it
  r.hw.laser.mm = 1234;
  r.tick(scout::kLaserRetryMs + 400);
  std::vector<Sent> tel = r.ofType("TEL");
  CHECK(!tel.empty());
  if (!tel.empty()) {
    CHECK_EQ(tel.front().arg(7), "-2");
    CHECK_EQ(tel.back().arg(7), "123");
  }
  CHECK_EQ(r.car.laserCm(), 123);
  CHECK_EQ(r.car.distanceCm(), -1);  // the ultrasonic reading stays separate
}

TEST(telemetry_does_not_drift_or_stop) {
  Booted r;
  r.rx("MOVE,1,50,0,0,5000");
  r.tickWithPings(60000);
  CHECK_EQ(r.ofType("TEL").size(), 300u);
  CHECK_EQ(r.hw.badFrames, 0u);
}

TEST(telemetry_after_a_stall_restarts_without_a_burst) {
  Booted r;
  r.tick(100);
  r.now += 5000;  // the loop didn't run for 5 s
  r.tick();
  CHECK_EQ(r.ofType("TEL").size(), 1u);
  r.tick(199);
  CHECK_EQ(r.ofType("TEL").size(), 1u);
  r.tick();
  CHECK_EQ(r.ofType("TEL").size(), 2u);
}
