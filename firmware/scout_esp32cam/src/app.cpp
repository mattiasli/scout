#include "app.h"

#include <Arduino.h>
#include <WiFi.h>
#include <cJSON.h>
#include <cmath>
#include <esp_attr.h>
#include <esp_log.h>
#include <esp_psram.h>
#include <esp_random.h>
#include <esp_system.h>
#include <esp_timer.h>

#include "camera.h"
#include "config.h"
#include "core/backoff.h"
#include "core/camera_info.h"
#include "core/json_out.h"
#include "core/request_tracker.h"
#include "core/reset_counter.h"
#include "core/util.h"
#include "core/ws_handshake.h"
#include "leds.h"
#include "link_client.h"
#include "ota.h"
#include "settings.h"
#include "uno_bridge.h"
#include "wifi_setup.h"

namespace scout {

namespace {

// ---------------------------------------------------------------- console silence

// UART0 is the wire to the UNO. Nothing but ScoutLink frames may go out on it, so every
// log path of the core and of ESP-IDF is switched off before anything else runs. (The
// ROM boot messages and a crash dump can't be switched off; the UART framing is built
// to survive them.)
int silentVprintf(const char*, va_list) { return 0; }

void silenceConsole() {
  esp_log_level_set("*", ESP_LOG_NONE);
  esp_log_set_vprintf(&silentVprintf);
  Serial.setDebugOutput(false);  // no ets_printf / log_x output on the UART
  (void)freopen("/dev/null", "w", stdout);
  (void)freopen("/dev/null", "w", stderr);
}

// ---------------------------------------------------------------- reset counter

// The record lives in RTC memory, which survives a software or watchdog reset. On the
// ESP32 the RST button pulls the chip's enable pin, which may clear RTC memory as well,
// so a copy also goes to NVS (a handful of small writes per power-on, none afterwards)
// and is used whenever the RTC copy is gone.
RTC_NOINIT_ATTR ResetRecord g_resetRecord;
ResetCounter g_resetCounter(cfg::kResetsForSetup, cfg::kResetWindowMs);
esp_timer_handle_t g_resetTimer = nullptr;
uint32_t g_resetSavedMs = 0;

bool countResets() {
  if (!ResetCounter::valid(g_resetRecord)) {
    ResetRecord stored;
    if (loadResetRecord(&stored)) g_resetRecord = stored;
  }
  bool countable = resetIsCountable(static_cast<int>(esp_reset_reason()));
  bool trigger = g_resetCounter.onBoot(&g_resetRecord, countable);
  saveResetRecord(g_resetRecord);
  return trigger;
}

void resetTick(void*) {
  uint32_t up = static_cast<uint32_t>(esp_timer_get_time() / 1000);
  g_resetCounter.onUptime(&g_resetRecord, up);
  bool done = !g_resetCounter.watching();
  if (done || up - g_resetSavedMs >= cfg::kResetSaveMs) {
    saveResetRecord(g_resetRecord);
    g_resetSavedMs = up;
  }
  if (done && g_resetTimer != nullptr) esp_timer_stop(g_resetTimer);
}

// Runs on a timer so it keeps counting while the boot blocks on WiFi.
void watchResets() {
  if (!g_resetCounter.watching()) return;
  g_resetSavedMs = static_cast<uint32_t>(esp_timer_get_time() / 1000);
  esp_timer_create_args_t args = {};
  args.callback = &resetTick;
  args.dispatch_method = ESP_TIMER_TASK;
  args.name = "resets";
  if (esp_timer_create(&args, &g_resetTimer) == ESP_OK) {
    esp_timer_start_periodic(g_resetTimer, static_cast<uint64_t>(cfg::kResetUpdateMs) * 1000);
  }
}

// ---------------------------------------------------------------- JSON fields

enum Field { kMissing, kOk, kBad };

Field intField(const cJSON* obj, const char* name, int32_t lo, int32_t hi, int32_t* out) {
  const cJSON* item = cJSON_GetObjectItemCaseSensitive(obj, name);
  if (item == nullptr || cJSON_IsNull(item)) return kMissing;
  if (!cJSON_IsNumber(item)) return kBad;
  return numberToInt(item->valuedouble, lo, hi, out) ? kOk : kBad;
}

Field boolField(const cJSON* obj, const char* name, bool* out) {
  const cJSON* item = cJSON_GetObjectItemCaseSensitive(obj, name);
  if (item == nullptr || cJSON_IsNull(item)) return kMissing;
  if (!cJSON_IsBool(item)) return kBad;
  *out = cJSON_IsTrue(item);
  return kOk;
}

Field strField(const cJSON* obj, const char* name, const char** out) {
  const cJSON* item = cJSON_GetObjectItemCaseSensitive(obj, name);
  if (item == nullptr || cJSON_IsNull(item)) return kMissing;
  if (!cJSON_IsString(item) || item->valuestring == nullptr) return kBad;
  *out = item->valuestring;
  return kOk;
}

// The UNO settings `conf` can carry: car-link name, UART key, allowed range.
struct UnoConfKey {
  const char* json;
  const char* uart;
  int32_t lo;
  int32_t hi;
};
const UnoConfKey kUnoConf[] = {
    {"obst_cm", "OBST", 5, 100},
    {"min_pwm", "MINPWM", 0, 255},
    {"link_ms", "LINKMS", 300, 5000},
};
constexpr size_t kUnoConfCount = sizeof kUnoConf / sizeof kUnoConf[0];

// ---------------------------------------------------------------- the car

class Car {
 public:
  void setup();
  void loop();

 private:
  // boot
  void enterSetup(const char* why);
  void setupLoop();

  // link
  void connectLink(uint32_t now);
  void onLinkDown(uint32_t now);
  static void onTextThunk(void* self, char* text, size_t len) { static_cast<Car*>(self)->onText(text, len); }
  void onText(char* text, size_t len);

  // requests
  void handleWelcome(const cJSON* msg);
  void handleMove(const cJSON* msg, uint32_t id, bool test);
  void handleStop(uint32_t id);
  void handleTilt(const cJSON* msg, uint32_t id);
  void handleLight(const cJSON* msg, uint32_t id);
  void handleSnap(const cJSON* msg, uint32_t id);
  void handleStream(const cJSON* msg, uint32_t id);
  void handleConf(const cJSON* conf, uint32_t id, bool fromWelcome);
  void handleOta(const cJSON* msg, uint32_t id);
  void handleReboot(uint32_t id);
  bool pictureArgs(const cJSON* msg, uint32_t id, const Resolution** res, int32_t* q);
  bool cameraReady(uint32_t id);

  // periodic work
  void pumpUno(uint32_t now);
  void watchUno();
  void watchWifi(uint32_t now);
  void sendTelemetry(uint32_t now);
  void pumpStream(uint32_t now);
  void pumpOta(uint32_t now);
  void watchRollback(uint32_t now);
  void sendUnoConf();
  void restartNow();
  void updateLed();

  // messages
  bool sendJson(JsonOut& j);
  void sendHello();
  void sendAck(uint32_t id, const char* field = nullptr, int32_t value = 0);
  void sendErr(uint32_t id, const char* code, const char* msg);
  void sendEvent(const char* kind, const char* msg);
  void sendOta(const char* state, int pct, const char* msg);
  static void replyThunk(void* self, const Reply& r) { static_cast<Car*>(self)->sendReply(r); }
  void sendReply(const Reply& r);
  ReplySink sink() { return ReplySink{&Car::replyThunk, this}; }

  Settings settings_;
  UnoBridge uno_;
  Camera camera_;
  Light light_;
  StatusLed led_;
  LinkClient link_;
  Ota ota_;
  RequestTracker tracker_;
  Backoff backoff_{cfg::kBackoffBaseMs, cfg::kBackoffCapMs, cfg::kBackoffJitterPercent, cfg::kBackoffStableMs};
  SetupPortal* portal_ = nullptr;
  ws::ServerUrl url_ = {};

  bool cameraStarted_ = false;
  uint32_t nextAttemptMs_ = 0;
  bool welcomed_ = false;
  uint32_t telNextMs_ = 0;
  uint32_t wifiLostMs_ = 0;
  uint32_t wifiRescanMs_ = 0;
  bool unoPresent_ = false;
  bool unoSeen_ = false;
  uint32_t rebootAtMs_ = 0;
  bool rebootPending_ = false;
  bool pendingVerify_ = false;

  // `conf` values, kept so they can be re-sent after the UNO restarts.
  bool unoConfHave_[kUnoConfCount] = {false, false, false};
  int32_t unoConfValue_[kUnoConfCount] = {0, 0, 0};
  bool vflip_ = cfg::kDefaultVflip;
  bool hmirror_ = cfg::kDefaultHmirror;

  struct Stream {
    bool on;
    uint32_t id;
    const Resolution* res;
    int32_t quality;
    uint32_t intervalMs;
    uint32_t untilMs;
    uint32_t nextMs;
  };
  Stream stream_ = {};

  uint32_t otaId_ = 0;
  char out_[cfg::kJsonOutBytes];
};

// ---------------------------------------------------------------- boot

void Car::setup() {
  // The white LED's transistor input floats at reset; hold it off from the start.
  pinMode(cfg::kLightPin, OUTPUT);
  digitalWrite(cfg::kLightPin, LOW);
  silenceConsole();
  Serial.setRxBufferSize(cfg::kUnoRxBufferBytes);
  Serial.begin(cfg::kUnoBaud);
  Serial.setDebugOutput(false);
  silenceConsole();  // again: begin() may have re-attached the log output

  // The UNO keeps its motors off until told otherwise, but say it anyway.
  uno_.begin();
  uno_.sendStop();

  led_.begin();
  led_.setMode(LedMode::kConnecting);

  bool resetSetup = countResets();
  watchResets();

  pendingVerify_ = Ota::pendingVerify();
  settings_.load();
  bool haveUrl = ws::parseServerUrl(settings_.serverUrl(), &url_);

  if (resetSetup) return enterSetup("You pressed reset 3 times, so the car started in setup mode.");
  if (settings_.networks().count() == 0) return enterSetup("No WiFi network is saved yet.");
  if (!haveUrl) return enterSetup("The server address isn't set yet.");
  if (!settings_.hasCarKey()) return enterSetup("The car key isn't set yet.");

  wifiPrepareStation();
  if (!wifiConnectSaved(settings_.networks(), cfg::kWifiConnectBudgetMs, false)) {
    return enterSetup("The car couldn't join any saved WiFi network. Check the password, or add another network.");
  }

  uno_.startPinging();
  // The camera takes LEDC timer 0 for its clock; the light attaches after it.
  camera_.begin(vflip_, hmirror_);
  cameraStarted_ = true;
  light_.begin();
  ota_.begin();
  link_.setHandler(&Car::onTextThunk, this);
  nextAttemptMs_ = millis();
}

void Car::enterSetup(const char* why) {
  uno_.sendStop();
  led_.setMode(LedMode::kSetup);
  portal_ = new SetupPortal();
  portal_->begin(&settings_, why);
}

void Car::setupLoop() {
  watchRollback(millis());
  if (!portal_->loop()) {
    delay(500);  // let the last page reach the phone
    restartNow();
  }
  delay(2);
}

void Car::restartNow() {
  uno_.sendStop();
  light_.set(0);
  if (link_.connected()) link_.close(ws::kCloseGoingAway);
  Serial.flush();
  delay(100);
  ESP.restart();
}

// ---------------------------------------------------------------- main loop

void Car::loop() {
  if (portal_ != nullptr) return setupLoop();
  uint32_t now = millis();
  watchRollback(now);
  pumpUno(now);
  tracker_.tick(now, sink());
  watchUno();
  watchWifi(now);

  if (link_.connected()) {
    link_.poll();
    if (link_.connected() && millis() - link_.lastHeardMs() > cfg::kLinkSilenceMs) {
      link_.close(ws::kCloseGoingAway);  // the server has gone quiet: start over
    }
    if (!link_.connected()) {
      onLinkDown(millis());
    } else {
      sendTelemetry(millis());
      pumpStream(millis());
    }
  } else if (WiFi.isConnected() && static_cast<int32_t>(millis() - nextAttemptMs_) >= 0) {
    connectLink(millis());
  }

  pumpOta(millis());
  if (rebootPending_ && static_cast<int32_t>(millis() - rebootAtMs_) >= 0) restartNow();
  updateLed();
  delay(1);
}

void Car::updateLed() {
  if (ota_.running()) {
    led_.setMode(LedMode::kUpdating);
  } else if (link_.connected() && welcomed_) {
    led_.setMode(LedMode::kConnected);
  } else {
    led_.setMode(LedMode::kConnecting);
  }
}

void Car::watchRollback(uint32_t now) {
  if (!pendingVerify_ || now < cfg::kOtaConfirmDeadlineMs) return;
  // A new image that never reached the server: go back to the one that did.
  uno_.sendStop();
  light_.set(0);
  Ota::rollBack();
}

// ---------------------------------------------------------------- link

void Car::connectLink(uint32_t now) {
  (void)now;
  char why[96] = "";
  if (!link_.connect(url_, settings_.carKey(), cfg::kFirmwareVersion, why, sizeof why)) {
    nextAttemptMs_ = millis() + backoff_.nextDelay(esp_random());
    return;
  }
  backoff_.connected(millis());
  welcomed_ = false;
  sendHello();
  if (cameraStarted_ && !camera_.ok()) sendEvent("camera_error", camera_.error());
  telNextMs_ = millis();
}

void Car::onLinkDown(uint32_t now) {
  // Safety first: nothing keeps running without the server.
  uno_.sendStop();
  light_.set(0);
  stream_.on = false;
  tracker_.clear();
  welcomed_ = false;
  backoff_.disconnected(now);
  nextAttemptMs_ = now + backoff_.nextDelay(esp_random());
}

void Car::onText(char* text, size_t len) {
  cJSON* msg = cJSON_ParseWithLength(text, len);
  if (msg == nullptr || !cJSON_IsObject(msg)) {
    cJSON_Delete(msg);
    sendErr(0, "args", "not a JSON object");
    return;
  }
  uint32_t id = 0;
  const cJSON* idItem = cJSON_GetObjectItemCaseSensitive(msg, "id");
  bool hasId = cJSON_IsNumber(idItem) && numberToId(idItem->valuedouble, &id);
  if (!hasId) id = 0;
  const char* t = nullptr;
  if (strField(msg, "t", &t) != kOk) {
    sendErr(id, "args", "missing \"t\"");
  } else if (strcmp(t, "welcome") == 0) {
    handleWelcome(msg);
  } else if (strcmp(t, "ping") == 0) {
    JsonOut j(out_, sizeof out_);
    j.str("t", "pong");
    if (hasId) {
      j.num("id", id);
    } else {
      j.null("id");
    }
    sendJson(j);
  } else if (!hasId) {
    sendErr(0, "args", "missing or bad \"id\"");
  } else if (strcmp(t, "move") == 0) {
    handleMove(msg, id, false);
  } else if (strcmp(t, "test") == 0) {
    handleMove(msg, id, true);
  } else if (strcmp(t, "stop") == 0) {
    handleStop(id);
  } else if (strcmp(t, "tilt") == 0) {
    handleTilt(msg, id);
  } else if (strcmp(t, "light") == 0) {
    handleLight(msg, id);
  } else if (strcmp(t, "snap") == 0) {
    handleSnap(msg, id);
  } else if (strcmp(t, "stream") == 0) {
    handleStream(msg, id);
  } else if (strcmp(t, "conf") == 0) {
    handleConf(msg, id, false);
  } else if (strcmp(t, "ota") == 0) {
    handleOta(msg, id);
  } else if (strcmp(t, "reboot") == 0) {
    handleReboot(id);
  } else {
    sendErr(id, "unknown_type", t);
  }
  cJSON_Delete(msg);
}

// ---------------------------------------------------------------- requests

void Car::handleWelcome(const cJSON* msg) {
  welcomed_ = true;
  if (pendingVerify_) {
    Ota::markAppGood();  // this image reached the server: keep it
    pendingVerify_ = false;
  }
  const cJSON* conf = cJSON_GetObjectItemCaseSensitive(msg, "conf");
  if (cJSON_IsObject(conf)) handleConf(conf, 0, true);
}

void Car::handleMove(const cJSON* msg, uint32_t id, bool test) {
  if (ota_.running()) return sendErr(id, "busy", "a firmware update is running");
  int32_t args[4] = {0, 0, 0, 0};
  bool ok;
  if (test) {
    ok = intField(msg, "bits", 0, 255, &args[0]) == kOk && intField(msg, "pwm5", 0, 255, &args[1]) == kOk &&
         intField(msg, "pwm6", 0, 255, &args[2]) == kOk && intField(msg, "ms", 1, 2000, &args[3]) == kOk;
    if (!ok) return sendErr(id, "args", "bits, pwm5, pwm6 0..255 and ms 1..2000 are required");
  } else {
    // Axes left out mean 0; ms is required.
    ok = intField(msg, "vx", -100, 100, &args[0]) != kBad && intField(msg, "vy", -100, 100, &args[1]) != kBad &&
         intField(msg, "wz", -100, 100, &args[2]) != kBad && intField(msg, "ms", 1, 5000, &args[3]) == kOk;
    if (!ok) return sendErr(id, "args", "vx, vy, wz -100..100 and ms 1..5000");
  }
  if (!uno_.present()) return sendErr(id, "uno", "the UNO isn't answering");
  if (tracker_.full()) return sendErr(id, "busy", "too many requests waiting for the UNO");
  uint16_t seq = uno_.send(test ? "TEST" : "MOVE", args, 4);
  if (seq == 0) return sendErr(id, "uno", "could not build the UART frame");
  tracker_.add(seq, id, test ? ReqKind::kTest : ReqKind::kMove, millis(),
               static_cast<uint32_t>(args[3]) + cfg::kUnoDoneGraceMs);
  telNextMs_ = millis();  // moving: switch to the fast telemetry rate now
}

void Car::handleStop(uint32_t id) {
  if (!uno_.present()) return sendErr(id, "uno", "the UNO isn't answering");
  if (tracker_.full()) return sendErr(id, "busy", "too many requests waiting for the UNO");
  uint16_t seq = uno_.sendStop();
  if (seq == 0) return sendErr(id, "uno", "could not build the UART frame");
  tracker_.add(seq, id, ReqKind::kStop, millis(), cfg::kUnoAckTimeoutMs);
}

void Car::handleTilt(const cJSON* msg, uint32_t id) {
  const cJSON* item = cJSON_GetObjectItemCaseSensitive(msg, "deg");
  if (!cJSON_IsNumber(item) || !std::isfinite(item->valuedouble)) return sendErr(id, "args", "deg is required");
  double deg = item->valuedouble;
  if (deg > cfg::kTiltLimitDeg) deg = cfg::kTiltLimitDeg;
  if (deg < -cfg::kTiltLimitDeg) deg = -cfg::kTiltLimitDeg;
  int32_t arg = 0;
  numberToInt(deg, -cfg::kTiltLimitDeg, cfg::kTiltLimitDeg, &arg);
  if (!uno_.present()) return sendErr(id, "uno", "the UNO isn't answering");
  if (tracker_.full()) return sendErr(id, "busy", "too many requests waiting for the UNO");
  uint16_t seq = uno_.send("TILT", &arg, 1);
  if (seq == 0) return sendErr(id, "uno", "could not build the UART frame");
  tracker_.add(seq, id, ReqKind::kTilt, millis(), cfg::kUnoAckTimeoutMs);
}

void Car::handleLight(const cJSON* msg, uint32_t id) {
  int32_t level = 0;
  if (intField(msg, "level", 0, 100, &level) != kOk) return sendErr(id, "args", "level 0..100 is required");
  light_.set(level);
  sendAck(id, "level", light_.level());
}

bool Car::pictureArgs(const cJSON* msg, uint32_t id, const Resolution** res, int32_t* q) {
  const char* name = cfg::kDefaultSnapRes;
  if (strField(msg, "res", &name) == kBad) {
    sendErr(id, "args", "res must be a string");
    return false;
  }
  *res = findResolution(name);
  if (*res == nullptr) {
    sendErr(id, "args", "res must be qvga, vga, svga, xga, hd, sxga, uxga or qxga");
    return false;
  }
  *q = cfg::kDefaultJpegQuality;
  if (intField(msg, "q", cfg::kJpegQualityMin, cfg::kJpegQualityMax, q) == kBad) {
    sendErr(id, "args", "q must be 4..63");
    return false;
  }
  return true;
}

bool Car::cameraReady(uint32_t id) {
  if (!camera_.ok()) {
    sendErr(id, "camera", camera_.error()[0] ? camera_.error() : "no camera");
    return false;
  }
  return true;
}

void Car::handleSnap(const cJSON* msg, uint32_t id) {
  if (ota_.running()) return sendErr(id, "busy", "a firmware update is running");
  const Resolution* res = nullptr;
  int32_t q = 0;
  if (!pictureArgs(msg, id, &res, &q) || !cameraReady(id)) return;
  if (!camera_.supports(res)) {
    char why[48];
    snprintf(why, sizeof why, "res above max_res (%s)", camera_.maxRes()->name);
    return sendErr(id, "args", why);
  }
  camera_fb_t* fb = camera_.grab(res, q, true);
  if (fb == nullptr) {
    sendErr(id, "camera", camera_.error());
    sendEvent("camera_error", camera_.error());
    return;
  }
  uint8_t header[8];
  writeImageHeader(header, false, id);
  link_.sendImage(header, fb->buf, fb->len);
  camera_.release(fb);
}

void Car::handleStream(const cJSON* msg, uint32_t id) {
  bool on = false;
  if (boolField(msg, "on", &on) != kOk) return sendErr(id, "args", "on (true/false) is required");
  if (!on) {
    stream_.on = false;
    return sendAck(id);
  }
  if (ota_.running()) return sendErr(id, "busy", "a firmware update is running");
  const Resolution* res = nullptr;
  int32_t q = 0;
  int32_t fps = 0;
  int32_t secs = 0;
  if (!pictureArgs(msg, id, &res, &q)) return;
  if (intField(msg, "fps", cfg::kStreamFpsMin, cfg::kStreamFpsMax, &fps) != kOk ||
      intField(msg, "secs", cfg::kStreamSecsMin, cfg::kStreamSecsMax, &secs) != kOk) {
    return sendErr(id, "args", "fps 1..10 and secs 1..120 are required");
  }
  if (!cameraReady(id)) return;
  if (!camera_.supports(res)) {
    char why[48];
    snprintf(why, sizeof why, "res above max_res (%s)", camera_.maxRes()->name);
    return sendErr(id, "args", why);
  }
  uint32_t now = millis();
  bool wasOn = stream_.on;
  stream_.on = true;
  stream_.id = id;  // a re-sent stream request takes over: frames carry the newest id
  stream_.res = res;
  stream_.quality = q;
  stream_.intervalMs = 1000u / static_cast<uint32_t>(fps);
  stream_.untilMs = now + static_cast<uint32_t>(secs) * 1000u;
  if (!wasOn) stream_.nextMs = now;
  sendAck(id);
}

void Car::handleConf(const cJSON* conf, uint32_t id, bool fromWelcome) {
  // Check everything first: a request with one bad value changes nothing.
  int32_t values[kUnoConfCount] = {0, 0, 0};
  bool present[kUnoConfCount] = {false, false, false};
  bool vflip = vflip_;
  bool hmirror = hmirror_;
  Field fv = boolField(conf, "vflip", &vflip);
  Field fh = boolField(conf, "hmirror", &hmirror);
  bool bad = fv == kBad || fh == kBad;
  for (size_t i = 0; i < kUnoConfCount; ++i) {
    Field f = intField(conf, kUnoConf[i].json, kUnoConf[i].lo, kUnoConf[i].hi, &values[i]);
    bad = bad || f == kBad;
    present[i] = f == kOk;
  }
  if (bad) {
    if (!fromWelcome) sendErr(id, "args", "obst_cm 5..100, min_pwm 0..255, link_ms 300..5000, vflip/hmirror bool");
    return;
  }

  if (fv == kOk || fh == kOk) {
    vflip_ = vflip;
    hmirror_ = hmirror;
    camera_.setOrientation(vflip_, hmirror_);
  }
  size_t unoCount = 0;
  for (size_t i = 0; i < kUnoConfCount; ++i) {
    if (!present[i]) continue;
    unoConfHave_[i] = true;
    unoConfValue_[i] = values[i];
    ++unoCount;
  }

  if (fromWelcome) {
    if (uno_.present()) sendUnoConf();  // otherwise as soon as the UNO answers
    return;
  }
  if (unoCount == 0) return sendAck(id);
  if (!uno_.present()) return sendErr(id, "uno", "the UNO isn't answering; the values are sent when it is back");
  if (tracker_.pending() + unoCount > RequestTracker::kSlots) {
    return sendErr(id, "busy", "too many requests waiting for the UNO");
  }
  uint32_t now = millis();
  for (size_t i = 0; i < kUnoConfCount; ++i) {
    if (!present[i]) continue;
    uint16_t seq = uno_.sendConf(kUnoConf[i].uart, values[i]);
    if (seq != 0) tracker_.add(seq, id, ReqKind::kConf, now, cfg::kUnoAckTimeoutMs);
  }
}

void Car::sendUnoConf() {
  // The car's own requests (id 0): not tracked, their ACKs are simply ignored.
  for (size_t i = 0; i < kUnoConfCount; ++i) {
    if (unoConfHave_[i]) uno_.sendConf(kUnoConf[i].uart, unoConfValue_[i]);
  }
}

void Car::handleOta(const cJSON* msg, uint32_t id) {
  if (ota_.running()) return sendErr(id, "busy", "an update is already running");
  if (tracker_.moving()) return sendErr(id, "busy", "the car is moving");
  const char* url = nullptr;
  const char* hash = nullptr;
  uint8_t sha[32];
  const cJSON* sizeItem = cJSON_GetObjectItemCaseSensitive(msg, "size");
  uint32_t size = 0;
  if (strField(msg, "url", &url) != kOk || strField(msg, "sha256", &hash) != kOk || !parseSha256Hex(hash, sha) ||
      !cJSON_IsNumber(sizeItem) || !numberToId(sizeItem->valuedouble, &size)) {
    return sendErr(id, "args", "url, sha256 (64 hex digits) and size are required");
  }
  char why[64];
  if (!ota_.start(url, sha, size, why, sizeof why)) return sendErr(id, "ota", why);
  otaId_ = id;
  stream_.on = false;  // the download needs the bandwidth and the memory
}

void Car::handleReboot(uint32_t id) {
  if (ota_.running()) return sendErr(id, "busy", "a firmware update is running");
  sendAck(id);
  rebootPending_ = true;
  rebootAtMs_ = millis() + cfg::kRebootDelayMs;
}

// ---------------------------------------------------------------- periodic work

void Car::pumpUno(uint32_t now) {
  (void)now;
  UnoFrame f;
  ReplySink out = sink();
  while (uno_.takeFrame(&f)) {
    if (f.is("ACK")) {
      int32_t value = 0;
      bool hasValue = f.argInt(0, &value);
      tracker_.onAck(f.seq(), hasValue, value, out);
    } else if (f.is("ERR")) {
      tracker_.onErr(f.seq(), f.arg(0), out);
    } else if (f.is("DONE")) {
      UnoDone done;
      if (parseDone(f, &done)) tracker_.onDone(f.seq(), done.reason, done.cmStart, done.cmEnd, done.ms, out);
    } else if (f.is("BOOT")) {
      tracker_.onUnoBoot(out);
      char msg[48];
      snprintf(msg, sizeof msg, "UNO restarted (fw %s, reset flags %s)", f.arg(0), f.arg(1));
      sendEvent("uno_boot", msg);
      sendUnoConf();  // the UNO forgets its settings on every boot
    }
  }
}

void Car::watchUno() {
  bool present = uno_.present();
  if (present == unoPresent_) return;
  unoPresent_ = present;
  if (!present) {
    sendEvent("uno_lost", "the UNO stopped answering");
    return;
  }
  if (unoSeen_) sendEvent("uno_back", "the UNO is answering again");
  unoSeen_ = true;
  sendUnoConf();
}

void Car::watchWifi(uint32_t now) {
  if (WiFi.isConnected()) {
    wifiLostMs_ = 0;
    return;
  }
  if (link_.connected()) {
    link_.drop();
    onLinkDown(now);
  }
  if (wifiLostMs_ == 0) {
    wifiLostMs_ = now;
    wifiRescanMs_ = now;
    return;
  }
  // The WiFi driver keeps retrying the same network by itself. After a while, try every
  // saved network again (the car may have been carried somewhere else).
  if (now - wifiRescanMs_ >= cfg::kWifiLostRescanMs) {
    wifiConnectSaved(settings_.networks(), cfg::kWifiConnectBudgetMs, false);
    wifiRescanMs_ = millis();
  }
}

void Car::sendTelemetry(uint32_t now) {
  UnoTelemetry tel;
  bool have = uno_.telemetry(&tel);
  bool moving = tracker_.moving() || (have && tel.moveSeq != 0);
  if (static_cast<int32_t>(now - telNextMs_) < 0) return;
  telNextMs_ = now + (moving ? cfg::kTelMovingMs : cfg::kTelIdleMs);

  JsonOut j(out_, sizeof out_);
  j.str("t", "tel");
  if (have && tel.cm >= 0) {
    j.num("cm", tel.cm);
  } else {
    j.null("cm");
  }
  if (have) {
    j.ints("line", tel.line, 3).num("tilt", tel.tilt);
  } else {
    j.null("line").null("tilt");
  }
  j.boolean("moving", moving)
      .num("light", light_.level())
      .num("rssi", WiFi.RSSI())
      .num("heap", ESP.getFreeHeap())
      .boolean("uno", uno_.present())
      .num("up", esp_timer_get_time() / 1000000);
  sendJson(j);
}

void Car::pumpStream(uint32_t now) {
  if (!stream_.on) return;
  if (static_cast<int32_t>(now - stream_.untilMs) >= 0) {
    stream_.on = false;
    return;
  }
  if (static_cast<int32_t>(now - stream_.nextMs) < 0) return;
  stream_.nextMs = now + stream_.intervalMs;
  camera_fb_t* fb = camera_.grab(stream_.res, stream_.quality, false);
  if (fb == nullptr) {
    stream_.on = false;
    sendErr(stream_.id, "camera", camera_.error());
    sendEvent("camera_error", camera_.error());
    return;
  }
  uint8_t header[8];
  writeImageHeader(header, true, stream_.id);
  link_.sendImage(header, fb->buf, fb->len);
  camera_.release(fb);
}

void Car::pumpOta(uint32_t now) {
  OtaProgress p;
  while (ota_.takeProgress(&p)) {
    switch (p.state) {
      case OtaProgress::kDownloading:
        sendOta("downloading", p.pct, p.msg);
        break;
      case OtaProgress::kVerifying:
        sendOta("verifying", p.pct, p.msg);
        break;
      case OtaProgress::kReady:
        sendOta("rebooting", 100, "");
        rebootPending_ = true;
        rebootAtMs_ = now + cfg::kRebootDelayMs;
        break;
      case OtaProgress::kFailed:
        sendOta("failed", p.pct, p.msg);
        sendErr(otaId_, "ota", p.msg);
        break;
    }
  }
}

// ---------------------------------------------------------------- messages

bool Car::sendJson(JsonOut& j) {
  if (!j.finish() || !link_.connected()) return false;
  return link_.sendText(j.text(), j.size());
}

void Car::sendHello() {
  char unoFw[16];
  bool haveUnoFw = uno_.firmware(unoFw, sizeof unoFw);
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char macText[18];
  formatMac(mac, macText);
  String ip = WiFi.localIP().toString();
  String ssid = WiFi.SSID();
  const Resolution* maxRes = camera_.ok() ? camera_.maxRes() : nullptr;

  JsonOut j(out_, sizeof out_);
  j.str("t", "hello")
      .str("fw", cfg::kFirmwareVersion)
      .str("uno_fw", haveUnoFw ? unoFw : nullptr)
      .str("camera", camera_.ok() ? camera_.model() : nullptr)
      .num("psram", psramFound() ? static_cast<int64_t>(esp_psram_get_size()) : 0)
      .str("max_res", maxRes != nullptr ? maxRes->name : nullptr)
      .str("mac", macText)
      .str("ip", ip.c_str())
      .str("ssid", ssid.c_str())
      .num("rssi", WiFi.RSSI())
      .str("reset", resetReasonName(static_cast<int>(esp_reset_reason())));
  sendJson(j);
}

void Car::sendAck(uint32_t id, const char* field, int32_t value) {
  JsonOut j(out_, sizeof out_);
  j.str("t", "ack").num("id", id);
  if (field != nullptr) j.num(field, value);
  sendJson(j);
}

void Car::sendErr(uint32_t id, const char* code, const char* msg) {
  JsonOut j(out_, sizeof out_);
  j.str("t", "err");
  if (id != 0) {
    j.num("id", id);
  } else {
    j.null("id");
  }
  j.str("code", code).str("msg", msg != nullptr ? msg : "");
  sendJson(j);
}

void Car::sendEvent(const char* kind, const char* msg) {
  JsonOut j(out_, sizeof out_);
  j.str("t", "event").str("kind", kind).str("msg", msg != nullptr ? msg : "");
  sendJson(j);
}

void Car::sendOta(const char* state, int pct, const char* msg) {
  JsonOut j(out_, sizeof out_);
  j.str("t", "ota").num("id", otaId_).str("state", state).num("pct", pct).str("msg", msg);
  sendJson(j);
}

void Car::sendReply(const Reply& r) {
  if (r.id == 0) return;
  if (r.type == Reply::kErr) return sendErr(r.id, r.code, r.msg);
  JsonOut j(out_, sizeof out_);
  if (r.type == Reply::kAck) {
    j.str("t", "ack").num("id", r.id);
    if (r.hasDeg) j.num("deg", r.deg);
  } else {
    j.str("t", "done").num("id", r.id).str("reason", r.reason);
    if (r.cmStart >= 0) {
      j.num("cm_start", r.cmStart);
    } else {
      j.null("cm_start");
    }
    if (r.cmEnd >= 0) {
      j.num("cm_end", r.cmEnd);
    } else {
      j.null("cm_end");
    }
    j.num("ms", r.ms);
  }
  sendJson(j);
}

Car g_car;

}  // namespace

void appSetup() { g_car.setup(); }
void appLoop() { g_car.loop(); }

}  // namespace scout
