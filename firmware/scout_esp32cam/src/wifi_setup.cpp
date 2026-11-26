#include "wifi_setup.h"

#include <WiFi.h>
#include <esp_mac.h>

#include "config.h"
#include "core/util.h"
#include "core/ws_handshake.h"

namespace scout {

namespace {

// The setup network's own address. Samsung phones only look for a sign-in page when the
// name they check resolves to a public address; for a private one such as 192.168.4.1
// they just report "no internet". Other ESP32 setup pages use 4.3.2.1 for the same
// reason. A phone on the setup network can't reach the real 4.3.2.1 anyway.
const IPAddress kApIp(4, 3, 2, 1);
const IPAddress kApMask(255, 255, 255, 0);

const char kPageHead[] = R"(<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Scout setup</title><style>
:root{--bg:#f3f4f6;--card:#fff;--text:#111827;--muted:#6b7280;--line:#e5e7eb;--accent:#2563eb;--ok:#065f46;--okbg:#d1fae5;--bad:#991b1b;--badbg:#fee2e2}
@media (prefers-color-scheme:dark){:root{--bg:#111827;--card:#1f2937;--text:#f9fafb;--muted:#9ca3af;--line:#374151;--accent:#60a5fa;--ok:#a7f3d0;--okbg:#064e3b;--bad:#fecaca;--badbg:#7f1d1d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:32rem;margin:0 auto;padding:16px}h1{font-size:1.5rem;margin:8px 0 16px}h2{font-size:1.05rem;margin:0 0 12px}
section{background:var(--card);border-radius:14px;padding:16px;margin-bottom:14px;box-shadow:0 1px 2px rgba(0,0,0,.08)}
label{display:block;font-size:.9rem;color:var(--muted);margin:12px 0 4px}
input,select{width:100%;padding:11px 12px;font-size:1rem;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--text)}
button{margin-top:14px;padding:11px 16px;font-size:1rem;font-weight:600;border:0;border-radius:10px;background:var(--accent);color:#fff;width:100%}
button.plain{background:transparent;color:var(--accent);border:1px solid var(--line)}button.del{width:auto;margin:0;padding:6px 12px;background:transparent;color:var(--bad);border:1px solid var(--line);font-weight:500}
ul{list-style:none;margin:0;padding:0}li{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--line)}li:last-child{border-bottom:0}
li span{overflow-wrap:anywhere}.note{color:var(--muted);font-size:.9rem;margin:8px 0 0}.msg{padding:12px;border-radius:10px;margin-bottom:14px;background:var(--okbg);color:var(--ok)}
.msg.bad{background:var(--badbg);color:var(--bad)}form{margin:0}
</style></head><body><main><h1>Scout setup</h1>
)";

const char kPageTail[] = "</main></body></html>";

struct Message {
  const char* code;
  bool bad;
  const char* text;
};

const Message kMessages[] = {
    {"added", false, "Network saved."},
    {"updated", false, "Password for that network updated."},
    {"full", true, "Five networks are saved already. Delete one first."},
    {"badnet", true, "That network name or password isn't valid. WiFi passwords have 8 to 63 characters."},
    {"deleted", false, "Network deleted."},
    {"url", false, "Server address saved."},
    {"badurl", true, "That isn't a server address. It should look like wss://your-server/car/ws"},
    {"key", false, "Car key saved."},
    {"badkey", true, "The car key must be 1 to 128 letters, digits or symbols, without spaces."},
    {"pass", false, "Setup password changed. Use it the next time you join this network."},
    {"badpass", true, "The setup password needs 8 to 63 characters."},
    {"scanned", false, "Network list updated."},
    {"error", true, "Couldn't save that. Please try again."},
};

void appendEscaped(String& page, const char* text) {
  char buf[1024];  // a 199-character server address of nothing but "&" still fits
  htmlEscape(text, buf, sizeof buf);
  page += buf;
}

const char* signalWord(int32_t rssi) {
  if (rssi >= -60) return "strong";
  if (rssi >= -72) return "good";
  return "weak";
}

}  // namespace

void macSuffix(char out[5]) {
  uint8_t mac[6] = {0};
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  snprintf(out, 5, "%02X%02X", mac[4], mac[5]);
}

void wifiPrepareStation() {
  char sfx[5];
  macSuffix(sfx);
  char host[24];
  snprintf(host, sizeof host, "%s%c%c%c%c", cfg::kHostnamePrefix, tolower(sfx[0]), tolower(sfx[1]),
           tolower(sfx[2]), tolower(sfx[3]));
  WiFi.persistent(false);  // the saved networks live in our own NVS namespace
  WiFi.setHostname(host);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // modem sleep adds up to a few hundred ms to every command
  WiFi.setAutoReconnect(true);
  WiFi.setScanMethod(WIFI_ALL_CHANNEL_SCAN);
  WiFi.setSortMethod(WIFI_CONNECT_AP_BY_SIGNAL);
}

bool wifiConnectSaved(const NetworkList& networks, uint32_t budgetMs, bool keepAp) {
  if (networks.count() == 0) return false;
  uint32_t start = millis();
  WiFi.mode(keepAp ? WIFI_AP_STA : WIFI_STA);

  // Scan once so the strongest saved network is tried first.
  static char names[cfg::kScanListMax][33];
  ScanEntry entries[cfg::kScanListMax];
  size_t seen = 0;
  int16_t found = WiFi.scanNetworks(false, false);
  for (int16_t i = 0; i < found && seen < cfg::kScanListMax; ++i) {
    String ssid = WiFi.SSID(i);
    if (ssid.length() == 0 || ssid.length() > 32) continue;
    strcpy(names[seen], ssid.c_str());
    entries[seen].ssid = names[seen];
    entries[seen].rssi = WiFi.RSSI(i);
    ++seen;
  }
  WiFi.scanDelete();
  uint8_t order[NetworkList::kMax];
  uint8_t count = networks.connectOrder(entries, seen, order);

  for (uint8_t k = 0; k < count; ++k) {
    uint32_t used = millis() - start;
    if (used + 2000 > budgetMs) break;
    uint32_t limit = budgetMs - used;
    if (limit > cfg::kWifiPerNetworkMs) limit = cfg::kWifiPerNetworkMs;
    const SavedNetwork& net = networks.at(order[k]);
    bool open = net.pass[0] == '\0';
    WiFi.setMinSecurity(open ? WIFI_AUTH_OPEN : WIFI_AUTH_WPA2_PSK);
    WiFi.begin(net.ssid, open ? nullptr : net.pass);
    uint32_t t0 = millis();
    while (millis() - t0 < limit) {
      wl_status_t status = WiFi.status();
      if (status == WL_CONNECTED) return true;
      if (status == WL_CONNECT_FAILED) break;  // wrong password
      delay(100);
    }
    WiFi.disconnect(false, false);
    delay(100);
  }
  return false;
}

// ---------------------------------------------------------------- setup portal

void SetupPortal::begin(Settings* settings, const char* why) {
  settings_ = settings;
  strncpy(why_, why, sizeof why_ - 1);
  why_[sizeof why_ - 1] = '\0';
  char sfx[5];
  macSuffix(sfx);
  snprintf(ssid_, sizeof ssid_, "%s%s", cfg::kSetupSsidPrefix, sfx);

  WiFi.persistent(false);
  WiFi.mode(WIFI_AP_STA);  // STA as well, for scanning and the periodic retries
  // DHCP offers the car's own address as DNS server (the last argument). The DNS server
  // below answers every name with the car, and the web server sends requests for other
  // hosts to the page, so a phone checking for a sign-in page finds this one.
  WiFi.softAPConfig(kApIp, kApIp, kApMask, IPAddress(static_cast<uint32_t>(0)), kApIp);
  WiFi.softAP(ssid_, settings_->setupPassword(), cfg::kSetupChannel, 0, cfg::kSetupMaxClients, false,
              WIFI_AUTH_WPA2_PSK);
  // DHCP option 114 names the page too (http://4.3.2.1), for phones that read it.
  WiFi.AP.enableDhcpCaptivePortal();

  dns_.setErrorReplyCode(DNSReplyCode::NoError);
  dns_.start(53, "*", kApIp);

  web_.on("/", HTTP_GET, [this]() { handleRoot(); });
  web_.on("/scan", HTTP_POST, [this]() { handleScan(); });
  web_.on("/net/add", HTTP_POST, [this]() { handleAddNetwork(); });
  web_.on("/net/del", HTTP_POST, [this]() { handleDeleteNetwork(); });
  web_.on("/server", HTTP_POST, [this]() { handleServer(); });
  web_.on("/key", HTTP_POST, [this]() { handleKey(); });
  web_.on("/appass", HTTP_POST, [this]() { handlePassword(); });
  web_.on("/restart", HTTP_POST, [this]() { handleRestart(); });
  web_.onNotFound([this]() { handleNotFound(); });
  web_.begin();

  WiFi.scanNetworks(true, false);  // in the background, so the list is ready
  scanPending_ = true;
  lastClientMs_ = millis();
  lastRetryMs_ = millis();
}

bool SetupPortal::loop() {
  web_.handleClient();
  if (scanPending_) {
    int16_t state = WiFi.scanComplete();
    if (state >= 0) {
      collectScan();
      scanPending_ = false;
    } else if (state == WIFI_SCAN_FAILED) {
      scanPending_ = false;
    }
  }
  uint32_t now = millis();
  if (WiFi.softAPgetStationNum() > 0) lastClientMs_ = now;
  bool worthTrying = settings_->networks().count() > 0 && settings_->serverUrl()[0] != '\0' && settings_->hasCarKey();
  if (worthTrying && !scanPending_ && now - lastClientMs_ >= cfg::kSetupRetryMs &&
      now - lastRetryMs_ >= cfg::kSetupRetryMs) {
    retrySavedNetworks();
    lastRetryMs_ = millis();
  }
  return !restart_;
}

void SetupPortal::retrySavedNetworks() {
  if (wifiConnectSaved(settings_->networks(), cfg::kWifiConnectBudgetMs, true)) {
    restart_ = true;  // a network works again: restart into normal mode
    return;
  }
  WiFi.disconnect(false, false);  // stop the station from hopping channels under the AP
}

void SetupPortal::collectScan() {
  seenCount_ = 0;
  int16_t found = WiFi.scanComplete();
  for (int16_t i = 0; i < found; ++i) {
    String ssid = WiFi.SSID(i);
    if (ssid.length() == 0 || ssid.length() > 32) continue;
    int32_t rssi = WiFi.RSSI(i);
    bool open = WiFi.encryptionType(i) == WIFI_AUTH_OPEN;
    int existing = -1;
    for (uint8_t k = 0; k < seenCount_; ++k) {
      if (strcmp(seen_[k].ssid, ssid.c_str()) == 0) existing = k;
    }
    if (existing >= 0) {
      if (rssi > seen_[existing].rssi) seen_[existing].rssi = rssi;
      continue;
    }
    if (seenCount_ >= sizeof seen_ / sizeof seen_[0]) {
      // Full: replace the weakest if this one is stronger.
      uint8_t weakest = 0;
      for (uint8_t k = 1; k < seenCount_; ++k) {
        if (seen_[k].rssi < seen_[weakest].rssi) weakest = k;
      }
      if (rssi <= seen_[weakest].rssi) continue;
      existing = weakest;
    } else {
      existing = seenCount_++;
    }
    strcpy(seen_[existing].ssid, ssid.c_str());
    seen_[existing].rssi = rssi;
    seen_[existing].open = open;
  }
  WiFi.scanDelete();
  // Strongest first.
  for (uint8_t i = 1; i < seenCount_; ++i) {
    for (uint8_t k = i; k > 0 && seen_[k].rssi > seen_[k - 1].rssi; --k) {
      Seen tmp = seen_[k];
      seen_[k] = seen_[k - 1];
      seen_[k - 1] = tmp;
    }
  }
}

bool SetupPortal::captive() {
  String host = web_.hostHeader();
  int colon = host.indexOf(':');
  if (colon >= 0) host = host.substring(0, colon);
  if (host == kApIp.toString()) return false;
  remember(host, web_.uri());
  web_.sendHeader("Location", String("http://") + kApIp.toString() + "/", true);
  web_.send(302, "text/plain", "");
  return true;
}

void SetupPortal::remember(const String& host, const String& uri) {
  char entry[sizeof recent_[0]];
  snprintf(entry, sizeof entry, "%s%s", host.c_str(), uri.c_str());
  uint8_t newest = (recentNext_ + kRecentMax - 1) % kRecentMax;
  if (recentCount_ > 0 && strcmp(recent_[newest], entry) == 0) return;  // phones repeat their checks
  strcpy(recent_[recentNext_], entry);
  recentNext_ = (recentNext_ + 1) % kRecentMax;
  if (recentCount_ < kRecentMax) ++recentCount_;
}

void SetupPortal::redirectHome(const char* message) {
  web_.sendHeader("Location", String("/?m=") + message, true);
  web_.sendHeader("Cache-Control", "no-store");
  web_.send(303, "text/plain", "");
}

void SetupPortal::handleNotFound() {
  if (captive()) return;
  web_.sendHeader("Location", "/", true);
  web_.send(302, "text/plain", "");
}

void SetupPortal::handleRoot() {
  if (captive()) return;
  String page;
  page.reserve(7000);
  page += kPageHead;

  String code = web_.arg("m");
  for (const Message& m : kMessages) {
    if (code == m.code) {
      page += m.bad ? "<div class=\"msg bad\">" : "<div class=\"msg\">";
      page += m.text;
      page += "</div>";
    }
  }

  if (why_[0] != '\0') {
    page += "<section><p class=\"note\" style=\"margin:0\">";
    appendEscaped(page, why_);
    page += "</p></section>";
  }

  // WiFi networks
  NetworkList& nets = settings_->networks();
  page += "<section><h2>WiFi networks</h2>";
  if (nets.count() == 0) {
    page += "<p class=\"note\">No networks saved yet. Add the WiFi the car should use (2.4 GHz).</p>";
  } else {
    page += "<ul>";
    for (uint8_t i = 0; i < nets.count(); ++i) {
      page += "<li><span>";
      appendEscaped(page, nets.at(i).ssid);
      page += nets.at(i).pass[0] == '\0' ? " (open)" : "";
      page += "</span><form method=\"post\" action=\"/net/del\"><input type=\"hidden\" name=\"i\" value=\"";
      page += String(i);
      page += "\"><button class=\"del\">Delete</button></form></li>";
    }
    page += "</ul>";
  }
  if (nets.count() < NetworkList::kMax) {
    page += "<form method=\"post\" action=\"/net/add\"><label for=\"pick\">Network</label><select id=\"pick\" "
            "name=\"pick\"><option value=\"\">";
    page += seenCount_ > 0 ? "Choose a network" : "No networks found yet";
    page += "</option>";
    for (uint8_t i = 0; i < seenCount_; ++i) {
      page += "<option value=\"";
      appendEscaped(page, seen_[i].ssid);
      page += "\">";
      appendEscaped(page, seen_[i].ssid);
      page += " (";
      page += signalWord(seen_[i].rssi);
      page += seen_[i].open ? ", open" : "";
      page += ")</option>";
    }
    page += "</select><label for=\"ssid\">Or type its name (for hidden networks)</label>"
            "<input id=\"ssid\" name=\"ssid\" maxlength=\"32\" autocapitalize=\"none\" autocomplete=\"off\" "
            "spellcheck=\"false\"><label for=\"pass\">Password (empty for an open network)</label>"
            "<input id=\"pass\" name=\"pass\" type=\"password\" maxlength=\"64\" autocomplete=\"off\">"
            "<button>Add network</button></form>";
  } else {
    page += "<p class=\"note\">Five networks saved: delete one to add another.</p>";
  }
  page += "<form method=\"post\" action=\"/scan\"><button class=\"plain\">Scan again</button></form></section>";

  // Server address
  page += "<section><h2>Server</h2><form method=\"post\" action=\"/server\"><label for=\"url\">Server "
          "address</label><input id=\"url\" name=\"url\" maxlength=\"190\" autocapitalize=\"none\" "
          "autocomplete=\"off\" spellcheck=\"false\" placeholder=\"wss://your-server/car/ws\" value=\"";
  appendEscaped(page, settings_->serverUrl());
  page += "\"><button>Save address</button></form></section>";

  // Car key (write-only)
  page += "<section><h2>Car key</h2><p class=\"note\">The key is ";
  page += settings_->hasCarKey() ? "<b>set</b>. It is never shown again; saving a new one replaces it."
                                 : "<b>not set</b>. Copy it from the Scout website.";
  page += "</p><form method=\"post\" action=\"/key\"><label for=\"key\">New car key</label><input id=\"key\" "
          "name=\"key\" type=\"password\" maxlength=\"128\" autocomplete=\"off\"><button>Save key</button>"
          "</form></section>";

  // Setup password
  page += "<section><h2>Setup password</h2><form method=\"post\" action=\"/appass\"><label for=\"ap\">New password "
          "for ";
  appendEscaped(page, ssid_);
  page += "</label><input id=\"ap\" name=\"pass\" type=\"password\" minlength=\"8\" maxlength=\"63\" "
          "autocomplete=\"new-password\"><button>Change password</button></form></section>";

  // Finish
  page += "<section><h2>Done?</h2><form method=\"post\" action=\"/restart\"><button>Save and restart</button>"
          "</form><p class=\"note\">Everything above is saved as soon as you press its button. The car restarts "
          "and joins your WiFi. Firmware ";
  page += cfg::kFirmwareVersion;
  page += ".</p></section>";

  // The checks phones make for a sign-in page, for when this page doesn't open by itself.
  page += "<section><details><summary>Sign-in checks</summary><p class=\"note\">Addresses that phones "
          "on this network asked for, newest first. Each was sent on to this page.</p>";
  if (recentCount_ == 0) {
    page += "<p class=\"note\">None yet.</p>";
  } else {
    page += "<ul>";
    for (uint8_t k = 0; k < recentCount_; ++k) {
      page += "<li><span>";
      appendEscaped(page, recent_[(recentNext_ + kRecentMax - 1 - k) % kRecentMax]);
      page += "</span></li>";
    }
    page += "</ul>";
  }
  page += "</details></section>";
  page += kPageTail;

  web_.sendHeader("Cache-Control", "no-store");
  web_.send(200, "text/html; charset=utf-8", page);
}

void SetupPortal::handleScan() {
  WiFi.scanNetworks(false, false);  // a few seconds
  collectScan();
  scanPending_ = false;
  redirectHome("scanned");
}

void SetupPortal::handleAddNetwork() {
  String ssid = web_.arg("ssid");
  if (ssid.length() == 0) ssid = web_.arg("pick");
  String pass = web_.arg("pass");
  NetworkList::AddResult result = settings_->networks().add(ssid.c_str(), pass.c_str());
  if (result == NetworkList::kFull) return redirectHome("full");
  if (result == NetworkList::kInvalid) return redirectHome("badnet");
  if (!settings_->saveNetworks()) return redirectHome("error");
  redirectHome(result == NetworkList::kUpdated ? "updated" : "added");
}

void SetupPortal::handleDeleteNetwork() {
  long index = web_.arg("i").toInt();
  if (index < 0 || index >= settings_->networks().count() ||
      !settings_->networks().remove(static_cast<uint8_t>(index))) {
    return redirectHome("error");
  }
  if (!settings_->saveNetworks()) return redirectHome("error");
  redirectHome("deleted");
}

void SetupPortal::handleServer() {
  String url = web_.arg("url");
  url.trim();
  ws::ServerUrl parsed;
  if (url.length() == 0 || !ws::parseServerUrl(url.c_str(), &parsed)) return redirectHome("badurl");
  redirectHome(settings_->setServerUrl(url.c_str()) ? "url" : "error");
}

void SetupPortal::handleKey() {
  String key = web_.arg("key");
  key.trim();
  if (!validCarKey(key.c_str())) return redirectHome("badkey");
  redirectHome(settings_->setCarKey(key.c_str()) ? "key" : "error");
}

void SetupPortal::handlePassword() {
  String pass = web_.arg("pass");
  if (!validSetupPassword(pass.c_str())) return redirectHome("badpass");
  redirectHome(settings_->setSetupPassword(pass.c_str()) ? "pass" : "error");
}

void SetupPortal::handleRestart() {
  String page = kPageHead;
  page += "<section><h2>Restarting</h2><p>The car is restarting and will join your WiFi. You can close this page "
          "and reconnect your phone to your usual network.</p></section>";
  page += kPageTail;
  web_.send(200, "text/html; charset=utf-8", page);
  restart_ = true;
}

}  // namespace scout
