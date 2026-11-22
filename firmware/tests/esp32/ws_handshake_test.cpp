// The WebSocket upgrade request/response and the server address parser.

#include <cstring>
#include <string>

#include "testing.h"
#include "ws_handshake.h"

using scout::ws::ResponseParser;
using scout::ws::ServerUrl;

namespace {

const char kRfcKey[] = "dGhlIHNhbXBsZSBub25jZQ==";
const char kRfcAccept[] = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=";

// Feeds text until the parser finishes; returns the result and how many bytes it used.
ResponseParser::Result feedText(ResponseParser& p, const std::string& text, size_t* used) {
  ResponseParser::Result r = ResponseParser::kMore;
  size_t i = 0;
  while (i < text.size() && r == ResponseParser::kMore) r = p.feed(static_cast<uint8_t>(text[i++]));
  *used = i;
  return r;
}

scout::ws::RequestParams params() {
  scout::ws::RequestParams p;
  p.host = "scout.example.com";
  p.port = 443;
  p.secure = true;
  p.path = "/car/ws";
  p.key = kRfcKey;
  p.bearer = "car-secret";
  p.firmware = "0.1.0";
  return p;
}

bool contains(const std::string& text, const std::string& part) { return text.find(part) != std::string::npos; }

}  // namespace

TEST(key_and_accept_match_rfc6455) {
  char key[25];
  CHECK(scout::ws::makeKey(reinterpret_cast<const uint8_t*>("the sample nonce"), key));
  CHECK_EQ(std::string(key), std::string(kRfcKey));
  char accept[29];
  CHECK(scout::ws::acceptFor(kRfcKey, accept));
  CHECK_EQ(std::string(accept), std::string(kRfcAccept));
}

TEST(request_has_the_required_headers) {
  char out[1024];
  size_t n = scout::ws::buildRequest(out, sizeof out, params());
  CHECK(n > 0);
  std::string req(out, n);
  CHECK_EQ(req.find("GET /car/ws HTTP/1.1\r\n"), 0u);
  CHECK(contains(req, "\r\nHost: scout.example.com\r\n"));
  CHECK(contains(req, "\r\nUpgrade: websocket\r\n"));
  CHECK(contains(req, "\r\nConnection: Upgrade\r\n"));
  CHECK(contains(req, "\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"));
  CHECK(contains(req, "\r\nSec-WebSocket-Version: 13\r\n"));
  CHECK(contains(req, "\r\nAuthorization: Bearer car-secret\r\n"));
  CHECK(contains(req, "\r\nX-Scout-Firmware: 0.1.0\r\n"));
  CHECK_EQ(req.substr(req.size() - 4), std::string("\r\n\r\n"));
  CHECK_EQ(std::strlen(out), n);
}

TEST(request_host_port_and_auth) {
  char out[1024];
  scout::ws::RequestParams p = params();
  p.port = 8443;
  std::string req(out, scout::ws::buildRequest(out, sizeof out, p));
  CHECK(contains(req, "\r\nHost: scout.example.com:8443\r\n"));

  p = params();
  p.secure = false;
  p.port = 80;
  p.bearer = "";
  req.assign(out, scout::ws::buildRequest(out, sizeof out, p));
  CHECK(contains(req, "\r\nHost: scout.example.com\r\n"));
  CHECK(!contains(req, "Authorization"));

  p = params();
  p.secure = false;
  p.port = 443;
  req.assign(out, scout::ws::buildRequest(out, sizeof out, p));
  CHECK(contains(req, "\r\nHost: scout.example.com:443\r\n"));
}

TEST(request_refuses_header_injection_and_small_buffers) {
  char out[1024];
  scout::ws::RequestParams p = params();
  p.bearer = "abc\r\nX-Evil: 1";
  CHECK_EQ(scout::ws::buildRequest(out, sizeof out, p), 0u);
  p = params();
  p.bearer = "has space";
  CHECK_EQ(scout::ws::buildRequest(out, sizeof out, p), 0u);
  p = params();
  p.path = "car/ws";
  CHECK_EQ(scout::ws::buildRequest(out, sizeof out, p), 0u);
  p = params();
  CHECK_EQ(scout::ws::buildRequest(out, 64, p), 0u);
}

TEST(accepts_a_valid_upgrade_and_stops_at_the_blank_line) {
  ResponseParser p(kRfcAccept);
  std::string resp =
      "HTTP/1.1 101 Switching Protocols\r\n"
      "upgrade: WebSocket\r\n"
      "Connection: keep-alive, Upgrade\r\n"
      "Sec-WebSocket-Accept:   s3pPLMBiTxaQ9kYGzzhZRbK+xOo=  \r\n"
      "Set-Cookie: " +
      std::string(600, 'c') +
      "\r\n"
      "\r\n";
  std::string frame = "\x81\x02hi";
  size_t used = 0;
  CHECK_EQ(feedText(p, resp + frame, &used), ResponseParser::kDone);
  CHECK_EQ(used, resp.size());
  CHECK_EQ(p.status(), 101);
  CHECK(p.accepted());
  CHECK_EQ(std::string(p.failure()), std::string(""));
}

TEST(tolerates_bare_newlines) {
  ResponseParser p(kRfcAccept);
  size_t used = 0;
  std::string resp =
      "HTTP/1.1 101 OK\nUpgrade: websocket\nConnection: Upgrade\nSec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\n\n";
  CHECK_EQ(feedText(p, resp, &used), ResponseParser::kDone);
  CHECK(p.accepted());
}

TEST(rejects_bad_upgrades) {
  struct Case {
    std::string resp;
    const char* failure;
    int status;
  };
  const std::string good = "Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n";
  Case cases[] = {
      {"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n", "car key refused", 403},
      {"HTTP/1.1 200 OK\r\n\r\n", "server did not upgrade", 200},
      {"HTTP/1.1 101 OK\r\nConnection: Upgrade\r\n" + good + "\r\n", "missing upgrade headers", 101},
      {"HTTP/1.1 101 OK\r\nUpgrade: websocket\r\n" + good + "\r\n", "missing upgrade headers", 101},
      {"HTTP/1.1 101 OK\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n", "missing accept header", 101},
      {"HTTP/1.1 101 OK\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: "
       "S3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n",
       "wrong accept header", 101},
  };
  for (const Case& c : cases) {
    ResponseParser p(kRfcAccept);
    size_t used = 0;
    CHECK_EQ(feedText(p, c.resp, &used), ResponseParser::kDone);
    CHECK(!p.accepted());
    CHECK_EQ(p.status(), c.status);
    CHECK_EQ(std::string(p.failure()), std::string(c.failure));
  }
}

TEST(rejects_garbage_and_endless_headers) {
  {
    ResponseParser p(kRfcAccept);
    size_t used = 0;
    CHECK_EQ(feedText(p, "SSH-2.0-OpenSSH\r\n", &used), ResponseParser::kError);
    CHECK(!p.accepted());
  }
  {
    ResponseParser p(kRfcAccept);
    size_t used = 0;
    CHECK_EQ(feedText(p, "\r\n", &used), ResponseParser::kError);
  }
  {
    ResponseParser p(kRfcAccept);
    std::string resp = "HTTP/1.1 101 OK\r\n";
    while (resp.size() < ResponseParser::kMaxHeaderBytes + 10) resp += "X-Filler: 0123456789\r\n";
    size_t used = 0;
    CHECK_EQ(feedText(p, resp, &used), ResponseParser::kError);
    CHECK_EQ(std::string(p.failure()), std::string("malformed response"));
  }
  {
    ResponseParser p(kRfcAccept);
    size_t used = 0;
    CHECK_EQ(feedText(p, "HTTP/1.1 101 OK\r\nUpgrade: websocket\r\n", &used), ResponseParser::kMore);
    CHECK_EQ(std::string(p.failure()), std::string("incomplete response"));
  }
}

TEST(server_url_forms) {
  ServerUrl u;
  CHECK(scout::ws::parseServerUrl("wss://scout.example.com/car/ws", &u));
  CHECK(u.secure);
  CHECK_EQ(std::string(u.host), std::string("scout.example.com"));
  CHECK_EQ(u.port, 443);
  CHECK_EQ(std::string(u.path), std::string("/car/ws"));

  CHECK(scout::ws::parseServerUrl("  WSS://Scout.Example.com  ", &u));
  CHECK(u.secure);
  CHECK_EQ(std::string(u.host), std::string("Scout.Example.com"));
  CHECK_EQ(std::string(u.path), std::string("/car/ws"));

  CHECK(scout::ws::parseServerUrl("https://scout.example.com/", &u));
  CHECK(u.secure);
  CHECK_EQ(std::string(u.path), std::string("/car/ws"));

  CHECK(scout::ws::parseServerUrl("ws://192.168.1.20:8000/car/ws?dev=1", &u));
  CHECK(!u.secure);
  CHECK_EQ(std::string(u.host), std::string("192.168.1.20"));
  CHECK_EQ(u.port, 8000);
  CHECK_EQ(std::string(u.path), std::string("/car/ws?dev=1"));

  CHECK(scout::ws::parseServerUrl("http://localhost:8080?x=1", &u));
  CHECK(!u.secure);
  CHECK_EQ(u.port, 8080);
  CHECK_EQ(std::string(u.path), std::string("/car/ws?x=1"));

  CHECK(scout::ws::parseServerUrl("ws://host/other/path", &u));
  CHECK_EQ(u.port, 80);
  CHECK_EQ(std::string(u.path), std::string("/other/path"));
}

TEST(server_url_rejects) {
  ServerUrl u;
  const char* bad[] = {
      "",
      "scout.example.com",
      "ftp://scout.example.com",
      "wss://",
      "wss://:443/car/ws",
      "wss://user:pw@scout.example.com/car/ws",
      "wss://[::1]/car/ws",
      "wss://scout.example.com:0/car/ws",
      "wss://scout.example.com:65536/car/ws",
      "wss://scout.example.com:123456/car/ws",
      "wss://scout.example.com:/car/ws",
      "wss://scout example.com/car/ws",
      "wss://scout.example.com/car ws",
      "wss://scout.example.com/car/ws#frag",
      "wss://scout.example.com\\car",
  };
  for (const char* text : bad) {
    if (scout::ws::parseServerUrl(text, &u)) testing::fail(__FILE__, __LINE__, std::string("accepted: ") + text);
  }
  CHECK(!scout::ws::parseServerUrl(nullptr, &u));
  std::string longHost = "wss://" + std::string(200, 'a') + ".com/car/ws";
  CHECK(!scout::ws::parseServerUrl(longHost.c_str(), &u));
}
