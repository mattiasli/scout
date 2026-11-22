#include "camera_info.h"

#include <string.h>

namespace scout {

namespace {

// framesize_t values from esp32-camera's sensor.h.
const Resolution kResolutions[] = {
    {"qvga", 320, 240, 6},     // FRAMESIZE_QVGA
    {"vga", 640, 480, 10},     // FRAMESIZE_VGA
    {"svga", 800, 600, 11},    // FRAMESIZE_SVGA
    {"xga", 1024, 768, 12},    // FRAMESIZE_XGA
    {"hd", 1280, 720, 13},     // FRAMESIZE_HD
    {"sxga", 1280, 1024, 14},  // FRAMESIZE_SXGA
    {"uxga", 1600, 1200, 15},  // FRAMESIZE_UXGA
    {"qxga", 2048, 1536, 19},  // FRAMESIZE_QXGA
};

const size_t kResolutionCount = sizeof kResolutions / sizeof kResolutions[0];

struct Model {
  uint16_t pid;
  const char* name;
};

// camera_pid_t values from esp32-camera's sensor.h.
const Model kModels[] = {
    {0x26, "OV2640"},   {0x3660, "OV3660"},   {0x5640, "OV5640"},   {0x3640, "OV3640"}, {0x77, "OV7725"},
    {0x76, "OV7670"},   {0x96, "OV9650"},     {0x1410, "NT99141"},  {0x2145, "GC2145"}, {0x232a, "GC032A"},
    {0x9b, "GC0308"},   {0x30, "BF3005"},     {0x20a6, "BF20A6"},   {0xda4a, "SC101IOT"}, {0x9a46, "SC030IOT"},
    {0x0031, "SC031GS"}, {0x039E, "MEGA_CCM"}, {0x0955, "HM1055"}, {0x0360, "HM0360"},
};

}  // namespace

size_t resolutionCount() { return kResolutionCount; }

const Resolution& resolutionAt(size_t index) {
  return kResolutions[index < kResolutionCount ? index : kResolutionCount - 1];
}

const Resolution* findResolution(const char* name) {
  if (name == nullptr) return nullptr;
  for (size_t i = 0; i < kResolutionCount; ++i) {
    if (strcmp(kResolutions[i].name, name) == 0) return &kResolutions[i];
  }
  return nullptr;
}

const Resolution* largestResolution(uint8_t maxFramesize) {
  const Resolution* best = nullptr;
  for (size_t i = 0; i < kResolutionCount; ++i) {
    if (kResolutions[i].framesize <= maxFramesize) best = &kResolutions[i];
  }
  return best;
}

bool resolutionAtMost(const Resolution* a, const Resolution* b) {
  return a != nullptr && b != nullptr && a->framesize <= b->framesize;
}

const char* cameraModelName(uint16_t pid) {
  for (size_t i = 0; i < sizeof kModels / sizeof kModels[0]; ++i) {
    if (kModels[i].pid == pid) return kModels[i].name;
  }
  return nullptr;
}

}  // namespace scout
