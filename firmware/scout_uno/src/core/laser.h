// VL53L1X laser distance sensor (time of flight) on I2C, read without blocking. Once
// started, the sensor measures on its own every kLaserPeriodMs; update() asks whether a
// measurement is ready and collects it. A missing or failing sensor is looked for again
// every few seconds, so the car runs the same without one.
//
// On the AD176 the sensor sits on the camera's tilt panel, so it measures the distance to
// whatever is in the middle of the picture.

#ifndef SCOUT_UNO_LASER_H
#define SCOUT_UNO_LASER_H

#include <stdint.h>

namespace scout {

// What cm() gives besides a distance (the same codes go out in TEL, protocol/uart.md).
const int16_t kLaserNoReading = -1;  // nothing in range, or nothing measured yet
const int16_t kLaserMissing = -2;    // no sensor answers
const int16_t kLaserOtherChip = -3;  // a chip answers at the address, but it isn't a VL53L1X

class Laser {
 public:
  Laser();

  // Call every loop. Most calls do nothing; the rest do one short I2C exchange (a poll
  // every kLaserPollMs), one chunk of the start-up settings, or the handful of writes
  // that start continuous measuring.
  void update(uint32_t now);

  // Distance in cm, or one of the codes above.
  int16_t cm() const { return cm_; }

 private:
  enum State : uint8_t {
    kFind,       // look for the sensor and check it's a VL53L1X
    kConfigure,  // write the start-up settings, a chunk per call
    kCalibrate,  // first measurement after the settings: the sensor calibrates itself
    kRanging,    // measuring continuously
  };

  void find(uint32_t now);
  void configure(uint32_t now);
  void calibrate(uint32_t now);
  void range(uint32_t now);
  bool startRanging();
  bool dataReady(bool* ready);
  void failed(uint32_t now);
  void notFound(uint32_t now, int16_t code);

  int16_t cm_;
  State state_;
  uint8_t chunk_;     // next chunk of the start-up settings
  uint8_t failures_;  // failed exchanges in a row
  uint32_t nextMs_;   // nothing to do before this
  uint32_t sinceMs_;  // ranging started, or the last measurement came in
  uint32_t lastValidMs_;
};

}  // namespace scout

#endif  // SCOUT_UNO_LASER_H
