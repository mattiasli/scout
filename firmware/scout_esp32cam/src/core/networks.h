// The saved WiFi networks (up to five) and the order to try them in.

#ifndef SCOUT_CORE_NETWORKS_H
#define SCOUT_CORE_NETWORKS_H

#include <stddef.h>
#include <stdint.h>

namespace scout {

struct SavedNetwork {
  char ssid[33];  // up to 32 bytes
  char pass[65];  // 8..63 characters, 64 hex digits, or empty for an open network
};

struct ScanEntry {
  const char* ssid;
  int32_t rssi;
};

// 1..32 bytes, no control characters.
bool validSsid(const char* ssid);
// Empty (open network), 8..63 printable ASCII characters, or exactly 64 hex digits.
bool validPassphrase(const char* pass);

class NetworkList {
 public:
  static const uint8_t kMax = 5;
  enum AddResult { kAdded, kUpdated, kFull, kInvalid };

  NetworkList() : count_(0) {}

  // Adds a network, or replaces the password of one already saved under that name.
  AddResult add(const char* ssid, const char* pass);
  bool remove(uint8_t index);
  void clear() { count_ = 0; }

  uint8_t count() const { return count_; }
  const SavedNetwork& at(uint8_t index) const { return nets_[index < kMax ? index : 0]; }
  int find(const char* ssid) const;  // index, or -1

  // Writes the indexes of the saved networks in the order to try them: those seen in
  // the scan from the strongest signal down, then the ones not seen (they may be
  // hidden) in the order they were saved. Returns how many were written (count()).
  uint8_t connectOrder(const ScanEntry* scan, size_t scanCount, uint8_t* order) const;

 private:
  SavedNetwork nets_[kMax];
  uint8_t count_;
};

}  // namespace scout

#endif  // SCOUT_CORE_NETWORKS_H
