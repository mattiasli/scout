// The VL53L1X laser distance sensor: finding it, starting it the way ST's driver does,
// readings, and what happens when it's missing or misbehaves.

#include "config.h"
#include "fake_board.h"
#include "laser.h"
#include "testing.h"

namespace {

struct LaserRig {
  FakeBoard hw;
  scout::Laser laser;
  uint32_t now = 0;

  explicit LaserRig(bool fitted = true) { hw.laser.present = fitted; }

  void tick(uint32_t ms = 1) {
    for (uint32_t i = 0; i < ms; ++i) {
      ++now;
      FakeBoard::active = &hw;
      hw.clock = now;
      laser.update(now);
    }
  }

  // Runs until the sensor measures continuously (its second start; the first one is the
  // calibration measurement), for at most a second.
  void start() {
    for (int i = 0; i < 1000 && hw.laser.starts < 2; ++i) tick();
  }

  int reg(uint16_t r) const {
    const auto it = hw.laser.regs.find(r);
    return it == hw.laser.regs.end() ? -1 : it->second;
  }
  long reg16(uint16_t r) const { return (static_cast<long>(reg(r)) << 8) | reg(r + 1); }
  long reg32(uint16_t r) const { return (reg16(r) << 16) | reg16(r + 2); }
};

}  // namespace

TEST(laser_missing_reads_minus_two_and_is_looked_for_again) {
  LaserRig r(false);
  CHECK_EQ(r.laser.cm(), -1);  // nothing known yet
  r.tick();
  CHECK_EQ(r.laser.cm(), scout::kLaserMissing);
  CHECK_EQ(r.hw.laser.exchanges, 1);
  r.tick(scout::kLaserRetryMs - 1);
  CHECK_EQ(r.hw.laser.exchanges, 1);  // the bus is left alone in between
  r.tick();
  CHECK_EQ(r.hw.laser.exchanges, 2);

  r.hw.laser.present = true;  // fitted while running
  r.hw.laser.mm = 800;
  r.tick(scout::kLaserRetryMs + 400);
  CHECK_EQ(r.laser.cm(), 80);
}

TEST(laser_other_chip_reads_minus_three_and_gets_no_settings) {
  LaserRig r;
  r.hw.laser.modelId = 0xEEAA;
  r.tick(100);
  CHECK_EQ(r.laser.cm(), scout::kLaserOtherChip);
  CHECK(r.hw.laser.regs.empty());
}

TEST(laser_waits_for_the_sensor_to_boot) {
  LaserRig r;
  r.hw.laser.booted = false;
  r.tick(50);
  CHECK(r.hw.laser.regs.empty());
  CHECK_EQ(r.laser.cm(), -1);
  r.hw.laser.booted = true;
  r.start();
  CHECK_EQ(r.hw.laser.starts, 2);
}

TEST(laser_starts_up_the_way_st_driver_does) {
  LaserRig r;
  r.hw.laser.oscCalibrate = 0xFD53;  // only the low 10 bits count: 0x153
  r.start();
  CHECK_EQ(r.hw.laser.starts, 2);  // once to calibrate, then continuously
  CHECK(r.hw.laser.measuring);
  CHECK(r.hw.laser.longestWrite <= 16u);
  // A few of ST's start-up settings: I2C pulled up to AVDD, interrupt on each new
  // measurement, long distance mode, the sequence config.
  CHECK_EQ(r.reg(0x002E), 0x01);
  CHECK_EQ(r.reg(0x0046), 0x20);
  CHECK_EQ(r.reg(0x004B), 0x0A);
  CHECK_EQ(r.reg(0x0060), 0x0F);
  CHECK_EQ(r.reg(0x0081), 0x9B);
  // After the calibration measurement: start the VHV from the last temperature.
  CHECK_EQ(r.reg(0x0008), 0x09);
  CHECK_EQ(r.reg(0x000B), 0x00);
  // 100 ms timing budget in long distance mode.
  CHECK_EQ(r.reg16(0x005E), 0x01CCL);
  CHECK_EQ(r.reg16(0x0061), 0x01EAL);
  // A measurement every 100 ms, in the sensor's clock ticks.
  CHECK_EQ(r.reg32(0x006C), 0x153L * 100 * 1075 / 1000);
}

TEST(laser_reports_whole_cm_and_holds_through_a_bad_measurement) {
  LaserRig r;
  r.hw.laser.mm = 1234;
  r.start();
  CHECK_EQ(r.laser.cm(), -1);  // nothing measured yet
  r.tick(150);
  CHECK_EQ(r.laser.cm(), 123);
  r.hw.laser.mm = 1235;  // rounds up
  r.tick(100);
  CHECK_EQ(r.laser.cm(), 124);

  r.hw.laser.status = 4;  // "signal fail": nothing in range
  r.tick(100);
  CHECK_EQ(r.laser.cm(), 124);  // one bad measurement is held over
  r.tick(scout::kLaserHoldMs);
  CHECK_EQ(r.laser.cm(), -1);

  r.hw.laser.status = 0xE9;  // the upper bits aren't part of the status
  r.hw.laser.mm = 40;
  r.tick(150);
  CHECK_EQ(r.laser.cm(), 4);
}

TEST(laser_leaves_the_bus_mostly_quiet) {
  LaserRig r;
  r.start();
  r.tick(200);
  const int before = r.hw.laser.exchanges;
  r.tick(1000);
  // Ten measurements, each read with three exchanges after a few polls.
  CHECK(r.hw.laser.exchanges - before <= 100);
}

TEST(laser_rides_out_a_glitch_and_starts_over_after_three) {
  LaserRig r;
  r.hw.laser.mm = 700;
  r.start();
  r.tick(150);
  CHECK_EQ(r.laser.cm(), 70);

  r.hw.laser.failNext = 2;
  r.tick(200);
  CHECK_EQ(r.hw.laser.starts, 2);  // not started again
  CHECK_EQ(r.laser.cm(), 70);

  r.hw.laser.failNext = 3;
  r.tick(30);
  CHECK_EQ(r.laser.cm(), scout::kLaserMissing);
  r.tick(scout::kLaserRetryMs + 400);
  CHECK_EQ(r.hw.laser.starts, 4);  // set up from scratch
  CHECK_EQ(r.laser.cm(), 70);
}

TEST(laser_starts_over_when_measurements_stop_coming) {
  LaserRig r;
  r.hw.laser.mm = 700;
  r.start();
  r.tick(150);
  CHECK_EQ(r.laser.cm(), 70);

  r.hw.laser.periodMs = 1000000;  // stuck
  r.tick(scout::kLaserStallMs + 100);
  CHECK_EQ(r.laser.cm(), -1);
  CHECK_EQ(r.hw.laser.starts, 3);  // set up again

  r.hw.laser.periodMs = 100;
  r.tick(300);
  CHECK_EQ(r.laser.cm(), 70);
}

TEST(laser_stuck_for_good_counts_as_missing) {
  LaserRig r;
  r.hw.laser.periodMs = 1000000;  // never measures
  r.tick(3 * (scout::kLaserStallMs + 50));
  CHECK_EQ(r.laser.cm(), scout::kLaserMissing);
}
