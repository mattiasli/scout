#include "networks.h"

#include <string.h>

namespace scout {

namespace {

bool isHex(char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F'); }

}  // namespace

bool validSsid(const char* ssid) {
  if (ssid == nullptr) return false;
  size_t n = strlen(ssid);
  if (n == 0 || n > 32) return false;
  for (size_t i = 0; i < n; ++i) {
    unsigned char c = static_cast<unsigned char>(ssid[i]);
    if (c < 0x20 || c == 0x7F) return false;
  }
  return true;
}

bool validPassphrase(const char* pass) {
  if (pass == nullptr) return false;
  size_t n = strlen(pass);
  if (n == 0) return true;
  if (n == 64) {
    for (size_t i = 0; i < n; ++i) {
      if (!isHex(pass[i])) return false;
    }
    return true;
  }
  if (n < 8 || n > 63) return false;
  for (size_t i = 0; i < n; ++i) {
    if (pass[i] < 0x20 || pass[i] > 0x7E) return false;
  }
  return true;
}

int NetworkList::find(const char* ssid) const {
  for (uint8_t i = 0; i < count_; ++i) {
    if (strcmp(nets_[i].ssid, ssid) == 0) return i;
  }
  return -1;
}

NetworkList::AddResult NetworkList::add(const char* ssid, const char* pass) {
  if (!validSsid(ssid) || !validPassphrase(pass)) return kInvalid;
  int existing = find(ssid);
  if (existing >= 0) {
    strcpy(nets_[existing].pass, pass);
    return kUpdated;
  }
  if (count_ >= kMax) return kFull;
  strcpy(nets_[count_].ssid, ssid);
  strcpy(nets_[count_].pass, pass);
  ++count_;
  return kAdded;
}

bool NetworkList::remove(uint8_t index) {
  if (index >= count_) return false;
  for (uint8_t i = index; i + 1 < count_; ++i) nets_[i] = nets_[i + 1];
  --count_;
  return true;
}

uint8_t NetworkList::connectOrder(const ScanEntry* scan, size_t scanCount, uint8_t* order) const {
  int32_t best[kMax];
  bool seen[kMax];
  for (uint8_t i = 0; i < count_; ++i) {
    seen[i] = false;
    best[i] = 0;
    for (size_t j = 0; j < scanCount; ++j) {
      if (scan[j].ssid == nullptr || strcmp(scan[j].ssid, nets_[i].ssid) != 0) continue;
      if (!seen[i] || scan[j].rssi > best[i]) best[i] = scan[j].rssi;
      seen[i] = true;
    }
  }
  uint8_t n = 0;
  // Seen networks, strongest first (stable for equal signal).
  bool used[kMax] = {false, false, false, false, false};
  for (;;) {
    int pick = -1;
    for (uint8_t i = 0; i < count_; ++i) {
      if (!seen[i] || used[i]) continue;
      if (pick < 0 || best[i] > best[pick]) pick = i;
    }
    if (pick < 0) break;
    used[pick] = true;
    order[n++] = static_cast<uint8_t>(pick);
  }
  for (uint8_t i = 0; i < count_; ++i) {
    if (!seen[i]) order[n++] = i;
  }
  return n;
}

}  // namespace scout
