#include "json_out.h"

#include <stdio.h>

namespace scout {

namespace {

// Length of the valid UTF-8 sequence starting at s, or 0 if it isn't one.
size_t utf8Length(const unsigned char* s) {
  unsigned char c = s[0];
  if (c < 0x80) return 1;
  size_t n;
  uint32_t cp;
  if (c >= 0xC2 && c <= 0xDF) {
    n = 2;
    cp = c & 0x1F;
  } else if (c >= 0xE0 && c <= 0xEF) {
    n = 3;
    cp = c & 0x0F;
  } else if (c >= 0xF0 && c <= 0xF4) {
    n = 4;
    cp = c & 0x07;
  } else {
    return 0;
  }
  for (size_t i = 1; i < n; ++i) {
    if ((s[i] & 0xC0) != 0x80) return 0;  // also stops at the NUL
    cp = (cp << 6) | (s[i] & 0x3F);
  }
  if (n == 3 && (cp < 0x800 || (cp >= 0xD800 && cp <= 0xDFFF))) return 0;  // overlong, surrogate
  if (n == 4 && (cp < 0x10000 || cp > 0x10FFFF)) return 0;
  return n;
}

}  // namespace

JsonOut::JsonOut(char* buf, size_t cap) : buf_(buf), cap_(cap), len_(0), ok_(cap >= 3), first_(true), done_(false) {
  if (cap_ > 0) buf_[0] = '\0';
  put('{');
}

void JsonOut::put(char c) {
  if (!ok_) return;
  if (len_ + 2 > cap_) {  // keep room for the NUL
    ok_ = false;
    return;
  }
  buf_[len_++] = c;
  buf_[len_] = '\0';
}

void JsonOut::raw(const char* text) {
  while (*text) put(*text++);
}

void JsonOut::quoted(const char* text) {
  put('"');
  const unsigned char* s = reinterpret_cast<const unsigned char*>(text);
  while (*s && ok_) {
    unsigned char c = *s;
    if (c == '"' || c == '\\') {
      put('\\');
      put(static_cast<char>(c));
      ++s;
    } else if (c == '\n') {
      raw("\\n");
      ++s;
    } else if (c == '\r') {
      raw("\\r");
      ++s;
    } else if (c == '\t') {
      raw("\\t");
      ++s;
    } else if (c < 0x20 || c == 0x7F) {
      char esc[8];
      snprintf(esc, sizeof esc, "\\u%04x", c);
      raw(esc);
      ++s;
    } else {
      size_t n = utf8Length(s);
      if (n == 0) {
        raw("\\ufffd");
        ++s;
      } else {
        for (size_t i = 0; i < n; ++i) put(static_cast<char>(s[i]));
        s += n;
      }
    }
  }
  put('"');
}

void JsonOut::key(const char* name) {
  if (done_) {
    ok_ = false;
    return;
  }
  if (!first_) put(',');
  first_ = false;
  quoted(name);
  put(':');
}

JsonOut& JsonOut::str(const char* name, const char* value) {
  key(name);
  if (value == nullptr) {
    raw("null");
  } else {
    quoted(value);
  }
  return *this;
}

JsonOut& JsonOut::num(const char* name, int64_t value) {
  key(name);
  char text[24];
  snprintf(text, sizeof text, "%lld", static_cast<long long>(value));
  raw(text);
  return *this;
}

JsonOut& JsonOut::boolean(const char* name, bool value) {
  key(name);
  raw(value ? "true" : "false");
  return *this;
}

JsonOut& JsonOut::null(const char* name) {
  key(name);
  raw("null");
  return *this;
}

JsonOut& JsonOut::ints(const char* name, const int32_t* values, size_t count) {
  key(name);
  put('[');
  for (size_t i = 0; i < count; ++i) {
    if (i > 0) put(',');
    char text[16];
    snprintf(text, sizeof text, "%ld", static_cast<long>(values[i]));
    raw(text);
  }
  put(']');
  return *this;
}

bool JsonOut::finish() {
  if (!done_) {
    put('}');
    done_ = true;
  }
  return ok_;
}

}  // namespace scout
