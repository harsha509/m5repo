#include "ota.h"

#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <Update.h>
#include <WiFi.h>
#include <esp_ota_ops.h>
#include <stdio.h>
#include <string.h>

static const uint16_t MANIFEST_TIMEOUT_MS = 4000;
static const uint32_t STREAM_TIMEOUT_S    = 15;
static const char*    NVS_NAMESPACE       = "ota";
static const char*    NVS_ATTEMPT_KEY     = "trying";

static char     host[64]  = "";
static uint16_t port      = 8787;
static char     token[64] = "";
static char     running[65] = "";
static char     status[48]  = "not checked";

static void copyInto(char* dst, size_t cap, const char* src) {
  strncpy(dst, src ? src : "", cap - 1);
  dst[cap - 1] = '\0';
}

// A boot-time download that hangs or crashes would retry on every boot and put
// the device permanently out of reach. The flag is set before the write starts
// and cleared once it finishes, so one bad attempt only disables the boot check
// until the Settings entry is used.
static bool attemptPending() {
  Preferences prefs;
  prefs.begin(NVS_NAMESPACE, true);
  bool pending = prefs.getBool(NVS_ATTEMPT_KEY, false);
  prefs.end();
  return pending;
}

static void markAttempt(bool pending) {
  Preferences prefs;
  prefs.begin(NVS_NAMESPACE, false);
  prefs.putBool(NVS_ATTEMPT_KEY, pending);
  prefs.end();
}

const char* otaRunningBuild() {
  if (!running[0]) {
    const esp_app_desc_t* desc = esp_ota_get_app_description();
    for (int i = 0; i < 32; i++) sprintf(running + i * 2, "%02x", desc->app_elf_sha256[i]);
  }
  return running;
}

const char* otaStatus() { return status; }

void otaInit(const char* brokerHost, uint16_t brokerPort, const char* brokerToken) {
  copyInto(host, sizeof(host), brokerHost);
  copyInto(token, sizeof(token), brokerToken);
  if (brokerPort) port = brokerPort;
  otaRunningBuild();
}

// Leaves the connection open on success so the caller can read the body;
// the caller always owns http.end().
static bool get(HTTPClient& http, const char* path, uint16_t timeoutMs) {
  char url[128];
  snprintf(url, sizeof(url), "http://%s:%u%s", host, port, path);
  if (!http.begin(url)) return false;
  http.setConnectTimeout(timeoutMs);
  http.setTimeout(timeoutMs);
  if (token[0]) {
    char auth[80];
    snprintf(auth, sizeof(auth), "Bearer %s", token);
    http.addHeader("Authorization", auth);
  }
  return http.GET() == HTTP_CODE_OK;
}

OtaResult otaCheck(bool fromMenu) {
  if (fromMenu) markAttempt(false);
  if (attemptPending()) {
    copyInto(status, sizeof(status), "held: last attempt failed");
    return OTA_FAILED;
  }
  if (!host[0] || WiFi.status() != WL_CONNECTED) {
    copyInto(status, sizeof(status), "no wifi");
    return OTA_UNREACHABLE;
  }

  HTTPClient manifest;
  if (!get(manifest, "/device/ota/manifest", MANIFEST_TIMEOUT_MS)) {
    manifest.end();
    copyInto(status, sizeof(status), "broker unreachable");
    return OTA_UNREACHABLE;
  }
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, manifest.getString());
  manifest.end();
  if (err) {
    copyInto(status, sizeof(status), "bad manifest");
    return OTA_FAILED;
  }

  const char* build = doc["build"] | "";
  size_t      size  = doc["size"] | 0;
  if (!build[0] || !size) {
    copyInto(status, sizeof(status), "empty manifest");
    return OTA_FAILED;
  }
  if (strcmp(build, otaRunningBuild()) == 0) {
    copyInto(status, sizeof(status), "up to date");
    return OTA_UP_TO_DATE;
  }

  HTTPClient image;
  if (!get(image, "/device/ota/firmware.bin", MANIFEST_TIMEOUT_MS)) {
    image.end();
    copyInto(status, sizeof(status), "download refused");
    return OTA_UNREACHABLE;
  }
  // Update.writeStream() reads through Stream::readBytes on the raw WiFiClient,
  // whose setTimeout takes SECONDS — and HTTPClient::setTimeout (milliseconds)
  // only reaches the socket when already connected, so it must be set here.
  WiFiClient* stream = image.getStreamPtr();
  if (!stream) {
    image.end();
    copyInto(status, sizeof(status), "no stream");
    return OTA_FAILED;
  }
  stream->setTimeout(STREAM_TIMEOUT_S);
  int firstByte = stream->peek();
  int declared  = image.getSize();

  if (!Update.begin(size)) {
    image.end();
    copyInto(status, sizeof(status), Update.errorString());
    return OTA_FAILED;
  }
  markAttempt(true);
  size_t written = Update.writeStream(*stream);
  image.end();
  if (written != size || !Update.end(true)) {
    snprintf(status, sizeof(status), "%s b0=%02x len=%d w=%u",
             Update.errorString(), firstByte & 0xff, declared, (unsigned)written);
    Update.abort();
    markAttempt(false);
    return OTA_FAILED;
  }

  markAttempt(false);
  Serial.printf("[ota] %u bytes -> %.16s, rebooting\n", (unsigned)written, build);
  esp_restart();
  return OTA_UP_TO_DATE;   // unreachable
}
