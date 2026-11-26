#include "uno_bridge.h"

#include "config.h"

namespace scout {

bool UnoBridge::begin() {
  txLock_ = xSemaphoreCreateMutex();
  queue_ = xQueueCreate(cfg::kUnoQueueDepth, sizeof(UnoFrame));
  return txLock_ != nullptr && queue_ != nullptr;
}

bool UnoBridge::startPinging() {
  if (queue_ == nullptr) return false;
  // Same core as the loop task, so WiFi and the camera on core 0 are left alone.
  return xTaskCreatePinnedToCore(&UnoBridge::taskEntry, "uno", cfg::kUnoTaskStack, this, cfg::kUnoTaskPriority,
                                 nullptr, 1) == pdPASS;
}

bool UnoBridge::askUptime(uint32_t waitMs, uint32_t* uptimeS) {
  if (queue_ == nullptr) return false;
  uint32_t start = millis();
  uint32_t lastPing = start - cfg::kUnoPingMs;  // the first PING goes out at once
  while (millis() - start < waitMs) {
    if (millis() - lastPing >= cfg::kUnoPingMs) {
      lastPing = millis();
      send("PING", nullptr, 0);
    }
    while (Serial.available() > 0) {
      int byte = Serial.read();
      if (byte < 0) break;
      if (!parser_.feed(static_cast<uint8_t>(byte))) continue;
      lastRxMs_ = millis();
      everRx_ = true;
      if (!frame_.assign(parser_)) continue;
      handle(frame_);  // keeps TEL and the version, and queues BOOT and replies as usual
      if (frame_.is("BOOT")) {
        *uptimeS = 0;
        return true;
      }
      int32_t up;
      if (frame_.is("PONG") && frame_.argInt(1, &up) && up >= 0) {
        *uptimeS = static_cast<uint32_t>(up);
        return true;
      }
    }
    delay(2);
  }
  return false;
}

void UnoBridge::taskEntry(void* self) { static_cast<UnoBridge*>(self)->run(); }

void UnoBridge::run() {
  uint32_t lastPing = millis() - cfg::kUnoPingMs;
  for (;;) {
    int budget = 256;
    while (budget-- > 0 && Serial.available() > 0) {
      int byte = Serial.read();
      if (byte < 0) break;
      if (!parser_.feed(static_cast<uint8_t>(byte))) continue;
      lastRxMs_ = millis();
      everRx_ = true;
      if (frame_.assign(parser_)) handle(frame_);
    }
    uint32_t now = millis();
    if (now - lastPing >= cfg::kUnoPingMs) {
      lastPing = now;
      send("PING", nullptr, 0);
    }
    vTaskDelay(pdMS_TO_TICKS(5));
  }
}

void UnoBridge::handle(const UnoFrame& frame) {
  if (frame.is("TEL")) {
    UnoTelemetry tel;
    if (!parseTelemetry(frame, &tel)) return;
    portENTER_CRITICAL(&stateLock_);
    tel_ = tel;
    telMs_ = millis();
    haveTel_ = true;
    portEXIT_CRITICAL(&stateLock_);
    return;
  }
  bool boot = frame.is("BOOT");
  if (frame.is("PONG") || boot) {
    const char* fw = frame.arg(0);
    if (fw[0] != '\0') {
      portENTER_CRITICAL(&stateLock_);
      strncpy(fw_, fw, sizeof fw_ - 1);
      fw_[sizeof fw_ - 1] = '\0';
      portEXIT_CRITICAL(&stateLock_);
    }
    if (!boot) return;
  }
  if (!boot && !frame.is("ACK") && !frame.is("ERR") && !frame.is("DONE")) return;
  if (xQueueSend(queue_, &frame, 0) == pdTRUE) return;
  dropped_ = dropped_ + 1;  // only this task writes it
  if (boot) {
    // After a BOOT nothing queued before it matters any more: make room for it.
    xQueueReset(queue_);
    xQueueSend(queue_, &frame, 0);
  }
}

uint16_t UnoBridge::transmit(const scoutlink::Builder& builder, uint16_t seq) {
  char frame[scoutlink::kMaxFrame];
  size_t len = builder.finish(frame, sizeof frame);
  if (len == 0) return 0;
  Serial.write(reinterpret_cast<const uint8_t*>(frame), len);
  return seq;
}

uint16_t UnoBridge::send(const char* type, const int32_t* args, uint8_t argCount) {
  if (txLock_ == nullptr) return 0;
  xSemaphoreTake(txLock_, portMAX_DELAY);
  uint16_t seq = seq_.next();
  scoutlink::Builder builder(type, seq);
  for (uint8_t i = 0; i < argCount; ++i) builder.add(args[i]);
  uint16_t sent = transmit(builder, seq);
  xSemaphoreGive(txLock_);
  return sent;
}

uint16_t UnoBridge::sendConf(const char* key, int32_t value) {
  if (txLock_ == nullptr) return 0;
  xSemaphoreTake(txLock_, portMAX_DELAY);
  uint16_t seq = seq_.next();
  scoutlink::Builder builder("CONF", seq);
  builder.add(key).add(value);
  uint16_t sent = transmit(builder, seq);
  xSemaphoreGive(txLock_);
  return sent;
}

bool UnoBridge::takeFrame(UnoFrame* out) { return queue_ != nullptr && xQueueReceive(queue_, out, 0) == pdTRUE; }

bool UnoBridge::present() const {
  uint32_t last = lastRxMs_;  // read before millis(), so now >= last
  uint32_t now = millis();
  return everRx_ && now - last < cfg::kUnoPresentMs;
}

bool UnoBridge::telemetry(UnoTelemetry* out) const {
  portENTER_CRITICAL(&stateLock_);
  bool have = haveTel_;
  uint32_t at = telMs_;
  *out = tel_;
  portEXIT_CRITICAL(&stateLock_);
  return have && millis() - at < cfg::kUnoPresentMs;
}

bool UnoBridge::firmware(char* out, size_t cap) const {
  portENTER_CRITICAL(&stateLock_);
  strncpy(out, fw_, cap - 1);
  out[cap - 1] = '\0';
  portEXIT_CRITICAL(&stateLock_);
  return out[0] != '\0';
}

}  // namespace scout
