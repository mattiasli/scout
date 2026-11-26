// Joining a saved WiFi network, and setup mode: the "Scout-Setup-XXXX" access point
// with a captive portal page for networks, server address, car key and password.

#ifndef SCOUT_WIFI_SETUP_H
#define SCOUT_WIFI_SETUP_H

#include <Arduino.h>
#include <DNSServer.h>
#include <WebServer.h>

#include "settings.h"

namespace scout {

// Station mode, our own credential storage (not the IDF's), no power saving.
void wifiPrepareStation();

// Tries the saved networks, strongest first, until one connects or `budgetMs` runs
// out. Blocks. Keeps the setup access point up when `keepAp` is set.
bool wifiConnectSaved(const NetworkList& networks, uint32_t budgetMs, bool keepAp);

// "Scout-Setup-" + the last 4 hex digits of the MAC; also used for the hostname.
void macSuffix(char out[5]);

class SetupPortal {
 public:
  // `why` is shown at the top of the page (why the car is in setup mode).
  void begin(Settings* settings, const char* why);
  // Serves the page; every few minutes with nobody connected it retries the saved
  // networks and restarts the car if one works. Returns false once the owner asked
  // for a restart (the caller then restarts).
  bool loop();

 private:
  void handleRoot();
  void handleScan();
  void handleAddNetwork();
  void handleDeleteNetwork();
  void handleServer();
  void handleKey();
  void handlePassword();
  void handleRestart();
  void handleNotFound();
  void redirectHome(const char* message);
  bool captive();  // redirects requests for other hosts to the portal
  void remember(const String& host, const String& uri);  // for the page's "Sign-in checks"
  void collectScan();
  void retrySavedNetworks();

  Settings* settings_ = nullptr;
  WebServer web_{80};
  DNSServer dns_;
  char why_[96] = "";
  char ssid_[24] = "";
  bool restart_ = false;
  bool scanPending_ = false;
  uint32_t lastClientMs_ = 0;
  uint32_t lastRetryMs_ = 0;

  static constexpr uint8_t kRecentMax = 8;
  char recent_[kRecentMax][64] = {};
  uint8_t recentNext_ = 0;
  uint8_t recentCount_ = 0;

  struct Seen {
    char ssid[33];
    int32_t rssi;
    bool open;
  };
  Seen seen_[20];
  uint8_t seenCount_ = 0;
};

}  // namespace scout

#endif  // SCOUT_WIFI_SETUP_H
