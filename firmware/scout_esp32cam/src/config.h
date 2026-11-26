// Every tunable of the ESP32-CAM firmware in one place.

#ifndef SCOUT_CONFIG_H
#define SCOUT_CONFIG_H

#include <stddef.h>
#include <stdint.h>

namespace scout {
namespace cfg {

// ---------------------------------------------------------------- firmware

constexpr const char* kFirmwareVersion = "0.1.0";

// The car-link address used until one is saved on the setup page. This is the public
// Scout server; build with your own address if you run your own server.
constexpr const char* kDefaultServerUrl = "wss://scout-production-a08160b6.up.railway.app/car/ws";

// ---------------------------------------------------------------- serial link to the UNO

constexpr uint32_t kUnoBaud = 115200;
constexpr size_t kUnoRxBufferBytes = 1024;
constexpr uint32_t kUnoPingMs = 250;        // uart.md asks for at most 300 ms
constexpr uint32_t kUnoPresentMs = 1000;    // present = a valid frame within this time
constexpr uint32_t kUnoAckTimeoutMs = 1000; // stop/tilt/conf without an ACK -> err timeout
constexpr uint32_t kUnoDoneGraceMs = 1500;  // move/test without DONE after ms + this -> error
constexpr uint8_t kUnoQueueDepth = 32;      // frames waiting for the main loop
constexpr uint32_t kUnoTaskStack = 4096;
constexpr uint8_t kUnoTaskPriority = 3;     // above the loop task (1), so PINGs keep going
constexpr int32_t kTiltLimitDeg = 90;       // the UNO clamps further to its safe range

// ---------------------------------------------------------------- car link

constexpr uint32_t kLinkConnectTimeoutMs = 10000;    // TCP connect, and socket reads/writes
constexpr uint32_t kLinkTlsHandshakeTimeoutS = 15;
constexpr uint32_t kLinkUpgradeTimeoutMs = 10000;    // waiting for "101 Switching Protocols"
constexpr uint32_t kLinkSilenceMs = 15000;           // nothing from the server -> reconnect
constexpr size_t kLinkMaxMessageBytes = 4096;        // larger server messages close the link
constexpr size_t kLinkSendChunkBytes = 4096;         // masked pieces of outgoing frames
constexpr size_t kLinkReadBudgetBytes = 8192;        // per poll(), so one poll can't hog the loop
constexpr uint32_t kBackoffBaseMs = 2000;
constexpr uint32_t kBackoffCapMs = 60000;
constexpr uint32_t kBackoffJitterPercent = 20;
constexpr uint32_t kBackoffStableMs = 60000;
constexpr size_t kJsonOutBytes = 1024;               // largest message the car sends

// ---------------------------------------------------------------- telemetry

constexpr uint32_t kTelIdleMs = 1000;
constexpr uint32_t kTelMovingMs = 200;

// ---------------------------------------------------------------- camera (AI-Thinker pinout)

constexpr int kCamPinPwdn = 32;
constexpr int kCamPinReset = -1;
constexpr int kCamPinXclk = 0;
constexpr int kCamPinSiod = 26;
constexpr int kCamPinSioc = 27;
constexpr int kCamPinY9 = 35;
constexpr int kCamPinY8 = 34;
constexpr int kCamPinY7 = 39;
constexpr int kCamPinY6 = 36;
constexpr int kCamPinY5 = 21;
constexpr int kCamPinY4 = 19;
constexpr int kCamPinY3 = 18;
constexpr int kCamPinY2 = 5;
constexpr int kCamPinVsync = 25;
constexpr int kCamPinHref = 23;
constexpr int kCamPinPclk = 22;
constexpr int kCamXclkHz = 20000000;

// The vendor FAQ says ESP32-CAM-V2.0 boards need vflip. The server's `conf` overrides
// both on every connection.
constexpr bool kDefaultVflip = true;
constexpr bool kDefaultHmirror = false;

constexpr const char* kDefaultSnapRes = "vga";
constexpr int kDefaultJpegQuality = 12;
constexpr int kJpegQualityMin = 4;
constexpr int kJpegQualityMax = 63;
// Without PSRAM the single frame buffer sits in internal RAM next to WiFi and TLS, so
// pictures are limited to this size.
constexpr const char* kNoPsramMaxRes = "vga";
// JPEG frame buffers are sized for the largest picture: width * height / this. The
// driver's default (/5) is too small for low `q` values at high resolutions.
constexpr uint32_t kJpegBufferDivisor = 3;
// Frames to throw away after changing size or quality (exposure settles, and the
// queue still holds frames of the old size).
constexpr int kDiscardAfterChange = 2;
// Frames to throw away before a snapshot, so the picture is taken after the request.
constexpr int kDiscardBeforeSnap = 1;
constexpr int kGrabAttempts = 3;
constexpr int kStreamFpsMin = 1;
constexpr int kStreamFpsMax = 10;
constexpr int kStreamSecsMin = 1;
constexpr int kStreamSecsMax = 120;

// ---------------------------------------------------------------- light (white LED, GPIO4)

constexpr int kLightPin = 4;
constexpr uint32_t kLightFreqHz = 5000;
constexpr uint8_t kLightResolutionBits = 10;  // duty 0..1023
// Level 100 drives the LED at 25 % duty, not 100 %. The LED hangs off the board's
// small 3.3 V regulator through a transistor with almost no current limiting: at full
// duty it draws a few hundred mA, gets hot within seconds, and the extra load next to
// WiFi transmit bursts is a classic cause of brownout resets on ESP32-CAM boards.
// A quarter of that is plenty to light up things a metre in front of the camera.
constexpr uint32_t kLightMaxDuty = 256;

// ---------------------------------------------------------------- status LED (red, GPIO33)

// Active low on AI-Thinker style boards. GPIO33 isn't used by the camera or the shield.
constexpr int kStatusLedPin = 33;
constexpr bool kStatusLedActiveLow = true;
constexpr uint32_t kStatusLedTickMs = 25;

// ---------------------------------------------------------------- WiFi

constexpr uint32_t kWifiConnectBudgetMs = 30000;   // at boot: then setup mode
constexpr uint32_t kWifiPerNetworkMs = 12000;      // per saved network
constexpr uint32_t kWifiLostRescanMs = 120000;     // lost for this long -> try every saved network
constexpr const char* kHostnamePrefix = "scout-";

// ---------------------------------------------------------------- setup mode

constexpr const char* kSetupSsidPrefix = "Scout-Setup-";  // + last 4 hex digits of the MAC
constexpr const char* kSetupDefaultPassword = "scoutsetup";
constexpr uint8_t kSetupChannel = 6;
constexpr uint8_t kSetupMaxClients = 2;
constexpr uint32_t kSetupRetryMs = 180000;  // with nobody on the AP, retry saved networks
// One press of the reset button starts setup mode (resetButtonPressed() in core/util.h).
constexpr uint32_t kSetupButtonWaitMs = 600;     // at boot, how long to wait for the UNO's PONG
constexpr uint32_t kSetupButtonMarginMs = 3000;  // the UNO must have run this much longer
// In setup mode the white light blinks along with the status LED, dimly (level 0..100 as
// for `light`): not every board has a red LED you can see.
constexpr int32_t kSetupLampLevel = 5;
constexpr uint8_t kScanListMax = 20;

// ---------------------------------------------------------------- OTA

constexpr uint32_t kOtaHttpTimeoutMs = 15000;
constexpr uint32_t kOtaTlsHandshakeTimeoutS = 20;
constexpr uint16_t kOtaRedirectLimit = 5;
constexpr size_t kOtaBufferBytes = 4096;
constexpr uint32_t kOtaTaskStack = 12288;  // TLS handshake + HTTP client
constexpr uint8_t kOtaTaskPriority = 1;
constexpr int kOtaProgressStepPct = 10;
// Two TLS sessions (car link + download) need about this much internal RAM.
constexpr uint32_t kOtaMinFreeInternal = 60000;
// A freshly installed image that hasn't reached the server by then rolls back.
constexpr uint32_t kOtaConfirmDeadlineMs = 10UL * 60UL * 1000UL;
constexpr uint32_t kRebootDelayMs = 300;

}  // namespace cfg
}  // namespace scout

#endif  // SCOUT_CONFIG_H
