// The camera: JPEG frames from the sensor on the AI-Thinker pins.
//
// With PSRAM there are two frame buffers in PSRAM, refilled continuously so the newest
// frame is always ready (CAMERA_GRAB_LATEST), sized for the sensor's largest picture.
// Without PSRAM there is one small buffer in internal RAM and pictures stop at VGA.

#ifndef SCOUT_CAMERA_H
#define SCOUT_CAMERA_H

#include <Arduino.h>
#include <esp_camera.h>

#include "core/camera_info.h"

namespace scout {

class Camera {
 public:
  bool begin(bool vflip, bool hmirror);
  bool ok() const { return ok_; }

  const char* model() const { return model_; }       // "OV3660", ... or nullptr
  const Resolution* maxRes() const { return maxRes_; }  // largest picture allowed
  bool supports(const Resolution* res) const { return ok_ && resolutionAtMost(res, maxRes_); }
  bool psram() const { return psram_; }

  // A JPEG frame at exactly this size and quality, or nullptr (see error()). `fresh`
  // drops a frame first so the picture is taken after the call. Give the frame back
  // with release() as soon as it has been sent.
  camera_fb_t* grab(const Resolution* res, int quality, bool fresh);
  void release(camera_fb_t* frame);

  void setOrientation(bool vflip, bool hmirror);
  const char* error() const { return error_; }

 private:
  bool init();
  camera_fb_t* tryGrab(const Resolution* res, int quality, bool fresh);
  void setError(const char* format, int code);

  bool ok_ = false;
  bool psram_ = false;
  bool vflip_ = true;
  bool hmirror_ = false;
  const char* model_ = nullptr;
  const Resolution* maxRes_ = nullptr;
  int framesize_ = -1;
  int quality_ = -1;
  char error_[64] = "";
};

}  // namespace scout

#endif  // SCOUT_CAMERA_H
