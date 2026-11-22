// WebSocket framing, SHA-1 and base64, checked against the RFC examples.

#include <cstring>
#include <string>
#include <vector>

#include "sha1.h"
#include "testing.h"
#include "ws_codec.h"

using scout::ws::FrameParser;

namespace {

std::string hex(const uint8_t* data, size_t len) {
  static const char digits[] = "0123456789abcdef";
  std::string out;
  for (size_t i = 0; i < len; ++i) {
    out += digits[data[i] >> 4];
    out += digits[data[i] & 0x0F];
  }
  return out;
}

std::string sha1Hex(const std::string& text) {
  uint8_t digest[20];
  scout::sha1(reinterpret_cast<const uint8_t*>(text.data()), text.size(), digest);
  return hex(digest, sizeof digest);
}

std::string b64(const std::string& text) {
  char out[64];
  size_t n = scout::base64Encode(reinterpret_cast<const uint8_t*>(text.data()), text.size(), out, sizeof out);
  return std::string(out, n);
}

std::vector<uint8_t> bytes(std::initializer_list<int> list) {
  std::vector<uint8_t> out;
  for (int b : list) out.push_back(static_cast<uint8_t>(b));
  return out;
}

void append(std::vector<uint8_t>& out, const std::string& text) { out.insert(out.end(), text.begin(), text.end()); }

// An unmasked frame as a server sends it.
std::vector<uint8_t> serverFrame(uint8_t first, const std::string& payload) {
  std::vector<uint8_t> out;
  out.push_back(first);
  size_t n = payload.size();
  if (n < 126) {
    out.push_back(static_cast<uint8_t>(n));
  } else if (n <= 0xFFFF) {
    out.push_back(126);
    out.push_back(static_cast<uint8_t>(n >> 8));
    out.push_back(static_cast<uint8_t>(n));
  } else {
    out.push_back(127);
    for (int shift = 56; shift >= 0; shift -= 8) out.push_back(static_cast<uint8_t>(static_cast<uint64_t>(n) >> shift));
  }
  append(out, payload);
  return out;
}

struct Harness {
  uint8_t buffer[4097];
  FrameParser parser;
  Harness() : parser(buffer, 4096) {}

  // Feeds everything, collecting events (and the payload text of each).
  std::vector<FrameParser::Event> run(const std::vector<uint8_t>& in, std::vector<std::string>* payloads = nullptr,
                                      bool byteByByte = false) {
    std::vector<FrameParser::Event> events;
    size_t pos = 0;
    while (pos < in.size()) {
      size_t len = byteByByte ? 1 : in.size() - pos;
      size_t used = 0;
      FrameParser::Event e = parser.feed(in.data() + pos, len, &used);
      pos += used;
      if (e != FrameParser::kNone) {
        events.push_back(e);
        if (payloads) payloads->push_back(std::string(reinterpret_cast<const char*>(parser.data()), parser.size()));
        if (e == FrameParser::kError) break;
      }
      if (used == 0 && e == FrameParser::kNone) break;
    }
    return events;
  }
};

}  // namespace

TEST(sha1_fips_vectors) {
  CHECK_EQ(sha1Hex(""), std::string("da39a3ee5e6b4b0d3255bfef95601890afd80709"));
  CHECK_EQ(sha1Hex("abc"), std::string("a9993e364706816aba3e25717850c26c9cd0d89d"));
  CHECK_EQ(sha1Hex("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"),
           std::string("84983e441c3bd26ebaae4aa1f95129e5e54670f1"));
  CHECK_EQ(sha1Hex(std::string(1000000, 'a')), std::string("34aa973cd4c4daa4f61eeb2bdbad27316534016f"));
}

TEST(sha1_incremental_matches_one_shot) {
  std::string text;
  for (int i = 0; i < 300; ++i) text += static_cast<char>('a' + i % 26);
  scout::Sha1 h;
  for (size_t i = 0; i < text.size(); i += 7) {
    size_t n = text.size() - i < 7 ? text.size() - i : 7;
    h.update(reinterpret_cast<const uint8_t*>(text.data()) + i, n);
  }
  uint8_t digest[20];
  h.finish(digest);
  CHECK_EQ(hex(digest, 20), sha1Hex(text));
}

TEST(base64_rfc4648_vectors) {
  CHECK_EQ(b64(""), std::string(""));
  CHECK_EQ(b64("f"), std::string("Zg=="));
  CHECK_EQ(b64("fo"), std::string("Zm8="));
  CHECK_EQ(b64("foo"), std::string("Zm9v"));
  CHECK_EQ(b64("foob"), std::string("Zm9vYg=="));
  CHECK_EQ(b64("fooba"), std::string("Zm9vYmE="));
  CHECK_EQ(b64("foobar"), std::string("Zm9vYmFy"));
}

TEST(base64_refuses_small_buffer) {
  char out[5];
  const uint8_t data[] = {1, 2, 3, 4};
  CHECK_EQ(scout::base64Encode(data, 4, out, sizeof out), 0u);  // needs 9
  CHECK_EQ(scout::base64Encode(data, 3, out, sizeof out), 4u);
}

TEST(header_sizes_and_length_encoding) {
  const uint8_t mask[4] = {1, 2, 3, 4};
  uint8_t h[scout::ws::kMaxHeader];
  CHECK_EQ(scout::ws::encodeHeader(h, scout::ws::kOpText, true, 0, mask), 6u);
  CHECK_EQ(h[0], 0x81);
  CHECK_EQ(h[1], 0x80);
  CHECK_EQ(scout::ws::encodeHeader(h, scout::ws::kOpText, true, 125, mask), 6u);
  CHECK_EQ(h[1], 0x80 | 125);
  CHECK_EQ(scout::ws::encodeHeader(h, scout::ws::kOpBinary, true, 126, mask), 8u);
  CHECK_EQ(h[0], 0x82);
  CHECK_EQ(h[1], 0x80 | 126);
  CHECK_EQ(h[2], 0x00);
  CHECK_EQ(h[3], 126);
  CHECK_EQ(scout::ws::encodeHeader(h, scout::ws::kOpBinary, true, 65535, mask), 8u);
  CHECK_EQ(h[2], 0xFF);
  CHECK_EQ(h[3], 0xFF);
  CHECK_EQ(scout::ws::encodeHeader(h, scout::ws::kOpBinary, true, 65536, mask), 14u);
  CHECK_EQ(h[1], 0x80 | 127);
  CHECK_EQ(hex(h + 2, 8), std::string("0000000000010000"));
  CHECK_EQ(hex(h + 10, 4), std::string("01020304"));
  CHECK_EQ(scout::ws::encodeHeader(h, scout::ws::kOpClose, false, 2, mask), 6u);
  CHECK_EQ(h[0], 0x08);
}

TEST(rfc6455_masked_hello) {
  // RFC 6455 5.7: a single-frame masked text message "Hello".
  const uint8_t mask[4] = {0x37, 0xfa, 0x21, 0x3d};
  uint8_t frame[32];
  size_t n = scout::ws::encodeHeader(frame, scout::ws::kOpText, true, 5, mask);
  scout::ws::maskCopy(frame + n, reinterpret_cast<const uint8_t*>("Hello"), 5, mask, 0);
  CHECK_EQ(hex(frame, n + 5), std::string("818537fa213d7f9f4d5158"));
}

TEST(mask_in_pieces_matches_mask_at_once) {
  const uint8_t mask[4] = {0xA1, 0x07, 0x5C, 0xE3};
  std::vector<uint8_t> src(1000);
  for (size_t i = 0; i < src.size(); ++i) src[i] = static_cast<uint8_t>(i * 7 + 3);
  std::vector<uint8_t> whole = src;
  scout::ws::applyMask(whole.data(), whole.size(), mask, 0);
  std::vector<uint8_t> pieces(src.size());
  size_t sizes[] = {1, 2, 3, 5, 8, 13, 100, 333};
  size_t pos = 0, k = 0;
  while (pos < src.size()) {
    size_t n = sizes[k++ % 8];
    if (n > src.size() - pos) n = src.size() - pos;
    scout::ws::maskCopy(pieces.data() + pos, src.data() + pos, n, mask, pos);
    pos += n;
  }
  CHECK(pieces == whole);
  scout::ws::applyMask(whole.data(), whole.size(), mask, 0);
  CHECK(whole == src);
}

TEST(parses_rfc_examples) {
  Harness h;
  std::vector<std::string> payloads;
  // Unmasked "Hello", fragmented "Hel"+"lo", ping "Hello", 256-byte binary.
  std::vector<uint8_t> in = bytes({0x81, 0x05, 'H', 'e', 'l', 'l', 'o'});
  std::vector<uint8_t> frag = bytes({0x01, 0x03, 'H', 'e', 'l', 0x80, 0x02, 'l', 'o'});
  in.insert(in.end(), frag.begin(), frag.end());
  std::vector<uint8_t> ping = bytes({0x89, 0x05, 'H', 'e', 'l', 'l', 'o'});
  in.insert(in.end(), ping.begin(), ping.end());
  std::vector<uint8_t> bin = serverFrame(0x82, std::string(256, 'x'));
  in.insert(in.end(), bin.begin(), bin.end());

  std::vector<FrameParser::Event> events = h.run(in, &payloads);
  CHECK_EQ(events.size(), 4u);
  if (events.size() == 4) {
    CHECK_EQ(events[0], FrameParser::kText);
    CHECK_EQ(payloads[0], std::string("Hello"));
    CHECK_EQ(events[1], FrameParser::kText);
    CHECK_EQ(payloads[1], std::string("Hello"));
    CHECK_EQ(events[2], FrameParser::kPing);
    CHECK_EQ(payloads[2], std::string("Hello"));
    CHECK_EQ(events[3], FrameParser::kBinary);
    CHECK_EQ(payloads[3].size(), 256u);
  }
}

TEST(byte_by_byte_matches_bulk) {
  std::vector<uint8_t> in = serverFrame(0x81, "{\"t\":\"ping\",\"id\":7}");
  std::vector<uint8_t> more = serverFrame(0x82, std::string(300, 'z'));
  in.insert(in.end(), more.begin(), more.end());
  std::vector<uint8_t> pong = bytes({0x8A, 0x00});
  in.insert(in.end(), pong.begin(), pong.end());
  Harness a, b;
  std::vector<std::string> pa, pb;
  std::vector<FrameParser::Event> ea = a.run(in, &pa, false);
  std::vector<FrameParser::Event> eb = b.run(in, &pb, true);
  CHECK(ea == eb);
  CHECK(pa == pb);
  CHECK_EQ(ea.size(), 3u);
}

TEST(text_message_is_nul_terminated) {
  Harness h;
  std::vector<uint8_t> in = serverFrame(0x81, "abc");
  size_t used = 0;
  CHECK_EQ(h.parser.feed(in.data(), in.size(), &used), FrameParser::kText);
  CHECK_EQ(used, in.size());
  CHECK_EQ(std::strlen(reinterpret_cast<const char*>(h.parser.data())), 3u);
}

TEST(empty_frames) {
  Harness h;
  std::vector<std::string> payloads;
  std::vector<uint8_t> in = bytes({0x81, 0x00, 0x89, 0x00, 0x88, 0x00});
  std::vector<FrameParser::Event> events = h.run(in, &payloads);
  CHECK_EQ(events.size(), 3u);
  if (events.size() == 3) {
    CHECK_EQ(events[0], FrameParser::kText);
    CHECK_EQ(payloads[0], std::string(""));
    CHECK_EQ(events[1], FrameParser::kPing);
    CHECK_EQ(events[2], FrameParser::kClose);
    CHECK_EQ(h.parser.closeCode(), scout::ws::kCloseNoStatus);
  }
}

TEST(close_code_and_reason) {
  Harness h;
  std::vector<uint8_t> in = bytes({0x88, 0x0A, 0x11, 0x39});  // 4409 + "replaced"
  append(in, "replaced");
  std::vector<std::string> payloads;
  std::vector<FrameParser::Event> events = h.run(in, &payloads);
  CHECK_EQ(events.size(), 1u);
  CHECK_EQ(events[0], FrameParser::kClose);
  CHECK_EQ(h.parser.closeCode(), 4409);
  CHECK_EQ(payloads[0].substr(2), std::string("replaced"));
}

TEST(control_frame_inside_fragmented_message) {
  Harness h;
  std::vector<std::string> payloads;
  std::vector<uint8_t> in = bytes({0x01, 0x02, '{', '"', 0x89, 0x01, 'p', 0x00, 0x02, 't', '"', 0x80, 0x01, '}'});
  std::vector<FrameParser::Event> events = h.run(in, &payloads);
  CHECK_EQ(events.size(), 2u);
  if (events.size() == 2) {
    CHECK_EQ(events[0], FrameParser::kPing);
    CHECK_EQ(payloads[0], std::string("p"));
    CHECK_EQ(events[1], FrameParser::kText);
    CHECK_EQ(payloads[1], std::string("{\"t\"}"));
  }
}

TEST(protocol_errors) {
  struct Case {
    std::vector<uint8_t> in;
    const char* what;
  };
  std::vector<Case> cases = {
      {bytes({0x81, 0x85, 1, 2, 3, 4, 'a', 'b', 'c', 'd', 'e'}), "masked server frame"},
      {bytes({0xC1, 0x01, 'a'}), "RSV1 set"},
      {bytes({0x83, 0x00}), "reserved data opcode"},
      {bytes({0x8B, 0x00}), "reserved control opcode"},
      {bytes({0x09, 0x00}), "fragmented ping"},
      {bytes({0x80, 0x01, 'a'}), "continuation without start"},
      {bytes({0x01, 0x01, 'a', 0x81, 0x01, 'b'}), "new message during a fragmented one"},
      {bytes({0x88, 0x01, 0x03}), "close with one-byte payload"},
      {bytes({0x82, 0x7F, 0x80, 0, 0, 0, 0, 0, 0, 0}), "64-bit length with the top bit set"},
  };
  std::vector<uint8_t> bigPing = bytes({0x89, 0x7E, 0x00, 0x7E});
  bigPing.resize(bigPing.size() + 126, 'x');
  cases.push_back({bigPing, "ping longer than 125 bytes"});
  for (const Case& c : cases) {
    Harness h;
    std::vector<FrameParser::Event> events = h.run(c.in);
    bool failed = !events.empty() && events.back() == FrameParser::kError;
    if (!failed) testing::fail(__FILE__, __LINE__, std::string("not rejected: ") + c.what);
    CHECK_EQ(h.parser.error(), FrameParser::kProtocol);
    CHECK_EQ(h.parser.errorCloseCode(), scout::ws::kCloseProtocolError);
  }
}

TEST(too_big_messages) {
  {
    Harness h;  // one frame over the 4096-byte cap
    std::vector<FrameParser::Event> events = h.run(serverFrame(0x82, std::string(4097, 'a')));
    CHECK(!events.empty() && events.back() == FrameParser::kError);
    CHECK_EQ(h.parser.error(), FrameParser::kTooBig);
    CHECK_EQ(h.parser.errorCloseCode(), scout::ws::kCloseTooBig);
  }
  {
    Harness h;  // a 64 KiB frame header is refused before any payload arrives
    std::vector<uint8_t> in = bytes({0x82, 0x7F, 0, 0, 0, 0, 0, 1, 0, 0});
    std::vector<FrameParser::Event> events = h.run(in);
    CHECK(!events.empty() && events.back() == FrameParser::kError);
    CHECK_EQ(h.parser.error(), FrameParser::kTooBig);
  }
  {
    Harness h;  // fragments that only add up to too much
    std::vector<uint8_t> in = serverFrame(0x01, std::string(3000, 'a'));
    std::vector<uint8_t> second = serverFrame(0x80, std::string(1200, 'b'));
    in.insert(in.end(), second.begin(), second.end());
    std::vector<FrameParser::Event> events = h.run(in);
    CHECK(!events.empty() && events.back() == FrameParser::kError);
    CHECK_EQ(h.parser.error(), FrameParser::kTooBig);
  }
  {
    Harness h;  // exactly at the cap is fine
    std::vector<std::string> payloads;
    std::vector<FrameParser::Event> events = h.run(serverFrame(0x81, std::string(4096, 'c')), &payloads);
    CHECK_EQ(events.size(), 1u);
    CHECK_EQ(events[0], FrameParser::kText);
    CHECK_EQ(payloads[0].size(), 4096u);
  }
}

TEST(error_is_sticky_until_reset) {
  Harness h;
  std::vector<uint8_t> bad = bytes({0xC1, 0x00});
  size_t used = 0;
  CHECK_EQ(h.parser.feed(bad.data(), bad.size(), &used), FrameParser::kError);
  std::vector<uint8_t> good = serverFrame(0x81, "ok");
  CHECK_EQ(h.parser.feed(good.data(), good.size(), &used), FrameParser::kError);
  CHECK_EQ(used, 0u);
  h.parser.reset();
  CHECK_EQ(h.parser.feed(good.data(), good.size(), &used), FrameParser::kText);
}

TEST(stops_after_each_event) {
  Harness h;
  std::vector<uint8_t> in = serverFrame(0x81, "one");
  std::vector<uint8_t> two = serverFrame(0x81, "two");
  in.insert(in.end(), two.begin(), two.end());
  size_t used = 0;
  CHECK_EQ(h.parser.feed(in.data(), in.size(), &used), FrameParser::kText);
  CHECK_EQ(used, 5u);
  CHECK_EQ(std::string(reinterpret_cast<const char*>(h.parser.data()), h.parser.size()), std::string("one"));
  size_t used2 = 0;
  CHECK_EQ(h.parser.feed(in.data() + used, in.size() - used, &used2), FrameParser::kText);
  CHECK_EQ(std::string(reinterpret_cast<const char*>(h.parser.data()), h.parser.size()), std::string("two"));
}
