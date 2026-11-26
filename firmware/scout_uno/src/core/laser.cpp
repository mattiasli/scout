#include "laser.h"

#include "config.h"
#include "hardware.h"

#if defined(__AVR__)
#include <avr/pgmspace.h>
#define SCOUT_IN_FLASH PROGMEM
#else
#define SCOUT_IN_FLASH
#endif

namespace scout {

namespace {

// Registers. Addresses are 16 bits; values wider than a byte are big-endian.
const uint16_t kRegVhvLoopBound = 0x0008;  // VHV_CONFIG__TIMEOUT_MACROP_LOOP_BOUND
const uint16_t kRegVhvInit = 0x000B;       // VHV_CONFIG__INIT
const uint16_t kRegGpioStatus = 0x0031;    // GPIO__TIO_HV_STATUS: bit 0 = measurement ready
const uint16_t kRegTimeoutA = 0x005E;      // RANGE_CONFIG__TIMEOUT_MACROP_A, 16 bits
const uint16_t kRegTimeoutB = 0x0061;      // RANGE_CONFIG__TIMEOUT_MACROP_B, 16 bits
const uint16_t kRegPeriod = 0x006C;        // SYSTEM__INTERMEASUREMENT_PERIOD, 32 bits
const uint16_t kRegInterruptClear = 0x0086;
const uint16_t kRegModeStart = 0x0087;
const uint16_t kRegRangeStatus = 0x0089;   // RESULT__RANGE_STATUS, low 5 bits
const uint16_t kRegRangeMm = 0x0096;       // RESULT__FINAL_CROSSTALK_CORRECTED_RANGE_MM_SD0
const uint16_t kRegOscCalibrate = 0x00DE;  // RESULT__OSC_CALIBRATE_VAL, low 10 bits
const uint16_t kRegBootState = 0x00E5;     // FIRMWARE__SYSTEM_STATUS: bit 0 = booted
const uint16_t kRegModelId = 0x010F;       // model ID 0xEA, then module type 0xCC

const uint16_t kModelId = 0xEACC;
const uint8_t kModeContinuous = 0x40;
const uint8_t kModeStop = 0x00;
const uint8_t kRangeValid = 9;  // raw range status of a good measurement (status 0 in ST's API)

// Timing budget of 100 ms in long distance mode: the values ST's driver writes for it.
const uint16_t kTimeoutA = 0x01CC;
const uint16_t kTimeoutB = 0x01EA;
static_assert(kLaserPeriodMs >= 100, "the time between measurements can't be shorter than the budget");

// The start-up settings for registers 0x2D to 0x87 are VL51L1X_DEFAULT_CONFIGURATION from
// ST's VL53L1X ultra lite driver (STSW-IMG009): long distance mode, the interrupt raised
// on each new measurement, active high, I2C and GPIO pulled up to AVDD (2.8 V on the
// sensor board), and ranging stopped. The values are used under ST's licence:
//
//   COPYRIGHT(c) 2018 STMicroelectronics
//
//   Redistribution and use in source and binary forms, with or without modification,
//   are permitted provided that the following conditions are met:
//     1. Redistributions of source code must retain the above copyright notice,
//        this list of conditions and the following disclaimer.
//     2. Redistributions in binary form must reproduce the above copyright notice,
//        this list of conditions and the following disclaimer in the documentation
//        and/or other materials provided with the distribution.
//     3. Neither the name of STMicroelectronics nor the names of its contributors
//        may be used to endorse or promote products derived from this software
//        without specific prior written permission.
//
//   THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
//   AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
//   IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
//   DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
//   FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
//   DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
//   SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
//   CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
//   OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
//   OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
const uint16_t kConfigFirst = 0x002D;
const uint8_t kConfig[] SCOUT_IN_FLASH = {
    0x00, 0x01, 0x01, 0x01, 0x02, 0x00, 0x02, 0x08,  // 0x2D
    0x00, 0x08, 0x10, 0x01, 0x01, 0x00, 0x00, 0x00,  // 0x35
    0x00, 0xFF, 0x00, 0x0F, 0x00, 0x00, 0x00, 0x00,  // 0x3D
    0x00, 0x20, 0x0B, 0x00, 0x00, 0x02, 0x0A, 0x21,  // 0x45
    0x00, 0x00, 0x05, 0x00, 0x00, 0x00, 0x00, 0xC8,  // 0x4D
    0x00, 0x00, 0x38, 0xFF, 0x01, 0x00, 0x08, 0x00,  // 0x55
    0x00, 0x01, 0xDB, 0x0F, 0x01, 0xF1, 0x0D, 0x01,  // 0x5D
    0x68, 0x00, 0x80, 0x08, 0xB8, 0x00, 0x00, 0x00,  // 0x65
    0x00, 0x0F, 0x89, 0x00, 0x00, 0x00, 0x00, 0x00,  // 0x6D
    0x00, 0x00, 0x01, 0x0F, 0x0D, 0x0E, 0x0E, 0x00,  // 0x75
    0x00, 0x02, 0xC7, 0xFF, 0x9B, 0x00, 0x00, 0x00,  // 0x7D
    0x01, 0x00, 0x00,                                // 0x85
};
static_assert(sizeof kConfig == 0x87 - 0x2D + 1, "one byte for each register from 0x2D to 0x87");

// Bytes per write. The AVR Wire buffer holds 32, two of them the register address.
const uint8_t kConfigChunk = 16;

uint8_t configByte(uint8_t index) {
#if defined(__AVR__)
  return pgm_read_byte(&kConfig[index]);
#else
  return kConfig[index];
#endif
}

uint16_t be16(const uint8_t* bytes) {
  return static_cast<uint16_t>((static_cast<uint16_t>(bytes[0]) << 8) | bytes[1]);
}

bool writeByte(uint16_t reg, uint8_t value) { return hw::laserWrite(reg, &value, 1); }

bool writeWord(uint16_t reg, uint16_t value) {
  const uint8_t bytes[2] = {static_cast<uint8_t>(value >> 8), static_cast<uint8_t>(value)};
  return hw::laserWrite(reg, bytes, 2);
}

bool writeLong(uint16_t reg, uint32_t value) {
  const uint8_t bytes[4] = {static_cast<uint8_t>(value >> 24), static_cast<uint8_t>(value >> 16),
                            static_cast<uint8_t>(value >> 8), static_cast<uint8_t>(value)};
  return hw::laserWrite(reg, bytes, 4);
}

// SYSTEM__INTERMEASUREMENT_PERIOD counts in the sensor's own clock ticks, so it is
// scaled by the clock's calibration value, the way ST's driver does it.
uint32_t periodTicks(const uint8_t* oscCalibrate) {
  const uint32_t pll = be16(oscCalibrate) & 0x3FF;
  return pll * kLaserPeriodMs * 1075UL / 1000UL;
}

}  // namespace

Laser::Laser()
    : cm_(kLaserNoReading),
      state_(kFind),
      chunk_(0),
      failures_(0),
      nextMs_(0),
      sinceMs_(0),
      lastValidMs_(0) {}

void Laser::update(uint32_t now) {
  // Don't hold on to an old distance when only failed measurements have come since.
  if (cm_ >= 0 && now - lastValidMs_ >= kLaserHoldMs) cm_ = kLaserNoReading;

  if (static_cast<int32_t>(now - nextMs_) < 0) return;
  switch (state_) {
    case kFind:
      find(now);
      break;
    case kConfigure:
      configure(now);
      break;
    case kCalibrate:
      calibrate(now);
      break;
    case kRanging:
      range(now);
      break;
  }
}

void Laser::find(uint32_t now) {
  // The model ID first: on another chip, reading it changes nothing that matters to us.
  uint8_t id[2];
  if (!hw::laserRead(kRegModelId, id, 2)) {
    notFound(now, kLaserMissing);
    return;
  }
  if (be16(id) != kModelId) {
    notFound(now, kLaserOtherChip);
    return;
  }
  uint8_t boot;
  if (!hw::laserRead(kRegBootState, &boot, 1)) {
    failed(now);
    return;
  }
  if ((boot & 1) == 0) {  // still booting (it takes about 1.2 ms after power-up)
    nextMs_ = now + kLaserPollMs;
    return;
  }
  if (cm_ < kLaserNoReading) cm_ = kLaserNoReading;
  chunk_ = 0;
  state_ = kConfigure;
}

void Laser::configure(uint32_t now) {
  const uint8_t first = static_cast<uint8_t>(chunk_ * kConfigChunk);
  if (first < sizeof kConfig) {
    uint8_t count = static_cast<uint8_t>(sizeof kConfig - first);
    if (count > kConfigChunk) count = kConfigChunk;
    uint8_t bytes[kConfigChunk];
    for (uint8_t i = 0; i < count; ++i) bytes[i] = configByte(static_cast<uint8_t>(first + i));
    if (!hw::laserWrite(static_cast<uint16_t>(kConfigFirst + first), bytes, count)) {
      failed(now);
      return;
    }
    ++chunk_;
    return;
  }
  // One measurement with these settings: the first one after power-up calibrates the
  // sensor for the current temperature (VHV).
  if (!startRanging()) {
    failed(now);
    return;
  }
  state_ = kCalibrate;
  sinceMs_ = now;
  nextMs_ = now + kLaserPollMs;
}

void Laser::calibrate(uint32_t now) {
  bool ready;
  if (!dataReady(&ready)) {
    failed(now);
    return;
  }
  if (!ready) {
    if (now - sinceMs_ >= kLaserStallMs) {
      state_ = kFind;  // it never measured: start over
      failed(now);
    } else {
      nextMs_ = now + kLaserPollMs;
    }
    return;
  }
  // Then, as ST's driver does: stop, and from now on start the temperature calibration
  // from where the last one ended. Set the timing budget and the time between
  // measurements, and measure continuously.
  uint8_t osc[2];
  const bool ok = writeByte(kRegInterruptClear, 1) && writeByte(kRegModeStart, kModeStop) &&
                  writeByte(kRegVhvLoopBound, 0x09) && writeByte(kRegVhvInit, 0x00) &&
                  writeWord(kRegTimeoutA, kTimeoutA) && writeWord(kRegTimeoutB, kTimeoutB) &&
                  hw::laserRead(kRegOscCalibrate, osc, 2) &&
                  writeLong(kRegPeriod, periodTicks(osc)) && startRanging();
  if (!ok) {
    state_ = kFind;  // halfway through: start over
    failed(now);
    return;
  }
  failures_ = 0;
  state_ = kRanging;
  sinceMs_ = now;
  nextMs_ = now + kLaserPollMs;
}

void Laser::range(uint32_t now) {
  bool ready;
  if (!dataReady(&ready)) {
    failed(now);
    return;
  }
  if (!ready) {
    if (now - sinceMs_ >= kLaserStallMs) {
      state_ = kFind;  // measurements stopped coming: start over
      failed(now);
    } else {
      nextMs_ = now + kLaserPollMs;
    }
    return;
  }
  uint8_t status;
  uint8_t mm[2];
  if (!hw::laserRead(kRegRangeStatus, &status, 1) || !hw::laserRead(kRegRangeMm, mm, 2) ||
      !writeByte(kRegInterruptClear, 1)) {
    failed(now);
    return;
  }
  failures_ = 0;
  sinceMs_ = now;
  // The next measurement takes a while: start asking again halfway there.
  nextMs_ = now + kLaserPeriodMs / 2;
  // Any other status (nothing in range, too little light back, too noisy, ...) is no reading.
  if ((status & 0x1F) != kRangeValid) return;
  cm_ = static_cast<int16_t>((be16(mm) + 5) / 10);
  lastValidMs_ = now;
}

bool Laser::startRanging() {
  return writeByte(kRegInterruptClear, 1) && writeByte(kRegModeStart, kModeContinuous);
}

bool Laser::dataReady(bool* ready) {
  uint8_t status;
  if (!hw::laserRead(kRegGpioStatus, &status, 1)) return false;
  *ready = (status & 1) != 0;  // active high, as the start-up settings ask
  return true;
}

// A failed exchange can be a glitch on the bus. A failed poll is simply repeated; a
// failure while starting up starts over. Too many in a row and the sensor counts as gone.
void Laser::failed(uint32_t now) {
  if (++failures_ >= kLaserMaxFailures) {
    notFound(now, kLaserMissing);
    return;
  }
  if (state_ != kRanging) state_ = kFind;
  nextMs_ = now + kLaserPollMs;
}

void Laser::notFound(uint32_t now, int16_t code) {
  cm_ = code;
  state_ = kFind;
  failures_ = 0;
  nextMs_ = now + kLaserRetryMs;
}

}  // namespace scout
