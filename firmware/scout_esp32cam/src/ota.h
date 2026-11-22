// Firmware updates over HTTPS (the car-link `ota` request).
//
// A background task downloads the image (following redirects), writes it straight into
// the spare app slot while hashing it, and only switches the boot slot over once the
// size, the SHA-256 and the image's own checksum all match. The main loop turns the
// task's progress into `ota` messages and restarts the car.
//
// With the bootloader's rollback support, a new image boots as "pending": it must reach
// the Scout server (markAppGood) or the next reset goes back to the previous image.

#ifndef SCOUT_OTA_H
#define SCOUT_OTA_H

#include <Arduino.h>

namespace scout {

struct OtaProgress {
  enum State : uint8_t { kDownloading, kVerifying, kReady, kFailed };
  State state;
  int pct;
  char msg[64];
};

class Ota {
 public:
  bool begin();  // creates the progress queue

  // Starts an update. False (with `why`) if the request can't be started.
  bool start(const char* url, const uint8_t sha256[32], uint32_t size, char* why, size_t whyCap);
  bool running() const { return running_; }

  // The task's next progress report, oldest first.
  bool takeProgress(OtaProgress* out);

  // Rollback support. pendingVerify() is true while a freshly installed image hasn't
  // been confirmed yet.
  static bool pendingVerify();
  static void markAppGood();
  static void rollBack();  // marks this image bad and restarts into the previous one

 private:
  static void taskEntry(void* self);
  void run();
  void report(OtaProgress::State state, int pct, const char* msg);

  QueueHandle_t queue_ = nullptr;
  volatile bool running_ = false;
  char url_[400] = "";
  uint8_t sha_[32] = {0};
  uint32_t size_ = 0;
};

}  // namespace scout

#endif  // SCOUT_OTA_H
