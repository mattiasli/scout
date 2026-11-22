#include "request_tracker.h"

#include "uno_protocol.h"

namespace scout {

namespace {

bool finishesWithDone(ReqKind kind) { return kind == ReqKind::kMove || kind == ReqKind::kTest; }

const char* unoErrMessage(const char* code) {
  return code[0] == 'a' ? "the UNO rejected a value as out of range" : "the UNO did not understand the request";
}

}  // namespace

Reply makeAck(uint32_t id) {
  Reply r = Reply();
  r.type = Reply::kAck;
  r.id = id;
  return r;
}

Reply makeDone(uint32_t id, const char* reason, int32_t cmStart, int32_t cmEnd, int32_t ms) {
  Reply r = Reply();
  r.type = Reply::kDone;
  r.id = id;
  r.reason = reason;
  r.cmStart = cmStart;
  r.cmEnd = cmEnd;
  r.ms = ms;
  return r;
}

Reply makeErr(uint32_t id, const char* code, const char* msg) {
  Reply r = Reply();
  r.type = Reply::kErr;
  r.id = id;
  r.code = code;
  r.msg = msg;
  return r;
}

RequestTracker::RequestTracker() { clear(); }

void RequestTracker::clear() {
  for (uint8_t i = 0; i < kSlots; ++i) slots_[i] = Slot();
}

RequestTracker::Slot* RequestTracker::find(uint16_t seq) {
  for (uint8_t i = 0; i < kSlots; ++i) {
    if (slots_[i].used && slots_[i].seq == seq) return &slots_[i];
  }
  return nullptr;
}

void RequestTracker::release(Slot* slot) { *slot = Slot(); }

bool RequestTracker::hasOther(const Slot* slot) const {
  for (uint8_t i = 0; i < kSlots; ++i) {
    const Slot& s = slots_[i];
    if (&s != slot && s.used && s.kind == slot->kind && s.id == slot->id) return true;
  }
  return false;
}

void RequestTracker::dropRequest(uint32_t id, ReqKind kind) {
  for (uint8_t i = 0; i < kSlots; ++i) {
    if (slots_[i].used && slots_[i].kind == kind && slots_[i].id == id) release(&slots_[i]);
  }
}

void RequestTracker::emit(const ReplySink& out, const Reply& reply, uint32_t id) {
  if (id != 0) out.reply(reply);
}

bool RequestTracker::add(uint16_t seq, uint32_t id, ReqKind kind, uint32_t now, uint32_t timeoutMs) {
  Slot* stale = find(seq);
  if (stale != nullptr) release(stale);  // only after a full 65535 wrap; never in practice
  for (uint8_t i = 0; i < kSlots; ++i) {
    Slot& s = slots_[i];
    if (s.used) continue;
    s.used = true;
    s.acked = false;
    s.kind = kind;
    s.seq = seq;
    s.id = id;
    s.start = now;
    s.timeout = timeoutMs;
    return true;
  }
  return false;
}

void RequestTracker::onAck(uint16_t seq, bool hasValue, int32_t value, const ReplySink& out) {
  Slot* s = find(seq);
  if (s == nullptr) return;
  uint32_t id = s->id;
  switch (s->kind) {
    case ReqKind::kMove:
    case ReqKind::kTest:
      if (!s->acked) {
        s->acked = true;
        emit(out, makeAck(id), id);
      }
      break;
    case ReqKind::kStop:
      release(s);
      emit(out, makeAck(id), id);
      break;
    case ReqKind::kTilt: {
      release(s);
      Reply r = makeAck(id);
      r.hasDeg = hasValue;
      r.deg = value;
      emit(out, r, id);
      break;
    }
    case ReqKind::kConf: {
      bool more = hasOther(s);
      release(s);
      if (!more) emit(out, makeAck(id), id);
      break;
    }
  }
}

void RequestTracker::onErr(uint16_t seq, const char* unoCode, const ReplySink& out) {
  Slot* s = find(seq);
  if (s == nullptr) return;
  uint32_t id = s->id;
  ReqKind kind = s->kind;
  if (kind == ReqKind::kConf) {
    dropRequest(id, kind);
  } else {
    release(s);
  }
  const char* code = errCodeFromUno(unoCode);
  emit(out, makeErr(id, code, unoErrMessage(code)), id);
}

void RequestTracker::onDone(uint16_t seq, const char* unoReason, int32_t cmStart, int32_t cmEnd, int32_t ms,
                            const ReplySink& out) {
  Slot* s = find(seq);
  if (s == nullptr || !finishesWithDone(s->kind)) return;
  uint32_t id = s->id;
  bool acked = s->acked;
  release(s);
  if (!acked) emit(out, makeAck(id), id);  // the ACK frame was lost; keep the order ack, done
  emit(out, makeDone(id, doneReasonFromUno(unoReason), cmStart, cmEnd, ms), id);
}

void RequestTracker::onUnoBoot(const ReplySink& out) {
  for (uint8_t i = 0; i < kSlots; ++i) {
    if (!slots_[i].used) continue;
    Slot s = slots_[i];
    if (s.kind == ReqKind::kConf) {
      dropRequest(s.id, s.kind);
    } else {
      release(&slots_[i]);
    }
    if (finishesWithDone(s.kind)) emit(out, makeDone(s.id, "error", -1, -1, 0), s.id);
    emit(out, makeErr(s.id, "uno", "the UNO restarted"), s.id);
  }
}

void RequestTracker::tick(uint32_t now, const ReplySink& out) {
  for (uint8_t i = 0; i < kSlots; ++i) {
    if (!slots_[i].used || now - slots_[i].start < slots_[i].timeout) continue;
    Slot s = slots_[i];
    if (s.kind == ReqKind::kConf) {
      dropRequest(s.id, s.kind);
    } else {
      release(&slots_[i]);
    }
    if (finishesWithDone(s.kind)) {
      emit(out, makeDone(s.id, "error", -1, -1, 0), s.id);
      emit(out, makeErr(s.id, "timeout", "no DONE from the UNO"), s.id);
    } else {
      emit(out, makeErr(s.id, "timeout", "no reply from the UNO"), s.id);
    }
  }
}

bool RequestTracker::moving() const {
  for (uint8_t i = 0; i < kSlots; ++i) {
    if (slots_[i].used && finishesWithDone(slots_[i].kind)) return true;
  }
  return false;
}

bool RequestTracker::full() const { return pending() == kSlots; }

uint8_t RequestTracker::pending() const {
  uint8_t n = 0;
  for (uint8_t i = 0; i < kSlots; ++i) {
    if (slots_[i].used) ++n;
  }
  return n;
}

bool RequestTracker::tracks(uint16_t seq) const {
  for (uint8_t i = 0; i < kSlots; ++i) {
    if (slots_[i].used && slots_[i].seq == seq) return true;
  }
  return false;
}

}  // namespace scout
