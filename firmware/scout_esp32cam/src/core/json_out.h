// Writes one flat JSON object into a fixed buffer, for the messages the car sends.
//
//   JsonOut j(buf, sizeof buf);
//   j.str("t", "ack").num("id", 7).boolean("moving", false);
//   if (j.finish()) send(j.text(), j.size());
//
// Strings are escaped; bytes that aren't valid UTF-8 (a WiFi name can hold anything)
// become U+FFFD, so the output is always valid JSON. If the buffer runs out, finish()
// returns false and nothing should be sent.

#ifndef SCOUT_CORE_JSON_OUT_H
#define SCOUT_CORE_JSON_OUT_H

#include <stddef.h>
#include <stdint.h>

namespace scout {

class JsonOut {
 public:
  JsonOut(char* buf, size_t cap);

  JsonOut& str(const char* key, const char* value);  // nullptr -> null
  JsonOut& num(const char* key, int64_t value);
  JsonOut& boolean(const char* key, bool value);
  JsonOut& null(const char* key);
  JsonOut& ints(const char* key, const int32_t* values, size_t count);  // [1,2,3]

  // Closes the object. False if anything didn't fit.
  bool finish();

  const char* text() const { return buf_; }
  size_t size() const { return len_; }
  bool ok() const { return ok_; }

 private:
  void raw(const char* text);
  void put(char c);
  void key(const char* name);
  void quoted(const char* text);

  char* buf_;
  size_t cap_;
  size_t len_;
  bool ok_;
  bool first_;
  bool done_;
};

}  // namespace scout

#endif  // SCOUT_CORE_JSON_OUT_H
