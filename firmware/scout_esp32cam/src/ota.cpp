#include "ota.h"

#include <HTTPClient.h>
#include <NetworkClientSecure.h>
#include <esp_ota_ops.h>
#include <mbedtls/sha256.h>

#include "config.h"
#include "core/util.h"

namespace scout {

bool Ota::begin() {
  queue_ = xQueueCreate(8, sizeof(OtaProgress));
  return queue_ != nullptr;
}

bool Ota::pendingVerify() {
  const esp_partition_t* running = esp_ota_get_running_partition();
  esp_ota_img_states_t state;
  return running != nullptr && esp_ota_get_state_partition(running, &state) == ESP_OK &&
         state == ESP_OTA_IMG_PENDING_VERIFY;
}

void Ota::markAppGood() {
  if (pendingVerify()) esp_ota_mark_app_valid_cancel_rollback();
}

void Ota::rollBack() { esp_ota_mark_app_invalid_rollback_and_reboot(); }

bool Ota::start(const char* url, const uint8_t sha256[32], uint32_t size, char* why, size_t whyCap) {
  if (running_) {
    snprintf(why, whyCap, "an update is already running");
    return false;
  }
  if (strncasecmp(url, "https://", 8) != 0) {
    snprintf(why, whyCap, "the url must be https");
    return false;
  }
  if (strlen(url) >= sizeof url_) {
    snprintf(why, whyCap, "url too long");
    return false;
  }
  const esp_partition_t* slot = esp_ota_get_next_update_partition(nullptr);
  if (slot == nullptr) {
    snprintf(why, whyCap, "no update slot (partition scheme without OTA)");
    return false;
  }
  if (size == 0 || size > slot->size) {
    snprintf(why, whyCap, "size must be 1..%lu bytes", static_cast<unsigned long>(slot->size));
    return false;
  }
  if (pendingVerify()) {
    snprintf(why, whyCap, "this firmware is not confirmed yet");
    return false;
  }
  if (ESP.getFreeHeap() < cfg::kOtaMinFreeInternal) {
    snprintf(why, whyCap, "not enough memory (%lu bytes free)", static_cast<unsigned long>(ESP.getFreeHeap()));
    return false;
  }
  strcpy(url_, url);
  memcpy(sha_, sha256, sizeof sha_);
  size_ = size;
  xQueueReset(queue_);
  running_ = true;
  if (xTaskCreatePinnedToCore(&Ota::taskEntry, "ota", cfg::kOtaTaskStack, this, cfg::kOtaTaskPriority, nullptr, 1) !=
      pdPASS) {
    running_ = false;
    snprintf(why, whyCap, "could not start the update task");
    return false;
  }
  return true;
}

bool Ota::takeProgress(OtaProgress* out) { return queue_ != nullptr && xQueueReceive(queue_, out, 0) == pdTRUE; }

void Ota::report(OtaProgress::State state, int pct, const char* msg) {
  OtaProgress p;
  p.state = state;
  p.pct = pct;
  strncpy(p.msg, msg, sizeof p.msg - 1);
  p.msg[sizeof p.msg - 1] = '\0';
  // Never block the download on a full queue; the final states are retried.
  TickType_t wait = (state == OtaProgress::kReady || state == OtaProgress::kFailed) ? portMAX_DELAY : 0;
  xQueueSend(queue_, &p, wait);
}

void Ota::taskEntry(void* self) {
  Ota* ota = static_cast<Ota*>(self);
  ota->run();
  ota->running_ = false;
  vTaskDelete(nullptr);
}

void Ota::run() {
  char msg[64];
  report(OtaProgress::kDownloading, 0, "");

  uint8_t* buf = static_cast<uint8_t*>(malloc(cfg::kOtaBufferBytes));
  if (buf == nullptr) {
    report(OtaProgress::kFailed, 0, "out of memory");
    return;
  }

  NetworkClientSecure tls;
  tls.useBuiltinCACertBundle();  // the same CA bundle check as the car link
  tls.setHandshakeTimeout(cfg::kOtaTlsHandshakeTimeoutS);
  HTTPClient http;
  http.setFollowRedirects(HTTPC_FORCE_FOLLOW_REDIRECTS);
  http.setRedirectLimit(cfg::kOtaRedirectLimit);
  http.useHTTP10(true);  // no chunked encoding: the body is the image, byte for byte
  http.setTimeout(cfg::kOtaHttpTimeoutMs);
  http.setConnectTimeout(cfg::kOtaHttpTimeoutMs);
  char agent[32];
  snprintf(agent, sizeof agent, "scout-car/%s", cfg::kFirmwareVersion);
  http.setUserAgent(agent);

  esp_ota_handle_t handle = 0;
  bool otaOpen = false;
  mbedtls_sha256_context sha;
  mbedtls_sha256_init(&sha);
  const esp_partition_t* slot = esp_ota_get_next_update_partition(nullptr);
  bool ok = false;
  msg[0] = '\0';

  do {
    if (!http.begin(tls, url_)) {
      snprintf(msg, sizeof msg, "bad url");
      break;
    }
    int code = http.GET();
    if (code != HTTP_CODE_OK) {
      if (code < 0) {
        snprintf(msg, sizeof msg, "download failed: %s", HTTPClient::errorToString(code).c_str());
      } else {
        snprintf(msg, sizeof msg, "download failed: HTTP %d", code);
      }
      break;
    }
    int length = http.getSize();
    if (length >= 0 && static_cast<uint32_t>(length) != size_) {
      snprintf(msg, sizeof msg, "size mismatch: server sent %d bytes", length);
      break;
    }
    // Erase as we go, so the first bytes arrive before the whole slot is erased.
    if (slot == nullptr || esp_ota_begin(slot, OTA_WITH_SEQUENTIAL_WRITES, &handle) != ESP_OK) {
      snprintf(msg, sizeof msg, "could not open the update slot");
      break;
    }
    otaOpen = true;
    mbedtls_sha256_starts(&sha, 0);

    NetworkClient* stream = http.getStreamPtr();
    uint32_t received = 0;
    uint32_t lastData = millis();
    int lastPct = 0;
    bool failed = false;
    while (received < size_) {
      int ready = stream != nullptr ? stream->available() : 0;
      size_t avail = ready > 0 ? static_cast<size_t>(ready) : 0;
      if (avail == 0) {
        if (stream == nullptr || !stream->connected() || millis() - lastData > cfg::kOtaHttpTimeoutMs) {
          snprintf(msg, sizeof msg, "download stopped after %lu bytes", static_cast<unsigned long>(received));
          failed = true;
          break;
        }
        delay(2);
        continue;
      }
      size_t want = cfg::kOtaBufferBytes;
      if (want > avail) want = avail;
      if (want > size_ - received) want = size_ - received;
      int got = stream->read(buf, want);
      if (got <= 0) continue;
      lastData = millis();
      if (esp_ota_write(handle, buf, static_cast<size_t>(got)) != ESP_OK) {
        snprintf(msg, sizeof msg, "flash write failed");
        failed = true;
        break;
      }
      mbedtls_sha256_update(&sha, buf, static_cast<size_t>(got));
      received += static_cast<uint32_t>(got);
      int pct = percentOf(received, size_);
      if (pct >= lastPct + cfg::kOtaProgressStepPct && pct < 100) {
        lastPct = pct - pct % cfg::kOtaProgressStepPct;
        report(OtaProgress::kDownloading, lastPct, "");
      }
    }
    if (failed) break;
    // Anything beyond the announced size means it isn't the image we were promised.
    delay(50);
    if (stream->available() > 0) {
      snprintf(msg, sizeof msg, "size mismatch: server sent more than %lu bytes", static_cast<unsigned long>(size_));
      break;
    }

    report(OtaProgress::kVerifying, 100, "");
    uint8_t digest[32];
    mbedtls_sha256_finish(&sha, digest);
    if (memcmp(digest, sha_, sizeof digest) != 0) {
      snprintf(msg, sizeof msg, "sha256 mismatch");
      break;
    }
    esp_err_t err = esp_ota_end(handle);  // also checks the image header and checksum
    otaOpen = false;
    if (err != ESP_OK) {
      snprintf(msg, sizeof msg, "not a valid firmware image (%s)", esp_err_to_name(err));
      break;
    }
    if (esp_ota_set_boot_partition(slot) != ESP_OK) {
      snprintf(msg, sizeof msg, "could not switch to the new image");
      break;
    }
    ok = true;
  } while (false);

  if (otaOpen) esp_ota_abort(handle);
  mbedtls_sha256_free(&sha);
  http.end();
  tls.stop();
  free(buf);
  if (ok) {
    report(OtaProgress::kReady, 100, "");
  } else {
    report(OtaProgress::kFailed, 0, msg[0] ? msg : "update failed");
  }
}

}  // namespace scout
