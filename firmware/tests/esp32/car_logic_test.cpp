// Resolution names, camera models, the reset counter, saved networks and small helpers.

#include <cctype>
#include <cmath>
#include <cstring>
#include <limits>
#include <string>

#include "camera_info.h"
#include "networks.h"
#include "testing.h"
#include "util.h"

using scout::NetworkList;

// ---------------------------------------------------------------- resolutions

TEST(resolution_names_match_car_link) {
  struct Want {
    const char* name;
    int w, h;
  } want[] = {{"qvga", 320, 240},   {"vga", 640, 480},    {"svga", 800, 600},   {"xga", 1024, 768},
              {"hd", 1280, 720},    {"sxga", 1280, 1024}, {"uxga", 1600, 1200}, {"qxga", 2048, 1536}};
  CHECK_EQ(scout::resolutionCount(), sizeof want / sizeof want[0]);
  for (const Want& w : want) {
    const scout::Resolution* r = scout::findResolution(w.name);
    CHECK(r != nullptr);
    if (r == nullptr) continue;
    CHECK_EQ(r->width, w.w);
    CHECK_EQ(r->height, w.h);
  }
  CHECK(scout::findResolution("QVGA") == nullptr);
  CHECK(scout::findResolution("fhd") == nullptr);
  CHECK(scout::findResolution(nullptr) == nullptr);
}

TEST(resolution_table_is_ordered) {
  for (size_t i = 1; i < scout::resolutionCount(); ++i) {
    const scout::Resolution& a = scout::resolutionAt(i - 1);
    const scout::Resolution& b = scout::resolutionAt(i);
    CHECK(a.framesize < b.framesize);
    CHECK(static_cast<long>(a.width) * a.height < static_cast<long>(b.width) * b.height);
  }
}

TEST(largest_resolution_per_sensor) {
  CHECK_EQ(std::string(scout::largestResolution(15)->name), std::string("uxga"));  // OV2640
  CHECK_EQ(std::string(scout::largestResolution(19)->name), std::string("qxga"));  // OV3660
  CHECK_EQ(std::string(scout::largestResolution(24)->name), std::string("qxga"));  // OV5640 (5MP)
  CHECK_EQ(std::string(scout::largestResolution(16)->name), std::string("uxga"));  // FHD max
  CHECK_EQ(std::string(scout::largestResolution(10)->name), std::string("vga"));
  CHECK_EQ(std::string(scout::largestResolution(9)->name), std::string("qvga"));
  CHECK(scout::largestResolution(5) == nullptr);
  CHECK(scout::resolutionAtMost(scout::findResolution("vga"), scout::findResolution("vga")));
  CHECK(!scout::resolutionAtMost(scout::findResolution("svga"), scout::findResolution("vga")));
  CHECK(!scout::resolutionAtMost(nullptr, scout::findResolution("vga")));
}

TEST(camera_model_names) {
  CHECK_EQ(std::string(scout::cameraModelName(0x3660)), std::string("OV3660"));
  CHECK_EQ(std::string(scout::cameraModelName(0x26)), std::string("OV2640"));
  CHECK_EQ(std::string(scout::cameraModelName(0x5640)), std::string("OV5640"));
  CHECK_EQ(std::string(scout::cameraModelName(0x2145)), std::string("GC2145"));
  CHECK(scout::cameraModelName(0x1234) == nullptr);
}

// ---------------------------------------------------------------- reset button

TEST(one_press_of_the_reset_button_is_told_apart_from_the_power_switch) {
  // The button restarts only the ESP32, so the UNO has been running for a while.
  CHECK(scout::resetButtonPressed(1, true, 120, 700, 3000));
  CHECK(scout::resetButtonPressed(2, true, 120, 700, 3000));
  // The power switch restarts both: the UNO is about as young as the ESP32.
  CHECK(!scout::resetButtonPressed(1, true, 0, 700, 3000));
  CHECK(!scout::resetButtonPressed(1, true, 2, 700, 3000));
  // At the margin.
  CHECK(scout::resetButtonPressed(1, true, 4, 1000, 3000));
  CHECK(!scout::resetButtonPressed(1, true, 3, 1000, 3000));
  // No overflow with a UNO that has run for weeks.
  CHECK(scout::resetButtonPressed(1, true, 4000000, 700, 3000));
}

TEST(no_setup_mode_without_the_uno_or_after_restarts_and_crashes) {
  CHECK(!scout::resetButtonPressed(1, false, 120, 700, 3000));  // UNO not answering
  CHECK(!scout::resetButtonPressed(3, true, 120, 700, 3000));   // software restart
  CHECK(!scout::resetButtonPressed(4, true, 120, 700, 3000));   // panic
  CHECK(!scout::resetButtonPressed(6, true, 120, 700, 3000));   // task watchdog
  CHECK(!scout::resetButtonPressed(9, true, 120, 700, 3000));   // brownout
}

// ---------------------------------------------------------------- saved networks

TEST(passphrase_and_ssid_rules) {
  CHECK(scout::validPassphrase(""));
  CHECK(scout::validPassphrase("12345678"));
  CHECK(!scout::validPassphrase("1234567"));
  CHECK(scout::validPassphrase(std::string(63, 'p').c_str()));
  CHECK(!scout::validPassphrase(std::string(65, 'p').c_str()));
  CHECK(scout::validPassphrase(std::string(64, 'a').c_str()));   // a raw hex key
  CHECK(!scout::validPassphrase(std::string(64, 'g').c_str()));  // 64 chars must be hex
  CHECK(!scout::validPassphrase("tab\there!"));
  CHECK(scout::validSsid("home"));
  CHECK(scout::validSsid("Caf\xC3\xA9 5G"));  // UTF-8 is fine
  CHECK(!scout::validSsid(""));
  CHECK(!scout::validSsid(std::string(33, 's').c_str()));
  CHECK(!scout::validSsid("new\nline"));
}

TEST(network_list_add_update_remove) {
  NetworkList list;
  CHECK_EQ(list.add("one", "password1"), NetworkList::kAdded);
  CHECK_EQ(list.add("two", ""), NetworkList::kAdded);
  CHECK_EQ(list.add("one", "password2"), NetworkList::kUpdated);
  CHECK_EQ(list.count(), 2);
  CHECK_EQ(std::string(list.at(0).pass), std::string("password2"));
  CHECK_EQ(list.add("bad", "short"), NetworkList::kInvalid);
  CHECK_EQ(list.add("three", "password3"), NetworkList::kAdded);
  CHECK_EQ(list.add("four", "password4"), NetworkList::kAdded);
  CHECK_EQ(list.add("five", "password5"), NetworkList::kAdded);
  CHECK_EQ(list.add("six", "password6"), NetworkList::kFull);
  CHECK_EQ(list.add("five", "password55"), NetworkList::kUpdated);  // updating works when full
  CHECK(list.remove(1));
  CHECK(!list.remove(4));
  CHECK_EQ(list.count(), 4);
  CHECK_EQ(std::string(list.at(1).ssid), std::string("three"));
  CHECK_EQ(list.find("two"), -1);
  CHECK_EQ(list.find("five"), 3);
}

TEST(connect_order_strongest_first_then_unseen) {
  NetworkList list;
  list.add("attic", "password1");
  list.add("home", "password2");
  list.add("hidden", "password3");
  list.add("garage", "password4");
  scout::ScanEntry scan[] = {
      {"neighbour", -30}, {"garage", -80}, {"home", -60}, {"attic", -70}, {"home", -50}, {nullptr, -10},
  };
  uint8_t order[NetworkList::kMax];
  CHECK_EQ(list.connectOrder(scan, sizeof scan / sizeof scan[0], order), 4);
  CHECK_EQ(order[0], 1);  // home, best -50
  CHECK_EQ(order[1], 0);  // attic -70
  CHECK_EQ(order[2], 3);  // garage -80
  CHECK_EQ(order[3], 2);  // hidden, not seen

  CHECK_EQ(list.connectOrder(nullptr, 0, order), 4);  // no scan: saved order
  for (uint8_t i = 0; i < 4; ++i) CHECK_EQ(order[i], i);
}

// ---------------------------------------------------------------- helpers

TEST(sha256_hex_parsing) {
  uint8_t out[32];
  std::string hex = "E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855";
  CHECK(scout::parseSha256Hex(hex.c_str(), out));
  CHECK_EQ(out[0], 0xE3);
  CHECK_EQ(out[31], 0x55);
  for (char& c : hex) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  uint8_t lower[32];
  CHECK(scout::parseSha256Hex(hex.c_str(), lower));
  CHECK(std::memcmp(out, lower, 32) == 0);
  CHECK(!scout::parseSha256Hex(hex.substr(1).c_str(), out));
  CHECK(!scout::parseSha256Hex((hex.substr(1) + "g").c_str(), out));
  CHECK(!scout::parseSha256Hex(nullptr, out));
}

TEST(mac_and_reset_reason_names) {
  const uint8_t mac[6] = {0x0A, 0xBC, 0xDE, 0xF0, 0x12, 0x34};
  char text[18];
  scout::formatMac(mac, text);
  CHECK_EQ(std::string(text), std::string("0A:BC:DE:F0:12:34"));
  CHECK_EQ(std::string(scout::resetReasonName(1)), std::string("poweron"));
  CHECK_EQ(std::string(scout::resetReasonName(3)), std::string("software"));
  CHECK_EQ(std::string(scout::resetReasonName(9)), std::string("brownout"));
  CHECK_EQ(std::string(scout::resetReasonName(15)), std::string("cpu_lockup"));
  CHECK_EQ(std::string(scout::resetReasonName(99)), std::string("unknown"));
  CHECK_EQ(std::string(scout::resetReasonName(-1)), std::string("unknown"));
}

TEST(light_duty_mapping) {
  CHECK_EQ(scout::lightDuty(0, 300), 0u);
  CHECK_EQ(scout::lightDuty(-5, 300), 0u);
  CHECK_EQ(scout::lightDuty(100, 300), 300u);
  CHECK_EQ(scout::lightDuty(250, 300), 300u);
  CHECK_EQ(scout::lightDuty(50, 300), 150u);
  CHECK_EQ(scout::lightDuty(1, 307), 3u);
  for (int level = 1; level <= 100; ++level) CHECK(scout::lightDuty(level, 307) >= scout::lightDuty(level - 1, 307));
}

TEST(status_led_patterns) {
  using scout::LedMode;
  int on = 0;
  for (uint32_t t = 0; t < 2000; t += 10) on += scout::ledOn(LedMode::kSetup, t) ? 1 : 0;
  CHECK_EQ(on, 20);  // two 100 ms blinks
  CHECK(scout::ledOn(LedMode::kConnecting, 0));
  CHECK(!scout::ledOn(LedMode::kConnecting, 130));
  CHECK(scout::ledOn(LedMode::kConnecting, 250));
  CHECK(scout::ledOn(LedMode::kConnected, 12345));
  CHECK(!scout::ledOn(LedMode::kOff, 0));
  CHECK(scout::ledOn(LedMode::kUpdating, 10));
  CHECK(!scout::ledOn(LedMode::kUpdating, 60));
}

TEST(image_header) {
  uint8_t h[8];
  scout::writeImageHeader(h, false, 1);
  CHECK(std::memcmp(h, "SCIM\x01\x00\x00\x00", 8) == 0);
  scout::writeImageHeader(h, true, 0x12345678);
  CHECK(std::memcmp(h, "SCST\x78\x56\x34\x12", 8) == 0);
}

TEST(json_number_conversion) {
  int32_t v = 0;
  CHECK(scout::numberToInt(60, -100, 100, &v));
  CHECK_EQ(v, 60);
  CHECK(scout::numberToInt(-99.6, -100, 100, &v));
  CHECK_EQ(v, -100);
  CHECK(!scout::numberToInt(100.6, -100, 100, &v));
  CHECK(!scout::numberToInt(std::nan(""), -100, 100, &v));
  CHECK(!scout::numberToInt(std::numeric_limits<double>::infinity(), -100, 100, &v));
  uint32_t id = 0;
  CHECK(scout::numberToId(1, &id));
  CHECK_EQ(id, 1u);
  CHECK(scout::numberToId(4294967295.0, &id));
  CHECK_EQ(id, 4294967295u);
  CHECK(!scout::numberToId(0, &id));
  CHECK(!scout::numberToId(-3, &id));
  CHECK(!scout::numberToId(1.5, &id));
  CHECK(!scout::numberToId(4294967296.0, &id));
}

TEST(html_escaping) {
  char out[64];
  scout::htmlEscape("<b>\"Tom & Jerry's\"</b>", out, sizeof out);
  CHECK_EQ(std::string(out), std::string("&lt;b&gt;&quot;Tom &amp; Jerry&#39;s&quot;&lt;/b&gt;"));
  char small[8];
  CHECK_EQ(scout::htmlEscape("a&b&c", small, sizeof small), 7u);  // the second "&amp;" doesn't fit; stops cleanly
  CHECK_EQ(std::string(small), std::string("a&amp;b"));
  char tiny[4];
  scout::htmlEscape("&&&", tiny, sizeof tiny);
  CHECK_EQ(std::string(tiny), std::string(""));
}

TEST(key_and_password_rules) {
  CHECK(scout::validCarKey("abcDEF-123_.~"));
  CHECK(!scout::validCarKey(""));
  CHECK(!scout::validCarKey("has space"));
  CHECK(!scout::validCarKey("line\nbreak"));
  CHECK(!scout::validCarKey(std::string(129, 'k').c_str()));
  CHECK(scout::validSetupPassword("scoutsetup"));
  CHECK(scout::validSetupPassword("with spaces ok"));
  CHECK(!scout::validSetupPassword("short"));
  CHECK(!scout::validSetupPassword(std::string(64, 'p').c_str()));
}

TEST(percentages) {
  CHECK_EQ(scout::percentOf(0, 100), 0);
  CHECK_EQ(scout::percentOf(50, 200), 25);
  CHECK_EQ(scout::percentOf(1999999, 2000000), 99);
  CHECK_EQ(scout::percentOf(3000000, 2000000), 100);
  CHECK_EQ(scout::percentOf(5, 0), 0);
}
