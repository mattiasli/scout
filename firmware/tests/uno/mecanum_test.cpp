// Mecanum mixing, PWM mapping and the coast threshold (protocol/uart.md, "Mecanum mixing").

#include "mecanum.h"
#include "testing.h"

using scout::MotorOutput;
using scout::mecanumOutput;
using scout::mixWheels;
using scout::percentToPwm;

namespace {

struct Direction {
  const char* name;
  int vx, vy, wz;
  int bits;
};

// The check values from protocol/uart.md (all wheels at equal speed).
const Direction kChecks[] = {
    {"forward", 1, 0, 0, 163},       {"back", -1, 0, 0, 92},
    {"strafe left", 0, -1, 0, 106},  {"strafe right", 0, 1, 0, 149},
    {"forward-left", 1, -1, 0, 34},  {"forward-right", 1, 1, 0, 129},
    {"back-left", -1, -1, 0, 72},    {"back-right", -1, 1, 0, 20},
    {"clockwise", 0, 0, 1, 172},     {"counter-clockwise", 0, 0, -1, 83},
    {"stop", 0, 0, 0, 0},
};

MotorOutput mix(int vx, int vy, int wz, int minPwm = 90,
                const scout::PwmLayout& layout = scout::kPwmShared) {
  return mecanumOutput(static_cast<int8_t>(vx), static_cast<int8_t>(vy), static_cast<int8_t>(wz),
                       static_cast<uint8_t>(minPwm), layout);
}

}  // namespace

TEST(check_values_match_the_spec_at_several_speeds) {
  const int speeds[] = {100, 60, 50, 1};
  for (const Direction& d : kChecks) {
    for (int speed : speeds) {
      MotorOutput out = mix(d.vx * speed, d.vy * speed, d.wz * speed);
      if (out.bits != d.bits) std::printf("  %s at %d%%: got %d\n", d.name, speed, out.bits);
      CHECK_EQ(static_cast<int>(out.bits), d.bits);
    }
  }
}

TEST(stop_is_all_zero) {
  MotorOutput out = mix(0, 0, 0);
  CHECK_EQ(static_cast<int>(out.bits), 0);
  CHECK_EQ(static_cast<int>(out.pwm5), 0);
  CHECK_EQ(static_cast<int>(out.pwm6), 0);
}

TEST(bit_helpers_follow_the_bit_table) {
  // bit 7 M1+, 6 M1-, 5 M2+, 4 M2-, 3 M4-, 2 M3-, 1 M3+, 0 M4+
  CHECK_EQ(static_cast<int>(scout::forwardBit(scout::kFrontLeft)), 0x80);
  CHECK_EQ(static_cast<int>(scout::backBit(scout::kFrontLeft)), 0x40);
  CHECK_EQ(static_cast<int>(scout::forwardBit(scout::kRearLeft)), 0x20);
  CHECK_EQ(static_cast<int>(scout::backBit(scout::kRearLeft)), 0x10);
  CHECK_EQ(static_cast<int>(scout::forwardBit(scout::kFrontRight)), 0x02);
  CHECK_EQ(static_cast<int>(scout::backBit(scout::kFrontRight)), 0x04);
  CHECK_EQ(static_cast<int>(scout::forwardBit(scout::kRearRight)), 0x01);
  CHECK_EQ(static_cast<int>(scout::backBit(scout::kRearRight)), 0x08);
  CHECK_EQ(static_cast<int>(scout::forwardBit(4)), 0);
}

TEST(mixing_formulas) {
  int8_t w[4];
  mixWheels(30, 20, 10, w);  // FL = vx+vy+wz, RL = vx-vy+wz, FR = vx-vy-wz, RR = vx+vy-wz
  CHECK_EQ(static_cast<int>(w[scout::kFrontLeft]), 60);
  CHECK_EQ(static_cast<int>(w[scout::kRearLeft]), 20);
  CHECK_EQ(static_cast<int>(w[scout::kFrontRight]), 0);
  CHECK_EQ(static_cast<int>(w[scout::kRearRight]), 40);
}

TEST(wheels_over_100_are_scaled_together) {
  int8_t w[4];
  mixWheels(100, 100, 0, w);  // 200, 0, 0, 200
  CHECK_EQ(static_cast<int>(w[scout::kFrontLeft]), 100);
  CHECK_EQ(static_cast<int>(w[scout::kRearLeft]), 0);
  CHECK_EQ(static_cast<int>(w[scout::kFrontRight]), 0);
  CHECK_EQ(static_cast<int>(w[scout::kRearRight]), 100);

  mixWheels(100, 50, 0, w);  // 150, 50, 50, 150 -> 100, 33, 33, 100
  CHECK_EQ(static_cast<int>(w[scout::kFrontLeft]), 100);
  CHECK_EQ(static_cast<int>(w[scout::kRearLeft]), 33);
  CHECK_EQ(static_cast<int>(w[scout::kFrontRight]), 33);
  CHECK_EQ(static_cast<int>(w[scout::kRearRight]), 100);

  mixWheels(-100, 100, -100, w);  // -100, -300, -100, 100 -> -33, -100, -33, 33
  CHECK_EQ(static_cast<int>(w[scout::kFrontLeft]), -33);
  CHECK_EQ(static_cast<int>(w[scout::kRearLeft]), -100);
  CHECK_EQ(static_cast<int>(w[scout::kFrontRight]), -33);
  CHECK_EQ(static_cast<int>(w[scout::kRearRight]), 33);

  mixWheels(60, 0, 40, w);  // exactly 100: nothing to scale
  CHECK_EQ(static_cast<int>(w[scout::kFrontLeft]), 100);
  CHECK_EQ(static_cast<int>(w[scout::kFrontRight]), 20);
}

TEST(percent_to_pwm) {
  CHECK_EQ(static_cast<int>(percentToPwm(0, 90)), 0);
  CHECK_EQ(static_cast<int>(percentToPwm(1, 90)), 90);
  CHECK_EQ(static_cast<int>(percentToPwm(100, 90)), 255);
  CHECK_EQ(static_cast<int>(percentToPwm(50, 90)), 172);  // 90 + 49 * 165 / 99 = 171.7
  CHECK_EQ(static_cast<int>(percentToPwm(0, 0)), 0);
  CHECK_EQ(static_cast<int>(percentToPwm(1, 0)), 0);
  CHECK_EQ(static_cast<int>(percentToPwm(100, 0)), 255);
  CHECK_EQ(static_cast<int>(percentToPwm(1, 255)), 255);
  CHECK_EQ(static_cast<int>(percentToPwm(0, 255)), 0);
  int previous = 0;
  for (int p = 1; p <= 100; ++p) {  // monotonic, never below MINPWM
    int pwm = percentToPwm(static_cast<uint8_t>(p), 90);
    CHECK(pwm >= previous);
    CHECK(pwm >= 90);
    previous = pwm;
  }
}

TEST(shared_pwm_uses_the_fastest_wheel) {
  MotorOutput out = mix(60, 0, 0);
  CHECK_EQ(static_cast<int>(out.pwm5), static_cast<int>(percentToPwm(60, 90)));
  CHECK_EQ(static_cast<int>(out.pwm6), static_cast<int>(percentToPwm(60, 90)));

  out = mix(40, 0, 30);  // FL = RL = 70, FR = RR = 10
  CHECK_EQ(static_cast<int>(out.pwm5), static_cast<int>(percentToPwm(70, 90)));
  CHECK_EQ(static_cast<int>(out.pwm6), static_cast<int>(percentToPwm(70, 90)));

  out = mix(60, 0, 0, 120);  // MINPWM shifts the scale
  CHECK_EQ(static_cast<int>(out.pwm5), static_cast<int>(percentToPwm(60, 120)));
}

TEST(slow_wheels_coast_below_a_quarter_of_the_fastest) {
  // FL = RL = 70, FR = RR = 10: 10 < 70 / 4, so the right wheels coast.
  CHECK_EQ(static_cast<int>(mix(40, 0, 30).bits), 0x80 | 0x20);

  // 100, 100, 25, 25 after scaling (160, 160, 40, 40): exactly 25 % still drives.
  CHECK_EQ(static_cast<int>(mix(100, 0, 60).bits), 163);

  // 161, 161, 39, 39 -> 100, 100, 24, 24: just below 25 % coasts.
  CHECK_EQ(static_cast<int>(mix(100, 0, 61).bits), 0x80 | 0x20);

  // Without scaling: 40, 40, 10, 10 -> 10 is exactly a quarter, driven.
  CHECK_EQ(static_cast<int>(mix(25, 0, 15).bits), 163);
  // 80, 80, 18, 18: 18 is below a quarter of 80 (20), so it coasts.
  CHECK_EQ(static_cast<int>(mix(49, 0, 31).bits), 0x80 | 0x20);

  // A wheel turning backwards but slow coasts the same way: FL = RL = 100, FR = RR = -20.
  CHECK_EQ(static_cast<int>(mix(40, 0, 60).bits), 0x80 | 0x20);
  // ... and above the threshold it is driven backwards: 100, 100, -40, -40 -> M3-, M4-.
  CHECK_EQ(static_cast<int>(mix(30, 0, 70).bits), 0x80 | 0x20 | 0x04 | 0x08);
}

TEST(split_layout_gives_each_side_its_own_duty) {
  // FL = RL = 70, FR = RR = 30 (vx 50, wz 20).
  MotorOutput out = mix(50, 0, 20, 90, scout::kPwmLeftRight);
  CHECK_EQ(static_cast<int>(out.bits), 163);
  CHECK_EQ(static_cast<int>(out.pwm5), static_cast<int>(percentToPwm(70, 90)));
  CHECK_EQ(static_cast<int>(out.pwm6), static_cast<int>(percentToPwm(30, 90)));

  out = mix(50, 0, 20, 90, scout::kPwmRightLeft);
  CHECK_EQ(static_cast<int>(out.pwm5), static_cast<int>(percentToPwm(30, 90)));
  CHECK_EQ(static_cast<int>(out.pwm6), static_cast<int>(percentToPwm(70, 90)));

  // In split mode the slow side runs slowly instead of coasting: 70, 70, 10, 10.
  out = mix(40, 0, 30, 90, scout::kPwmLeftRight);
  CHECK_EQ(static_cast<int>(out.bits), 163);
  CHECK_EQ(static_cast<int>(out.pwm6), static_cast<int>(percentToPwm(10, 90)));

  // Within one side the 25 % rule still applies. vx 60, vy 40: FL 100, RL 20, FR 20,
  // RR 100. Each side's slow wheel is below a quarter of its fast one, so RL and FR coast.
  out = mix(60, 40, 0, 90, scout::kPwmLeftRight);
  CHECK_EQ(static_cast<int>(out.bits), 0x80 | 0x01);
}

TEST(a_wheel_on_no_pwm_pin_coasts) {
  const scout::PwmLayout leftOnly = {scout::kWheelsLeft, 0};
  MotorOutput out = mix(50, 0, 0, 90, leftOnly);
  CHECK_EQ(static_cast<int>(out.bits), 0x80 | 0x20);
  CHECK_EQ(static_cast<int>(out.pwm6), 0);
}

TEST(default_build_uses_shared_pwm) {
  CHECK_EQ(static_cast<int>(scout::kPwmLayout.pwm5Wheels), static_cast<int>(scout::kWheelsAll));
  CHECK_EQ(static_cast<int>(scout::kPwmLayout.pwm6Wheels), static_cast<int>(scout::kWheelsAll));
}
