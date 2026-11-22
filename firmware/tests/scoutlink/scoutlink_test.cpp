#include <cctype>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

#include "ScoutLink.h"
#include "testing.h"

using scoutlink::Builder;
using scoutlink::Parser;

namespace {

std::vector<std::string> splitOn(const std::string& text, char sep) {
  std::vector<std::string> parts;
  std::string current;
  for (char c : text) {
    if (c == sep) {
      parts.push_back(current);
      current.clear();
    } else {
      current += c;
    }
  }
  parts.push_back(current);
  return parts;
}

std::vector<std::vector<std::string>> vectors(const std::string& kind) {
  std::ifstream in(testing::repoPath("protocol/uart_vectors.txt"));
  CHECK(in.good());
  std::vector<std::vector<std::string>> rows;
  std::string line;
  while (std::getline(in, line)) {
    if (line.empty() || line[0] == '#') continue;
    std::vector<std::string> fields = splitOn(line, '\t');
    if (fields[0] == kind) rows.push_back(fields);
  }
  CHECK(!rows.empty());
  return rows;
}

std::string fromHex(const std::string& hex) {
  std::string out;
  for (size_t i = 0; i + 1 < hex.size(); i += 2) {
    out += static_cast<char>(std::stoi(hex.substr(i, 2), nullptr, 16));
  }
  return out;
}

// Feeds bytes and returns how many frames completed.
int feedAll(Parser& parser, const std::string& bytes) {
  int frames = 0;
  for (char c : bytes) {
    if (parser.feed(static_cast<uint8_t>(c))) ++frames;
  }
  return frames;
}

}  // namespace

TEST(crc_matches_vectors) {
  for (const auto& row : vectors("crc")) {
    const std::string& text = row[1];
    int expected = std::stoi(row[2], nullptr, 16);
    CHECK_EQ(static_cast<int>(scoutlink::crc8(reinterpret_cast<const uint8_t*>(text.data()), text.size())),
             expected);
  }
}

TEST(valid_frames_parse) {
  for (const auto& row : vectors("ok")) {
    Parser parser;
    std::string frame = row[1] + "\n";
    bool done = false;
    for (char c : frame) {
      if (parser.feed(static_cast<uint8_t>(c))) {
        done = true;
        CHECK_EQ(std::string(parser.type()), row[2]);
        CHECK_EQ(static_cast<int>(parser.seq()), std::stoi(row[3]));
        std::vector<std::string> args = row[4].empty() ? std::vector<std::string>() : splitOn(row[4], ',');
        CHECK_EQ(static_cast<size_t>(parser.argCount()), args.size());
        for (size_t i = 0; i < args.size() && i < parser.argCount(); ++i) {
          CHECK_EQ(std::string(parser.arg(static_cast<uint8_t>(i))), args[i]);
        }
      }
    }
    if (!done) testing::fail(__FILE__, __LINE__, "frame did not parse: " + row[1]);
    CHECK_EQ(parser.rxErrors(), 0u);
  }
}

TEST(invalid_frames_are_dropped_and_counted) {
  for (const auto& row : vectors("bad")) {
    Parser parser;
    int frames = feedAll(parser, fromHex(row[1]) + "\n");
    if (frames != 0) testing::fail(__FILE__, __LINE__, "accepted a bad frame: " + row[2]);
    if (parser.rxErrors() != 1u) testing::fail(__FILE__, __LINE__, "error not counted once: " + row[2]);
  }
}

TEST(builder_reproduces_vector_frames) {
  for (const auto& row : vectors("ok")) {
    const std::string crcText = row[1].substr(row[1].size() - 2);
    if (crcText != "" && (std::islower(static_cast<unsigned char>(crcText[0])) ||
                          std::islower(static_cast<unsigned char>(crcText[1])))) {
      continue;  // the lowercase-CRC row only tests the parser; the builder writes uppercase
    }
    Builder builder(row[2].c_str(), static_cast<uint16_t>(std::stoi(row[3])));
    if (!row[4].empty()) {
      for (const std::string& arg : splitOn(row[4], ',')) {
        int32_t value;
        if (scoutlink::parseInt(arg.c_str(), &value)) {
          builder.add(value);
        } else {
          builder.add(arg.c_str());
        }
      }
    }
    char out[scoutlink::kMaxFrame];
    size_t n = builder.finish(out, sizeof out);
    CHECK_EQ(std::string(out, n), row[1] + "\n");
  }
}

TEST(int_parsing_matches_vectors) {
  for (const auto& row : vectors("int")) {
    int32_t value = 12345;
    bool ok = scoutlink::parseInt(row[1].c_str(), &value);
    if (row[2] == "bad") {
      if (ok) testing::fail(__FILE__, __LINE__, "accepted bad int '" + row[1] + "'");
    } else {
      CHECK(ok);
      CHECK_EQ(static_cast<long long>(value), std::stoll(row[2]));
    }
  }
}

TEST(skips_noise_between_frames) {
  Parser parser;
  std::string noise = "ets Jun  8 2016 00:22:57\r\nrst:0x1 (POWERON_RESET),boot:0x13\r\n\x01\xff";
  int frames = feedAll(parser, noise + "$PING,1*51\n" + noise + "$STOP,7*5C\r\n");
  CHECK_EQ(frames, 2);
  CHECK(parser.is("STOP"));
  CHECK_EQ(parser.rxErrors(), 0u);
}

TEST(dollar_inside_frame_restarts_it) {
  Parser parser;
  CHECK_EQ(feedAll(parser, "$MOVE,1,5$PING,1*51\n"), 1);
  CHECK(parser.is("PING"));
  CHECK_EQ(parser.rxErrors(), 1u);
}

TEST(builder_rejects_bad_input) {
  char out[scoutlink::kMaxFrame];
  CHECK_EQ(Builder("ping", 1).finish(out, sizeof out), 0u);
  CHECK_EQ(Builder("ABCDEFGHI", 1).finish(out, sizeof out), 0u);
  CHECK_EQ(Builder("ACK", 1).add("a,b").finish(out, sizeof out), 0u);
  CHECK_EQ(Builder("ACK", 1).add("").finish(out, sizeof out), 0u);
  CHECK_EQ(Builder("ACK", 1).add("x*").finish(out, sizeof out), 0u);
  Builder nine("T", 1);
  for (int i = 0; i < 9; ++i) nine.add(i);
  CHECK_EQ(nine.finish(out, sizeof out), 0u);
  Builder eight("T", 1);
  for (int i = 0; i < 8; ++i) eight.add(i);
  CHECK(eight.finish(out, sizeof out) > 0u);
  Builder longBody("TEL", 0);
  for (int i = 0; i < 8; ++i) longBody.add(-2147483647 - 1);
  CHECK_EQ(longBody.finish(out, sizeof out), 0u);
  CHECK_EQ(Builder("ACK", 1).finish(out, 8), 0u);  // buffer too small
}

TEST(builder_writes_extreme_ints) {
  char out[scoutlink::kMaxFrame];
  size_t n = Builder("ACK", 65535).add(-2147483647 - 1).add(2147483647).finish(out, sizeof out);
  CHECK(n > 0u);
  Parser parser;
  CHECK_EQ(feedAll(parser, std::string(out, n)), 1);
  int32_t a = 0;
  int32_t b = 0;
  CHECK(parser.argInt(0, &a));
  CHECK(parser.argInt(1, &b));
  CHECK_EQ(a, -2147483647 - 1);
  CHECK_EQ(b, 2147483647);
  CHECK_EQ(static_cast<int>(parser.seq()), 65535);
}

TEST(arg_accessors_are_safe_out_of_range) {
  Parser parser;
  CHECK_EQ(feedAll(parser, "$ACK,5*B8\n"), 1);
  int32_t value = 7;
  CHECK_EQ(parser.argCount(), 0);
  CHECK_EQ(std::string(parser.arg(0)), std::string(""));
  CHECK(!parser.argInt(0, &value));
  CHECK(!parser.argIs(0, ""));
  CHECK_EQ(value, 7);
}
