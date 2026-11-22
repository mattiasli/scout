// Settings kept in NVS (Preferences namespace "scout"): saved WiFi networks, the server
// address, the car key and the setup access point's password.
//
// NVS is not encrypted on this board: anyone holding the car can read these back out
// of the flash chip. The car key is never shown on the setup page or sent anywhere but
// the server.

#ifndef SCOUT_SETTINGS_H
#define SCOUT_SETTINGS_H

#include <Arduino.h>

#include "core/networks.h"
#include "core/reset_counter.h"

namespace scout {

// A copy of the reset counter's record in NVS, for boards where RTC memory doesn't
// survive the reset button. False when none is stored.
bool loadResetRecord(ResetRecord* out);
void saveResetRecord(const ResetRecord& record);

class Settings {
 public:
  void load();

  NetworkList& networks() { return networks_; }
  bool saveNetworks();

  const char* serverUrl() const { return serverUrl_; }
  bool setServerUrl(const char* url);  // "" clears it

  bool hasCarKey() const { return carKey_[0] != '\0'; }
  const char* carKey() const { return carKey_; }
  bool setCarKey(const char* key);  // "" clears it

  const char* setupPassword() const { return setupPassword_; }
  bool setSetupPassword(const char* pass);

 private:
  NetworkList networks_;
  char serverUrl_[200] = "";
  char carKey_[129] = "";
  char setupPassword_[64] = "";
};

}  // namespace scout

#endif  // SCOUT_SETTINGS_H
