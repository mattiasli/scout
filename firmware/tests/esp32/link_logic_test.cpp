// Reconnect backoff, the UNO frame helpers and the id <-> seq request tracker.

#include <string>
#include <vector>

#include "ScoutLink.h"
#include "backoff.h"
#include "request_tracker.h"
#include "testing.h"
#include "uno_protocol.h"

using scout::Reply;
using scout::ReqKind;
using scout::RequestTracker;

namespace {

// Collects replies. Converts to the tracker's callback sink.
struct Recorder {
  std::vector<Reply> replies;
  static void record(void* self, const Reply& r) { static_cast<Recorder*>(self)->replies.push_back(r); }
  operator scout::ReplySink() { return scout::ReplySink{&Recorder::record, this}; }
  std::string describe(size_t i) const {
    const Reply& r = replies[i];
    std::string s = std::to_string(r.id) + ":";
    switch (r.type) {
      case Reply::kAck:
        s += "ack";
        if (r.hasDeg) s += "(" + std::to_string(r.deg) + ")";
        break;
      case Reply::kDone:
        s += std::string("done(") + r.reason + "," + std::to_string(r.cmStart) + "," + std::to_string(r.cmEnd) +
             "," + std::to_string(r.ms) + ")";
        break;
      case Reply::kErr:
        s += std::string("err(") + r.code + ")";
        break;
    }
    return s;
  }
  std::string all() const {
    std::string s;
    for (size_t i = 0; i < replies.size(); ++i) s += (i ? " " : "") + describe(i);
    return s;
  }
};

scout::UnoFrame frameFrom(const char* type, uint16_t seq, std::initializer_list<const char*> args) {
  scoutlink::Builder b(type, seq);
  for (const char* a : args) b.add(a);
  char buf[scoutlink::kMaxFrame];
  size_t n = b.finish(buf, sizeof buf);
  scoutlink::Parser p;
  bool got = false;
  for (size_t i = 0; i < n; ++i) got = p.feed(static_cast<uint8_t>(buf[i])) || got;
  CHECK(got);
  scout::UnoFrame f;
  CHECK(f.assign(p));
  return f;
}

}  // namespace

// ---------------------------------------------------------------- backoff

TEST(backoff_doubles_to_the_cap) {
  scout::Backoff b(2000, 60000, 20, 60000);
  const uint32_t mid = 20;  // random % 41 == 20 -> exactly 100 %
  uint32_t expected[] = {2000, 4000, 8000, 16000, 32000, 60000, 60000, 60000};
  for (uint32_t e : expected) CHECK_EQ(b.nextDelay(mid), e);
}

TEST(backoff_jitter_bounds) {
  scout::Backoff b(2000, 60000, 20, 60000);
  CHECK_EQ(b.nextDelay(0), 1600u);   // -20 %
  CHECK_EQ(b.nextDelay(40), 4800u);  // +20 % of 4000
  scout::Backoff c(2000, 60000, 20, 60000);
  for (int i = 0; i < 10; ++i) c.nextDelay(0);
  CHECK_EQ(c.nextDelay(40), 72000u);  // the cap plus jitter
  for (uint32_t r = 0; r < 1000; r += 7) {
    scout::Backoff d(2000, 60000, 20, 60000);
    uint32_t v = d.nextDelay(r * 2654435761u);
    CHECK(v >= 1600 && v <= 2400);
  }
}

TEST(backoff_resets_only_after_a_stable_connection) {
  scout::Backoff b(2000, 60000, 20, 60000);
  b.nextDelay(20);
  b.nextDelay(20);
  b.nextDelay(20);
  b.connected(1000);
  b.disconnected(1000 + 59999);  // short-lived: keep backing off
  CHECK_EQ(b.nextDelay(20), 16000u);
  b.connected(200000);
  b.disconnected(200000 + 60000);  // stayed up 60 s: start over
  CHECK_EQ(b.nextDelay(20), 2000u);
  b.disconnected(400000);  // a second disconnect without a connection changes nothing
  CHECK_EQ(b.nextDelay(20), 4000u);
}

TEST(backoff_survives_millis_wrap) {
  scout::Backoff b(2000, 60000, 20, 60000);
  b.nextDelay(20);
  b.nextDelay(20);
  b.connected(0xFFFFFF00u);
  b.disconnected(0xFFFFFF00u + 70000u);  // wraps past zero
  CHECK_EQ(b.nextDelay(20), 2000u);
}

// ---------------------------------------------------------------- UNO frames

TEST(seq_counter_wraps_past_65535_to_1) {
  scout::SeqCounter c;
  CHECK_EQ(c.next(), 1);
  CHECK_EQ(c.next(), 2);
  scout::SeqCounter w(65534);
  CHECK_EQ(w.next(), 65535);
  CHECK_EQ(w.next(), 1);
  CHECK_EQ(w.next(), 2);
}

TEST(uno_frame_copies_fields) {
  scout::UnoFrame f = frameFrom("DONE", 12, {"OBST", "40", "14", "350"});
  CHECK(f.is("DONE"));
  CHECK_EQ(f.seq(), 12);
  CHECK_EQ(f.argCount(), 4);
  CHECK_EQ(std::string(f.arg(0)), std::string("OBST"));
  CHECK_EQ(std::string(f.arg(9)), std::string(""));
  int32_t v = 0;
  CHECK(f.argInt(1, &v));
  CHECK_EQ(v, 40);
  CHECK(!f.argInt(0, &v));
  CHECK(!f.argInt(4, &v));

  scout::UnoDone d;
  CHECK(scout::parseDone(f, &d));
  CHECK_EQ(std::string(d.reason), std::string("OBST"));
  CHECK_EQ(d.cmStart, 40);
  CHECK_EQ(d.cmEnd, 14);
  CHECK_EQ(d.ms, 350);
}

TEST(uno_frame_with_full_body) {
  // 8 args and a 64-byte body: the largest frame there is.
  scout::UnoFrame f = frameFrom("ABCDEFGH", 65535, {"12345", "12345", "12345", "12345", "12345", "12345",
                                                     "12345", "1234567"});
  CHECK(f.is("ABCDEFGH"));
  CHECK_EQ(f.seq(), 65535);
  CHECK_EQ(f.argCount(), 8);
  CHECK_EQ(std::string(f.arg(0)), std::string("12345"));
  CHECK_EQ(std::string(f.arg(7)), std::string("1234567"));
}

TEST(telemetry_parsing) {
  scout::UnoTelemetry t;
  CHECK(scout::parseTelemetry(frameFrom("TEL", 0, {"-1", "512", "600", "480", "-10", "42", "3"}), &t));
  CHECK_EQ(t.cm, -1);
  CHECK_EQ(t.line[0], 512);
  CHECK_EQ(t.line[2], 480);
  CHECK_EQ(t.tilt, -10);
  CHECK_EQ(t.moveSeq, 42);
  CHECK_EQ(t.rxErrors, 3u);
  CHECK(!scout::parseTelemetry(frameFrom("TEL", 0, {"-1", "512", "600", "480", "0", "0"}), &t));
  CHECK(!scout::parseTelemetry(frameFrom("TEL", 0, {"x", "512", "600", "480", "0", "0", "0"}), &t));
  CHECK(!scout::parseTelemetry(frameFrom("TEL", 0, {"1", "2", "3", "4", "0", "70000", "0"}), &t));
  CHECK(!scout::parseTelemetry(frameFrom("PONG", 1, {"0.1.0", "5"}), &t));
}

TEST(done_and_err_word_mapping) {
  CHECK_EQ(std::string(scout::doneReasonFromUno("TIME")), std::string("time"));
  CHECK_EQ(std::string(scout::doneReasonFromUno("OBST")), std::string("obstacle"));
  CHECK_EQ(std::string(scout::doneReasonFromUno("STOP")), std::string("stopped"));
  CHECK_EQ(std::string(scout::doneReasonFromUno("LINK")), std::string("link"));
  CHECK_EQ(std::string(scout::doneReasonFromUno("NEW")), std::string("replaced"));
  CHECK_EQ(std::string(scout::doneReasonFromUno("WHAT")), std::string("error"));
  CHECK_EQ(std::string(scout::doneReasonFromUno("time")), std::string("error"));
  CHECK_EQ(std::string(scout::errCodeFromUno("RANGE")), std::string("args"));
  CHECK_EQ(std::string(scout::errCodeFromUno("ARGS")), std::string("args"));
  CHECK_EQ(std::string(scout::errCodeFromUno("TYPE")), std::string("uno"));
  CHECK_EQ(std::string(scout::errCodeFromUno("")), std::string("uno"));
}

// ---------------------------------------------------------------- request tracker

TEST(move_ack_then_done) {
  RequestTracker t;
  Recorder r;
  CHECK(t.add(5, 2, ReqKind::kMove, 1000, 800 + 1500));
  CHECK(t.moving());
  t.onAck(5, false, 0, r);
  t.onAck(5, false, 0, r);  // a duplicate ACK is not forwarded twice
  t.onDone(5, "TIME", 95, 71, 800, r);
  CHECK_EQ(r.all(), std::string("2:ack 2:done(time,95,71,800)"));
  CHECK(!t.moving());
  CHECK_EQ(t.pending(), 0);
}

TEST(done_without_ack_still_acks_first) {
  RequestTracker t;
  Recorder r;
  t.add(9, 4, ReqKind::kTest, 0, 3500);
  t.onDone(9, "OBST", 12, -1, 0, r);
  CHECK_EQ(r.all(), std::string("4:ack 4:done(obstacle,12,-1,0)"));
}

TEST(replaced_and_stopped_moves) {
  RequestTracker t;
  Recorder r;
  t.add(1, 10, ReqKind::kMove, 0, 6500);
  t.onAck(1, false, 0, r);
  t.add(2, 11, ReqKind::kMove, 100, 6500);
  t.onAck(2, false, 0, r);
  t.onDone(1, "NEW", 50, 48, 100, r);
  t.add(3, 12, ReqKind::kStop, 200, 1000);
  t.onAck(3, false, 0, r);
  t.onDone(2, "STOP", 48, 47, 100, r);
  CHECK_EQ(r.all(), std::string("10:ack 11:ack 10:done(replaced,50,48,100) 12:ack 11:done(stopped,48,47,100)"));
}

TEST(move_timeout_is_done_error_plus_err_timeout) {
  RequestTracker t;
  Recorder r;
  t.add(7, 3, ReqKind::kMove, 1000, 800 + 1500);
  t.onAck(7, false, 0, r);
  t.tick(1000 + 2299, r);
  CHECK_EQ(r.replies.size(), 1u);
  t.tick(1000 + 2300, r);
  CHECK_EQ(r.all(), std::string("3:ack 3:done(error,-1,-1,0) 3:err(timeout)"));
  t.onDone(7, "TIME", 1, 1, 800, r);  // too late: ignored
  CHECK_EQ(r.replies.size(), 3u);
}

TEST(timeouts_survive_millis_wrap) {
  RequestTracker t;
  Recorder r;
  t.add(7, 3, ReqKind::kTilt, 0xFFFFFF00u, 1000);
  t.tick(0xFFFFFF00u + 999u, r);
  CHECK(r.replies.empty());
  t.tick(0xFFFFFF00u + 1000u, r);
  CHECK_EQ(r.all(), std::string("3:err(timeout)"));
}

TEST(tilt_and_stop_acks) {
  RequestTracker t;
  Recorder r;
  t.add(1, 5, ReqKind::kTilt, 0, 1000);
  t.add(2, 6, ReqKind::kStop, 0, 1000);
  t.onAck(1, true, -20, r);
  t.onAck(2, false, 0, r);
  t.tick(5000, r);
  CHECK_EQ(r.all(), std::string("5:ack(-20) 6:ack"));
  CHECK_EQ(t.pending(), 0);
}

TEST(err_from_uno_is_mapped) {
  RequestTracker t;
  Recorder r;
  t.add(1, 5, ReqKind::kMove, 0, 6500);
  t.add(2, 6, ReqKind::kTilt, 0, 1000);
  t.onErr(1, "RANGE", r);
  t.onErr(2, "TYPE", r);
  CHECK_EQ(r.all(), std::string("5:err(args) 6:err(uno)"));
  CHECK(!t.moving());
}

TEST(conf_acks_once_all_parts_are_acked) {
  RequestTracker t;
  Recorder r;
  t.add(1, 8, ReqKind::kConf, 0, 1000);
  t.add(2, 8, ReqKind::kConf, 0, 1000);
  t.add(3, 8, ReqKind::kConf, 0, 1000);
  t.onAck(2, false, 0, r);
  t.onAck(1, false, 0, r);
  CHECK(r.replies.empty());
  t.onAck(3, false, 0, r);
  CHECK_EQ(r.all(), std::string("8:ack"));
}

TEST(conf_fails_on_first_err_or_timeout) {
  RequestTracker t;
  Recorder r;
  t.add(1, 8, ReqKind::kConf, 0, 1000);
  t.add(2, 8, ReqKind::kConf, 0, 1000);
  t.add(3, 9, ReqKind::kConf, 0, 1000);
  t.add(4, 9, ReqKind::kConf, 0, 1000);
  t.onErr(1, "RANGE", r);
  t.onAck(2, false, 0, r);  // the rest of request 8 no longer matters
  t.onAck(3, false, 0, r);
  t.tick(1000, r);          // request 9 is still missing one ACK
  CHECK_EQ(r.all(), std::string("8:err(args) 9:err(timeout)"));
  CHECK_EQ(t.pending(), 0);
}

TEST(internal_requests_never_reply) {
  RequestTracker t;
  Recorder r;
  t.add(1, 0, ReqKind::kConf, 0, 1000);
  t.add(2, 0, ReqKind::kConf, 0, 1000);
  t.add(3, 0, ReqKind::kStop, 0, 1000);
  t.onAck(1, false, 0, r);
  t.onErr(2, "RANGE", r);
  t.tick(2000, r);
  CHECK(r.replies.empty());
  CHECK_EQ(t.pending(), 0);
}

TEST(uno_boot_fails_everything_pending) {
  RequestTracker t;
  Recorder r;
  t.add(1, 20, ReqKind::kMove, 0, 6500);
  t.onAck(1, false, 0, r);
  t.add(2, 21, ReqKind::kConf, 0, 1000);
  t.add(3, 21, ReqKind::kConf, 0, 1000);
  t.add(4, 22, ReqKind::kTilt, 0, 1000);
  t.onUnoBoot(r);
  CHECK_EQ(r.all(), std::string("20:ack 20:done(error,-1,-1,0) 20:err(uno) 21:err(uno) 22:err(uno)"));
  CHECK_EQ(t.pending(), 0);
}

TEST(unknown_seqs_are_ignored) {
  RequestTracker t;
  Recorder r;
  t.onAck(99, false, 0, r);
  t.onErr(99, "RANGE", r);
  t.onDone(99, "TIME", 0, 0, 0, r);
  t.add(5, 1, ReqKind::kTilt, 0, 1000);
  t.onDone(5, "TIME", 0, 0, 0, r);  // DONE for a non-move is ignored
  CHECK(r.replies.empty());
  CHECK(t.tracks(5));
}

TEST(tracker_capacity_and_clear) {
  RequestTracker t;
  for (uint16_t i = 1; i <= RequestTracker::kSlots; ++i) CHECK(t.add(i, i, ReqKind::kTilt, 0, 1000));
  CHECK(t.full());
  CHECK(!t.add(100, 100, ReqKind::kTilt, 0, 1000));
  t.clear();
  CHECK_EQ(t.pending(), 0);
  CHECK(t.add(100, 100, ReqKind::kTilt, 0, 1000));
}

TEST(reused_seq_replaces_the_stale_entry) {
  RequestTracker t;
  Recorder r;
  t.add(1, 30, ReqKind::kTilt, 0, 1000);
  t.add(1, 31, ReqKind::kStop, 0, 1000);
  CHECK_EQ(t.pending(), 1);
  t.onAck(1, false, 0, r);
  CHECK_EQ(r.all(), std::string("31:ack"));
}
