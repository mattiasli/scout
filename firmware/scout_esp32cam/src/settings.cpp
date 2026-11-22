#include "settings.h"

#include <Preferences.h>

#include "config.h"
#include "core/util.h"
#include "core/ws_handshake.h"

namespace scout {

namespace {

const char kNamespace[] = "scout";

void copy(char* out, size_t cap, const char* text) {
  strncpy(out, text, cap - 1);
  out[cap - 1] = '\0';
}

void keyFor(char* out, size_t cap, const char* prefix, uint8_t index) { snprintf(out, cap, "%s%u", prefix, index); }

}  // namespace

bool loadResetRecord(ResetRecord* out) {
  Preferences prefs;
  if (!prefs.begin(kNamespace, true)) return false;
  bool ok = prefs.getBytesLength("rst") == sizeof *out && prefs.getBytes("rst", out, sizeof *out) == sizeof *out;
  prefs.end();
  return ok;
}

void saveResetRecord(const ResetRecord& record) {
  Preferences prefs;
  if (!prefs.begin(kNamespace, false)) return;
  ResetRecord old;
  bool same = prefs.getBytesLength("rst") == sizeof old && prefs.getBytes("rst", &old, sizeof old) == sizeof old &&
              memcmp(&old, &record, sizeof old) == 0;
  if (!same) prefs.putBytes("rst", &record, sizeof record);
  prefs.end();
}

void Settings::load() {
  Preferences prefs;
  networks_.clear();
  copy(serverUrl_, sizeof serverUrl_, cfg::kDefaultServerUrl);
  carKey_[0] = '\0';
  copy(setupPassword_, sizeof setupPassword_, cfg::kSetupDefaultPassword);
  if (!prefs.begin(kNamespace, true)) return;  // nothing saved yet

  uint8_t count = prefs.getUChar("n", 0);
  for (uint8_t i = 0; i < count && i < NetworkList::kMax; ++i) {
    char key[8];
    char ssid[33] = "";
    char pass[65] = "";
    keyFor(key, sizeof key, "s", i);
    prefs.getString(key, ssid, sizeof ssid);
    keyFor(key, sizeof key, "p", i);
    prefs.getString(key, pass, sizeof pass);
    networks_.add(ssid, pass);  // skips anything invalid
  }

  char text[200] = "";
  if (prefs.getString("url", text, sizeof text) > 0) {
    ws::ServerUrl parsed;
    if (ws::parseServerUrl(text, &parsed)) copy(serverUrl_, sizeof serverUrl_, text);
  }
  char key[129] = "";
  if (prefs.getString("key", key, sizeof key) > 0 && validCarKey(key)) copy(carKey_, sizeof carKey_, key);
  char pass[64] = "";
  if (prefs.getString("appass", pass, sizeof pass) > 0 && validSetupPassword(pass)) {
    copy(setupPassword_, sizeof setupPassword_, pass);
  }
  prefs.end();
}

bool Settings::saveNetworks() {
  Preferences prefs;
  if (!prefs.begin(kNamespace, false)) return false;
  bool ok = true;
  for (uint8_t i = 0; i < NetworkList::kMax; ++i) {
    char key[8];
    if (i < networks_.count()) {
      keyFor(key, sizeof key, "s", i);
      ok = prefs.putString(key, networks_.at(i).ssid) > 0 && ok;
      keyFor(key, sizeof key, "p", i);
      prefs.putString(key, networks_.at(i).pass);  // 0 bytes written is fine for an open network
    } else {
      keyFor(key, sizeof key, "s", i);
      if (prefs.isKey(key)) prefs.remove(key);
      keyFor(key, sizeof key, "p", i);
      if (prefs.isKey(key)) prefs.remove(key);
    }
  }
  ok = prefs.putUChar("n", networks_.count()) > 0 && ok;
  prefs.end();
  return ok;
}

bool Settings::setServerUrl(const char* url) {
  if (url[0] != '\0') {
    ws::ServerUrl parsed;
    if (strlen(url) >= sizeof serverUrl_ || !ws::parseServerUrl(url, &parsed)) return false;
  }
  Preferences prefs;
  if (!prefs.begin(kNamespace, false)) return false;
  bool ok = url[0] == '\0' ? (prefs.isKey("url") ? prefs.remove("url") : true) : prefs.putString("url", url) > 0;
  prefs.end();
  if (ok) copy(serverUrl_, sizeof serverUrl_, url);
  return ok;
}

bool Settings::setCarKey(const char* key) {
  if (key[0] != '\0' && !validCarKey(key)) return false;
  Preferences prefs;
  if (!prefs.begin(kNamespace, false)) return false;
  bool ok = key[0] == '\0' ? (prefs.isKey("key") ? prefs.remove("key") : true) : prefs.putString("key", key) > 0;
  prefs.end();
  if (ok) copy(carKey_, sizeof carKey_, key);
  return ok;
}

bool Settings::setSetupPassword(const char* pass) {
  if (!validSetupPassword(pass)) return false;
  Preferences prefs;
  if (!prefs.begin(kNamespace, false)) return false;
  bool ok = prefs.putString("appass", pass) > 0;
  prefs.end();
  if (ok) copy(setupPassword_, sizeof setupPassword_, pass);
  return ok;
}

}  // namespace scout
