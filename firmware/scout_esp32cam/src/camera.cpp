#include "camera.h"

#include "config.h"

namespace scout {

// core/camera_info.cpp keeps its own copy of these framesize_t values (it can't include
// the driver headers); make sure they still agree.
static_assert(FRAMESIZE_QVGA == 6 && FRAMESIZE_VGA == 10 && FRAMESIZE_SVGA == 11 && FRAMESIZE_XGA == 12 &&
                  FRAMESIZE_HD == 13 && FRAMESIZE_SXGA == 14 && FRAMESIZE_UXGA == 15 && FRAMESIZE_QXGA == 19,
              "framesize_t changed; update core/camera_info.cpp");
static_assert(OV2640_PID == 0x26 && OV3660_PID == 0x3660 && OV5640_PID == 0x5640,
              "camera_pid_t changed; update core/camera_info.cpp");

namespace {

camera_config_t baseConfig() {
  camera_config_t c = {};
  c.pin_pwdn = cfg::kCamPinPwdn;
  c.pin_reset = cfg::kCamPinReset;
  c.pin_xclk = cfg::kCamPinXclk;
  c.pin_sccb_sda = cfg::kCamPinSiod;
  c.pin_sccb_scl = cfg::kCamPinSioc;
  c.pin_d7 = cfg::kCamPinY9;
  c.pin_d6 = cfg::kCamPinY8;
  c.pin_d5 = cfg::kCamPinY7;
  c.pin_d4 = cfg::kCamPinY6;
  c.pin_d3 = cfg::kCamPinY5;
  c.pin_d2 = cfg::kCamPinY4;
  c.pin_d1 = cfg::kCamPinY3;
  c.pin_d0 = cfg::kCamPinY2;
  c.pin_vsync = cfg::kCamPinVsync;
  c.pin_href = cfg::kCamPinHref;
  c.pin_pclk = cfg::kCamPinPclk;
  c.xclk_freq_hz = cfg::kCamXclkHz;
  c.ledc_timer = LEDC_TIMER_0;
  c.ledc_channel = LEDC_CHANNEL_0;
  c.pixel_format = PIXFORMAT_JPEG;
  c.jpeg_quality = cfg::kDefaultJpegQuality;
  c.sccb_i2c_port = 1;
  return c;
}

}  // namespace

void Camera::setError(const char* format, int code) { snprintf(error_, sizeof error_, format, code); }

bool Camera::begin(bool vflip, bool hmirror) {
  vflip_ = vflip;
  hmirror_ = hmirror;
  psram_ = psramFound();
  return init();
}

bool Camera::init() {
  ok_ = false;
  framesize_ = -1;
  quality_ = -1;
  camera_config_t c = baseConfig();
  if (psram_) {
    // The largest size any supported sensor does; the driver lowers it to the sensor's
    // own maximum, and the buffers are sized from it.
    c.frame_size = FRAMESIZE_QXGA;
    c.fb_location = CAMERA_FB_IN_PSRAM;
    c.fb_count = 2;
    c.grab_mode = CAMERA_GRAB_LATEST;
    c.jpeg_buffer_size = 2048u * 1536u / cfg::kJpegBufferDivisor;
  } else {
    c.frame_size = FRAMESIZE_VGA;
    c.fb_location = CAMERA_FB_IN_DRAM;
    c.fb_count = 1;
    c.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  }
  esp_err_t err = esp_camera_init(&c);
  if (err == ESP_ERR_NO_MEM && psram_) {
    c.jpeg_buffer_size = 0;  // let the driver pick its smaller default and try again
    err = esp_camera_init(&c);
  }
  if (err != ESP_OK) {
    setError("camera init failed (0x%x)", err);
    return false;
  }
  sensor_t* s = esp_camera_sensor_get();
  if (s == nullptr) {
    setError("no camera sensor (%d)", 0);
    esp_camera_deinit();
    return false;
  }

  model_ = cameraModelName(s->id.PID);
  camera_sensor_info_t* info = esp_camera_sensor_get_info(&s->id);
  if (model_ == nullptr && info != nullptr) model_ = info->name;
  uint8_t maxFramesize = info != nullptr ? static_cast<uint8_t>(info->max_size) : static_cast<uint8_t>(FRAMESIZE_UXGA);
  maxRes_ = largestResolution(maxFramesize);
  if (!psram_) {
    const Resolution* cap = findResolution(cfg::kNoPsramMaxRes);
    if (!resolutionAtMost(maxRes_, cap)) maxRes_ = cap;
  }
  if (maxRes_ == nullptr) {
    setError("unsupported camera sensor (pid 0x%x)", s->id.PID);
    esp_camera_deinit();
    return false;
  }
  s->set_vflip(s, vflip_ ? 1 : 0);
  s->set_hmirror(s, hmirror_ ? 1 : 0);
  ok_ = true;
  error_[0] = '\0';
  return true;
}

void Camera::setOrientation(bool vflip, bool hmirror) {
  vflip_ = vflip;
  hmirror_ = hmirror;
  if (!ok_) return;
  sensor_t* s = esp_camera_sensor_get();
  if (s == nullptr) return;
  s->set_vflip(s, vflip ? 1 : 0);
  s->set_hmirror(s, hmirror ? 1 : 0);
}

camera_fb_t* Camera::tryGrab(const Resolution* res, int quality, bool fresh) {
  sensor_t* s = esp_camera_sensor_get();
  if (s == nullptr) {
    setError("camera sensor gone (%d)", 0);
    return nullptr;
  }
  bool changed = false;
  if (framesize_ != res->framesize) {
    if (s->set_framesize(s, static_cast<framesize_t>(res->framesize)) != 0) {
      setError("could not set picture size (%d)", res->framesize);
      return nullptr;
    }
    framesize_ = res->framesize;
    changed = true;
  }
  if (quality_ != quality) {
    s->set_quality(s, quality);
    quality_ = quality;
    changed = true;
  }
  int discard = changed ? cfg::kDiscardAfterChange : (fresh ? cfg::kDiscardBeforeSnap : 0);
  for (int i = 0; i < discard; ++i) {
    camera_fb_t* stale = esp_camera_fb_get();
    if (stale != nullptr) esp_camera_fb_return(stale);
  }
  for (int attempt = 0; attempt < cfg::kGrabAttempts; ++attempt) {
    camera_fb_t* fb = esp_camera_fb_get();
    if (fb == nullptr) continue;
    if (fb->format == PIXFORMAT_JPEG && fb->width == res->width && fb->height == res->height && fb->len > 0) {
      return fb;
    }
    esp_camera_fb_return(fb);  // a frame from before the size change
  }
  setError("no frame from the camera (%d)", res->framesize);
  return nullptr;
}

camera_fb_t* Camera::grab(const Resolution* res, int quality, bool fresh) {
  if (!ok_) return nullptr;
  if (!supports(res)) {
    setError("picture size not supported (%d)", res != nullptr ? res->framesize : -1);
    return nullptr;
  }
  camera_fb_t* fb = tryGrab(res, quality, fresh);
  if (fb != nullptr) return fb;
  // One recovery attempt: restart the driver.
  char first[sizeof error_];
  memcpy(first, error_, sizeof first);
  esp_camera_deinit();
  if (!init()) return nullptr;
  fb = tryGrab(res, quality, fresh);
  if (fb == nullptr) memcpy(error_, first, sizeof error_);
  return fb;
}

void Camera::release(camera_fb_t* frame) {
  if (frame != nullptr) esp_camera_fb_return(frame);
}

}  // namespace scout
