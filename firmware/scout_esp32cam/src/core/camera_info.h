// Picture sizes the car link knows by name, and camera sensor names by product id.
//
// `framesize` is the value of the esp32-camera framesize_t enum for that size. The
// table is ordered from smallest to largest, in pixels and in framesize_t value, so
// "the largest size a sensor can do" is a simple scan. camera.cpp checks these values
// against the real enum at compile time.

#ifndef SCOUT_CORE_CAMERA_INFO_H
#define SCOUT_CORE_CAMERA_INFO_H

#include <stddef.h>
#include <stdint.h>

namespace scout {

struct Resolution {
  const char* name;
  uint16_t width;
  uint16_t height;
  uint8_t framesize;
};

size_t resolutionCount();
const Resolution& resolutionAt(size_t index);

// By car-link name ("qvga", "vga", ...). nullptr if unknown.
const Resolution* findResolution(const char* name);

// The largest named size whose framesize_t value is <= `maxFramesize`. nullptr if even
// the smallest is too big.
const Resolution* largestResolution(uint8_t maxFramesize);

// True if `a` is no larger than `b`.
bool resolutionAtMost(const Resolution* a, const Resolution* b);

// Sensor name for a camera_pid_t value ("OV3660", "OV2640", "OV5640", ...), or nullptr.
const char* cameraModelName(uint16_t pid);

}  // namespace scout

#endif  // SCOUT_CORE_CAMERA_INFO_H
