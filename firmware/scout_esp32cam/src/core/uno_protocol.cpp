#include "uno_protocol.h"

#include <string.h>

namespace scout {

UnoFrame::UnoFrame() : argc_(0), seq_(0) {
  text_[0] = '\0';
  for (uint8_t i = 0; i < scoutlink::kMaxArgs; ++i) argOffset_[i] = 0;
}

bool UnoFrame::assign(const scoutlink::Parser& parser) {
  size_t used = 0;
  const char* type = parser.type();
  size_t len = strlen(type);
  if (len + 1 > sizeof text_) return false;
  memcpy(text_, type, len + 1);
  used = len + 1;
  uint8_t argc = parser.argCount();
  if (argc > scoutlink::kMaxArgs) return false;
  for (uint8_t i = 0; i < argc; ++i) {
    const char* arg = parser.arg(i);
    len = strlen(arg);
    if (used + len + 1 > sizeof text_) return false;
    argOffset_[i] = static_cast<uint8_t>(used);
    memcpy(text_ + used, arg, len + 1);
    used += len + 1;
  }
  argc_ = argc;
  seq_ = parser.seq();
  return true;
}

const char* UnoFrame::arg(uint8_t index) const { return index < argc_ ? text_ + argOffset_[index] : ""; }

bool UnoFrame::argInt(uint8_t index, int32_t* out) const {
  return index < argc_ && scoutlink::parseInt(text_ + argOffset_[index], out);
}

bool UnoFrame::is(const char* type) const { return strcmp(text_, type) == 0; }

bool parseTelemetry(const UnoFrame& frame, UnoTelemetry* out) {
  if (!frame.is("TEL") || frame.argCount() < 7) return false;
  int32_t v[7];
  for (uint8_t i = 0; i < 7; ++i) {
    if (!frame.argInt(i, &v[i])) return false;
  }
  if (v[5] < 0 || v[5] > 65535 || v[6] < 0) return false;
  out->cm = v[0];
  out->line[0] = v[1];
  out->line[1] = v[2];
  out->line[2] = v[3];
  out->tilt = v[4];
  out->moveSeq = static_cast<uint16_t>(v[5]);
  out->rxErrors = static_cast<uint32_t>(v[6]);
  return true;
}

bool parseDone(const UnoFrame& frame, UnoDone* out) {
  if (!frame.is("DONE") || frame.argCount() < 4) return false;
  int32_t cmStart, cmEnd, ms;
  if (!frame.argInt(1, &cmStart) || !frame.argInt(2, &cmEnd) || !frame.argInt(3, &ms)) return false;
  out->reason = frame.arg(0);
  out->cmStart = cmStart;
  out->cmEnd = cmEnd;
  out->ms = ms;
  return true;
}

const char* doneReasonFromUno(const char* reason) {
  if (strcmp(reason, "TIME") == 0) return "time";
  if (strcmp(reason, "OBST") == 0) return "obstacle";
  if (strcmp(reason, "STOP") == 0) return "stopped";
  if (strcmp(reason, "LINK") == 0) return "link";
  if (strcmp(reason, "NEW") == 0) return "replaced";
  return "error";
}

const char* errCodeFromUno(const char* code) {
  if (strcmp(code, "RANGE") == 0 || strcmp(code, "ARGS") == 0) return "args";
  return "uno";
}

}  // namespace scout
