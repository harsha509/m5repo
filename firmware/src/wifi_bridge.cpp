#include "wifi_bridge.h"

#include <Arduino.h>
#include <ArduinoJson.h>
#include <WiFi.h>
#include <esp_system.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>
#include <string.h>

// This board has ~10KB of heap left once the 240x135 sprite, the BLE stack and
// WiFi have taken theirs, so there is no room for HTTPClient (an Arduino String
// per header) or an inbound WebServer. Requests are written by hand over a raw
// WiFiClient, and the telemetry that used to be served on :81/debug now rides
// the poll's query string — a channel that keeps working even when the device's
// inbound path does not.

static const size_t   RX_CAP      = 4096;
static const uint16_t POLL_WAIT_S = 10;     // how long the broker holds a poll
static const uint16_t CONNECT_MS  = 4000;
static const uint8_t  OUTBOX_N    = 6;
static const uint16_t OUT_BODY    = 288;

static uint8_t rxBuf[RX_CAP];
static volatile size_t rxHead = 0;
static volatile size_t rxTail = 0;
static portMUX_TYPE rxMux = portMUX_INITIALIZER_UNLOCKED;

static char     brokerHost[64] = "";
static uint16_t brokerPort = 8787;
static char     brokerToken[64] = "";
static char     ipStr[20] = "0.0.0.0";
static char     ctrlResult[64] = "";

extern bool spriteReady;   // owned by main.cpp; false means the UI draws nowhere

struct OutMsg {
  char path[24];
  char body[OUT_BODY];
};
static OutMsg outbox[OUTBOX_N];
static volatile uint8_t outHead = 0, outTail = 0;
static SemaphoreHandle_t outLock = nullptr;
static SemaphoreHandle_t outWake = nullptr;

static struct {
  uint32_t applied = 0, polls = 0, pollFails = 0;
  char     promptId[40] = "";
} tel;

void wifiNoteRx(size_t len, const char* line, const char* promptId,
                uint8_t promptKind, uint8_t nOpts, const char* promptTool) {
  (void)len; (void)line; (void)promptKind; (void)nOpts; (void)promptTool;
  tel.applied++;
  strncpy(tel.promptId, promptId, sizeof(tel.promptId) - 1);
  tel.promptId[sizeof(tel.promptId) - 1] = 0;
}

static void rxPush(const uint8_t* p, size_t n) {
  portENTER_CRITICAL(&rxMux);
  for (size_t i = 0; i < n; i++) {
    size_t next = (rxHead + 1) % RX_CAP;
    if (next == rxTail) break;
    rxBuf[rxHead] = p[i];
    rxHead = next;
  }
  portEXIT_CRITICAL(&rxMux);
}

// Clears the prompt the instant an answer is sent, rather than leaving an
// answered question on screen until the next poll returns. strlen, never a
// literal count: a short count drops the newline and glues the next line on.
static void pushIdle() {
  static const char IDLE[] = "{\"total\":1,\"running\":1,\"waiting\":0,\"msg\":\"sent\"}\n";
  rxPush((const uint8_t*)IDLE, strlen(IDLE));
}

size_t wifiAvailable() {
  portENTER_CRITICAL(&rxMux);
  size_t n = (rxHead + RX_CAP - rxTail) % RX_CAP;
  portEXIT_CRITICAL(&rxMux);
  return n;
}

int wifiRead() {
  int c = -1;
  portENTER_CRITICAL(&rxMux);
  if (rxTail != rxHead) {
    c = rxBuf[rxTail];
    rxTail = (rxTail + 1) % RX_CAP;
  }
  portEXIT_CRITICAL(&rxMux);
  return c;
}

/// Blocks until the socket has data, yielding between checks. Stream::readBytes*
/// cannot be used here: its timedRead() busy-loops without yielding, so a poll
/// the broker holds for seconds starves IDLE0 and the task watchdog reboots us.
static bool waitData(WiFiClient& client, uint32_t timeoutMs) {
  uint32_t start = millis();
  while (!client.available()) {
    if (!client.connected() || millis() - start > timeoutMs) return false;
    vTaskDelay(pdMS_TO_TICKS(10));
  }
  return true;
}

static int readLine(WiFiClient& client, char* buf, size_t cap, uint32_t timeoutMs) {
  size_t n = 0;
  for (;;) {
    if (!waitData(client, timeoutMs)) return -1;
    int ch = client.read();
    if (ch < 0) continue;
    if (ch == '\n') break;
    if (n < cap - 1) buf[n++] = (char)ch;
  }
  buf[n] = 0;
  return (int)n;
}

/// One request, no dynamic allocation beyond the socket itself. Returns false
/// if the connection never opened; `status` carries the HTTP code otherwise.
static bool httpRequest(const char* method, const char* path, const char* body,
                        char* out, size_t outCap, int* status, uint32_t timeoutMs) {
  *status = 0;
  if (out && outCap) out[0] = 0;

  WiFiClient client;
  if (!client.connect(brokerHost, brokerPort, CONNECT_MS)) return false;

  char req[512];
  int n = snprintf(req, sizeof(req),
                   "%s %s HTTP/1.1\r\nHost: %s:%u\r\nConnection: close\r\n",
                   method, path, brokerHost, brokerPort);
  if (brokerToken[0] && n > 0 && n < (int)sizeof(req)) {
    n += snprintf(req + n, sizeof(req) - n, "Authorization: Bearer %s\r\n", brokerToken);
  }
  if (body && n > 0 && n < (int)sizeof(req)) {
    n += snprintf(req + n, sizeof(req) - n,
                  "Content-Type: application/json\r\nContent-Length: %u\r\n",
                  (unsigned)strlen(body));
  }
  if (n > 0 && n < (int)sizeof(req)) n += snprintf(req + n, sizeof(req) - n, "\r\n");
  if (n <= 0 || n >= (int)sizeof(req)) { client.stop(); return false; }

  client.write((const uint8_t*)req, n);
  if (body) client.write((const uint8_t*)body, strlen(body));

  char line[160];
  int len = readLine(client, line, sizeof(line), timeoutMs);
  if (len <= 0) { client.stop(); return false; }
  const char* space = strchr(line, ' ');
  *status = space ? atoi(space + 1) : 0;

  int contentLen = -1;
  for (;;) {
    len = readLine(client, line, sizeof(line), timeoutMs);
    if (len <= 1) break;                     // bare CR ends the headers
    if (strncasecmp(line, "Content-Length:", 15) == 0) contentLen = atoi(line + 15);
  }

  size_t got = 0;
  if (out && outCap > 1) {
    size_t want = contentLen > 0 ? (size_t)contentLen : outCap - 1;
    if (want > outCap - 1) want = outCap - 1;
    while (got < want) {
      if (!waitData(client, timeoutMs)) break;
      int r = client.read((uint8_t*)out + got, want - got);
      if (r > 0) got += r;
    }
    out[got] = 0;
  }
  client.stop();
  return true;
}

static void enqueue(const char* path, const char* body, size_t len) {
  if (!outLock) return;
  xSemaphoreTake(outLock, portMAX_DELAY);
  uint8_t next = (outHead + 1) % OUTBOX_N;
  if (next != outTail) {
    strncpy(outbox[outHead].path, path, sizeof(outbox[0].path) - 1);
    outbox[outHead].path[sizeof(outbox[0].path) - 1] = 0;
    size_t n = len < OUT_BODY - 1 ? len : OUT_BODY - 1;
    memcpy(outbox[outHead].body, body, n);
    outbox[outHead].body[n] = 0;
    outHead = next;
  }
  xSemaphoreGive(outLock);
  if (outWake) xSemaphoreGive(outWake);
}

/// Called by sendCmd(). Queues the verdict and clears the prompt locally, so
/// the screen never holds a question that has already been answered.
size_t wifiWrite(const uint8_t* data, size_t len) {
  if (!len) return len;
  JsonDocument doc;
  if (deserializeJson(doc, data, len)) return len;
  const char* cmd = doc["cmd"] | "";
  if (strcmp(cmd, "permission") != 0 && strcmp(cmd, "answer") != 0) return len;
  enqueue("/device/verdict", (const char*)data, len);
  pushIdle();
  return len;
}

void wifiControl(const char* json) {
  enqueue("/device/control", json, strlen(json));
}

const char* wifiControlResult() { return ctrlResult; }

/// Holds a request open while the broker waits for something to happen, then
/// feeds the body through the same newline-JSON path as USB and BLE. Device
/// telemetry rides the query string so the broker can report it.
static char pollBuf[1400];

static void pollTask(void*) {
  char path[360];
  for (;;) {
    if (WiFi.status() != WL_CONNECTED) { vTaskDelay(pdMS_TO_TICKS(2000)); continue; }
    snprintf(path, sizeof(path),
             "/device/poll?dev=cardputer&cols=40&wait=%u"
             "&heap=%u&blk=%u&spr=%d&polls=%u&applied=%u&fails=%u&up=%u&rst=%d&prompt=%s",
             POLL_WAIT_S, (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getMaxAllocHeap(),
             spriteReady ? 1 : 0, (unsigned)tel.polls, (unsigned)tel.applied,
             (unsigned)tel.pollFails, (unsigned)(millis() / 1000),
             (int)esp_reset_reason(), tel.promptId[0] ? tel.promptId : "-");

    int status = 0;
    bool ok = httpRequest("GET", path, nullptr, pollBuf, sizeof(pollBuf), &status,
                          (POLL_WAIT_S + 8) * 1000);
    tel.polls++;
    if (ok && status == 200 && pollBuf[0] == '{') {
      size_t n = strlen(pollBuf);
      pollBuf[n] = '\n';
      rxPush((const uint8_t*)pollBuf, n + 1);
    } else {
      tel.pollFails++;
      vTaskDelay(pdMS_TO_TICKS(3000));
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

/// Drains the outbox. Separate from the poller because a poll is parked for up
/// to POLL_WAIT_S and a verdict must not wait behind it.
static void senderTask(void*) {
  static char reply[256];
  for (;;) {
    xSemaphoreTake(outWake, pdMS_TO_TICKS(1000));
    while (outTail != outHead) {
      xSemaphoreTake(outLock, portMAX_DELAY);
      OutMsg msg = outbox[outTail];
      outTail = (outTail + 1) % OUTBOX_N;
      xSemaphoreGive(outLock);

      if (WiFi.status() != WL_CONNECTED) continue;
      int status = 0;
      bool ok = httpRequest("POST", msg.path, msg.body, reply, sizeof(reply), &status, 20000);
      if (ok && status == 200) {
        JsonDocument doc;
        if (!deserializeJson(doc, reply)) {
          const char* result = doc["result"] | "";
          if (result[0]) {
            strncpy(ctrlResult, result, sizeof(ctrlResult) - 1);
            ctrlResult[sizeof(ctrlResult) - 1] = 0;
          }
        }
      } else {
        snprintf(ctrlResult, sizeof(ctrlResult), "send failed (%d)", status);
      }
    }
  }
}

void wifiInit(const char* ssid, const char* pass, const char* host,
              uint16_t port, const char* token) {
  strncpy(brokerHost, host ? host : "", sizeof(brokerHost) - 1);
  brokerHost[sizeof(brokerHost) - 1] = 0;
  strncpy(brokerToken, token ? token : "", sizeof(brokerToken) - 1);
  brokerToken[sizeof(brokerToken) - 1] = 0;
  if (port) brokerPort = port;

  outLock = xSemaphoreCreateMutex();
  outWake = xSemaphoreCreateBinary();

  // Modem sleep must stay enabled: the ESP32 aborts if WiFi and BLE share the
  // radio without it.
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, pass);
  for (int i = 0; i < 60 && WiFi.status() != WL_CONNECTED; i++) delay(250);

  if (WiFi.status() == WL_CONNECTED) {
    strncpy(ipStr, WiFi.localIP().toString().c_str(), sizeof(ipStr) - 1);
    xTaskCreatePinnedToCore(pollTask,   "poll", 8192, nullptr, 1, nullptr, 0);
    xTaskCreatePinnedToCore(senderTask, "send", 6144, nullptr, 1, nullptr, 0);
  }
}

bool wifiConnected() { return WiFi.status() == WL_CONNECTED; }
const char* wifiIP() { return ipStr; }
uint16_t wifiRSSI() { return (uint16_t)abs((int)WiFi.RSSI()); }
