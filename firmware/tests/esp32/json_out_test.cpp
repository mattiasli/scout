// The JSON writer used for every message the car sends.

#include <string>

#include "json_out.h"
#include "testing.h"

using scout::JsonOut;

TEST(json_writes_flat_object) {
  char buf[128];
  JsonOut j(buf, sizeof buf);
  int32_t line[3] = {540, 612, -1};
  j.str("t", "tel").num("cm", 82).null("tilt").boolean("moving", true).boolean("uno", false).ints("line", line, 3);
  CHECK(j.finish());
  CHECK_EQ(std::string(j.text()),
           std::string("{\"t\":\"tel\",\"cm\":82,\"tilt\":null,\"moving\":true,\"uno\":false,\"line\":[540,612,-1]}"));
  CHECK_EQ(j.size(), std::string(j.text()).size());
}

TEST(json_empty_object_and_null_string) {
  char buf[16];
  JsonOut empty(buf, sizeof buf);
  CHECK(empty.finish());
  CHECK_EQ(std::string(buf), std::string("{}"));

  JsonOut j(buf, sizeof buf);
  j.str("a", nullptr);
  CHECK(j.finish());
  CHECK_EQ(std::string(buf), std::string("{\"a\":null}"));
}

TEST(json_large_numbers) {
  char buf[64];
  JsonOut j(buf, sizeof buf);
  j.num("id", 4294967295LL).num("neg", -2147483648LL);
  CHECK(j.finish());
  CHECK_EQ(std::string(buf), std::string("{\"id\":4294967295,\"neg\":-2147483648}"));
}

TEST(json_escapes_strings) {
  char buf[128];
  JsonOut j(buf, sizeof buf);
  j.str("s", "a\"b\\c\nd\re\tf\x01g\x7f");
  CHECK(j.finish());
  CHECK_EQ(std::string(buf), std::string("{\"s\":\"a\\\"b\\\\c\\nd\\re\\tf\\u0001g\\u007f\"}"));
}

TEST(json_keeps_valid_utf8) {
  char buf[64];
  JsonOut j(buf, sizeof buf);
  j.str("ssid", "Caf\xc3\xa9 \xe2\x82\xac \xf0\x9f\x9a\x97");  // "Café € 🚗"
  CHECK(j.finish());
  CHECK_EQ(std::string(buf), std::string("{\"ssid\":\"Caf\xc3\xa9 \xe2\x82\xac \xf0\x9f\x9a\x97\"}"));
}

TEST(json_replaces_invalid_utf8) {
  struct Case {
    const char* in;
    const char* out;
  } cases[] = {
      {"a\xff" "b", "a\\ufffdb"},                       // never valid
      {"\xc3", "\\ufffd"},                              // cut short by the end
      {"\xc3" "A", "\\ufffdA"},                         // bad continuation
      {"\xc0\xaf", "\\ufffd\\ufffd"},                   // overlong '/'
      {"\xe0\x80\xaf", "\\ufffd\\ufffd\\ufffd"},        // overlong 3-byte
      {"\xed\xa0\x80", "\\ufffd\\ufffd\\ufffd"},        // UTF-16 surrogate
      {"\xf4\x90\x80\x80", "\\ufffd\\ufffd\\ufffd\\ufffd"},  // above U+10FFFF
      {"\x80", "\\ufffd"},                              // lone continuation
  };
  for (const Case& c : cases) {
    char buf[96];
    JsonOut j(buf, sizeof buf);
    j.str("s", c.in);
    CHECK(j.finish());
    CHECK_EQ(std::string(buf), std::string("{\"s\":\"") + c.out + "\"}");
  }
}

TEST(json_reports_overflow) {
  char buf[12];
  JsonOut j(buf, sizeof buf);
  j.str("t", "hello");  // {"t":"hello"} is 13 characters + NUL
  CHECK(!j.finish());
  CHECK(std::string(buf).size() < sizeof buf);

  char exact[14];
  JsonOut k(exact, sizeof exact);
  k.str("t", "hello");
  CHECK(k.finish());
  CHECK_EQ(std::string(exact), std::string("{\"t\":\"hello\"}"));
}

TEST(json_no_fields_after_finish) {
  char buf[32];
  JsonOut j(buf, sizeof buf);
  CHECK(j.finish());
  j.num("late", 1);
  CHECK(!j.ok());
  CHECK(!j.finish());
}
