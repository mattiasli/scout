// Maps car-link request ids to the UART seqs used for them and turns the UNO's ACK,
// ERR and DONE frames (and their absence) into car-link replies.
//
//   move/test: ACK -> ack, DONE -> done. No DONE within ms + 1500 -> done "error" plus
//              err "timeout".
//   stop/tilt: ACK -> ack (tilt with the applied deg). No ACK in time -> err "timeout".
//   conf:      one server request may need several UART CONF frames. The ack goes out
//              once every one of them is acknowledged; the first ERR or timeout fails
//              the whole request.
//   ERR from the UNO -> err with the mapped code.
//
// Requests with id 0 are the car's own (CONF after a UNO reboot, STOP when the link
// drops): they are tracked the same way but never produce replies.

#ifndef SCOUT_CORE_REQUEST_TRACKER_H
#define SCOUT_CORE_REQUEST_TRACKER_H

#include <stddef.h>
#include <stdint.h>

namespace scout {

enum class ReqKind : uint8_t { kMove, kTest, kStop, kTilt, kConf };

struct Reply {
  enum Type : uint8_t { kAck, kDone, kErr };
  Type type;
  uint32_t id;
  // kAck
  bool hasDeg;
  int32_t deg;
  // kDone. cmStart/cmEnd of -1 mean "no echo" (null on the wire).
  const char* reason;
  int32_t cmStart;
  int32_t cmEnd;
  int32_t ms;
  // kErr
  const char* code;
  const char* msg;
};

// Where replies go: a plain callback (no virtual functions, so the host tests link with
// every toolchain).
struct ReplySink {
  void (*fn)(void* context, const Reply& reply);
  void* context;
  void reply(const Reply& r) const { fn(context, r); }
};

class RequestTracker {
 public:
  static const uint8_t kSlots = 16;

  RequestTracker();

  // Registers a request that was just sent to the UNO under `seq`. `timeoutMs` is how
  // long to wait for its final answer (DONE for move/test, ACK otherwise). Returns
  // false when every slot is taken.
  bool add(uint16_t seq, uint32_t id, ReqKind kind, uint32_t now, uint32_t timeoutMs);

  void onAck(uint16_t seq, bool hasValue, int32_t value, const ReplySink& out);
  void onErr(uint16_t seq, const char* unoCode, const ReplySink& out);
  void onDone(uint16_t seq, const char* unoReason, int32_t cmStart, int32_t cmEnd, int32_t ms, const ReplySink& out);

  // The UNO restarted: nothing it was doing will be answered any more.
  void onUnoBoot(const ReplySink& out);

  // Reports requests that ran out of time.
  void tick(uint32_t now, const ReplySink& out);

  // Forgets everything without replying (the server connection is gone).
  void clear();

  bool moving() const;  // a move or test is waiting for its DONE
  bool full() const;
  uint8_t pending() const;
  bool tracks(uint16_t seq) const;

 private:
  struct Slot {
    bool used;
    bool acked;
    ReqKind kind;
    uint16_t seq;
    uint32_t id;
    uint32_t start;
    uint32_t timeout;
  };

  Slot* find(uint16_t seq);
  void release(Slot* slot);
  bool hasOther(const Slot* slot) const;  // another conf slot for the same request
  void dropRequest(uint32_t id, ReqKind kind);
  static void emit(const ReplySink& out, const Reply& reply, uint32_t id);

  Slot slots_[kSlots];
};

Reply makeAck(uint32_t id);
Reply makeDone(uint32_t id, const char* reason, int32_t cmStart, int32_t cmEnd, int32_t ms);
Reply makeErr(uint32_t id, const char* code, const char* msg);

}  // namespace scout

#endif  // SCOUT_CORE_REQUEST_TRACKER_H
