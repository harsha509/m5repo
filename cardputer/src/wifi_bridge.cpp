#include "wifi_bridge.h"

#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESPmDNS.h>
#include <WebServer.h>
#include <WiFi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>
#include <string.h>
#include <ctype.h>

static const uint32_t APPROVAL_TIMEOUT_MS = 60000;
static const size_t RX_CAP = 4096;
static const uint16_t HTTP_PORT = 80;    // /approve — may block while a prompt is up
static const uint16_t STATE_PORT = 81;   // /state, /health — never blocks

static uint8_t rxBuf[RX_CAP];
static volatile size_t rxHead = 0;
static volatile size_t rxTail = 0;
static portMUX_TYPE rxMux = portMUX_INITIALIZER_UNLOCKED;

static WebServer server(HTTP_PORT);
static WebServer stateServer(STATE_PORT);
static char authToken[64] = "";
static char ipStr[20] = "0.0.0.0";

// The single in-flight approval. The HTTP task parks on `decided` while the UI
// task renders the prompt; wifiWrite() releases it when the verdict is emitted.
static SemaphoreHandle_t decided = nullptr;
static char pendingId[64] = "";
static volatile bool pendingApproved = false;
static char pendingReason[96] = "";
static char pendingAnswer[128] = "";
static volatile bool pendingIsAsk = false;   // a bare "once" must not answer a question

// Last line the UI drain applied, for GET /debug. Written on the UI task,
// read on the state task; a torn read only mis-reports, never faults.
static struct {
  uint32_t count = 0, atMs = 0;
  size_t   len = 0;
  char     head[96] = "";
  char     promptId[40] = "", promptTool[20] = "";
  uint8_t  promptKind = 0, nOpts = 0;
} rxDbg;

void wifiNoteRx(size_t len, const char* line, const char* promptId,
                uint8_t promptKind, uint8_t nOpts, const char* promptTool) {
  rxDbg.count++; rxDbg.atMs = millis(); rxDbg.len = len;
  strncpy(rxDbg.head, line, sizeof(rxDbg.head) - 1); rxDbg.head[sizeof(rxDbg.head) - 1] = 0;
  strncpy(rxDbg.promptId, promptId, sizeof(rxDbg.promptId) - 1); rxDbg.promptId[sizeof(rxDbg.promptId) - 1] = 0;
  strncpy(rxDbg.promptTool, promptTool, sizeof(rxDbg.promptTool) - 1); rxDbg.promptTool[sizeof(rxDbg.promptTool) - 1] = 0;
  rxDbg.promptKind = promptKind; rxDbg.nOpts = nOpts;
  Serial.printf("[wifi] rx %u: %.90s\n", (unsigned)len, line);
}

// Clears the prompt once a request finishes. strlen, never a literal count:
// a short count drops the newline and glues the next line onto this one,
// which makes both unparseable.
static void rxPush(const uint8_t* p, size_t n);
static void pushIdle() {
  static const char IDLE[] = "{\"total\":1,\"running\":1,\"waiting\":0,\"msg\":\"idle\"}\n";
  rxPush((const uint8_t*)IDLE, strlen(IDLE));
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

/// Resolves the in-flight approval when main.cpp emits a matching verdict.
size_t wifiWrite(const uint8_t* data, size_t len) {
  if (!len || pendingId[0] == '\0') return len;
  JsonDocument doc;
  if (deserializeJson(doc, data, len)) return len;
  const char* cmd = doc["cmd"] | "";
  if (strcmp(doc["id"] | "", pendingId) != 0) return len;
  if (strcmp(cmd, "answer") == 0) {
    // A picked (or typed) option for a question prompt.
    pendingApproved = true;
    strncpy(pendingAnswer, doc["a"] | "", sizeof(pendingAnswer) - 1);
    pendingAnswer[sizeof(pendingAnswer) - 1] = '\0';
    xSemaphoreGive(decided);
    return len;
  }
  if (strcmp(cmd, "permission") != 0) return len;
  // The stock Enter path emits "once" on key release, one frame after the
  // keyboard handler already answered; on a question that would blank the
  // next one. Only an explicit answer (or a deny) resolves a question.
  if (pendingIsAsk && strcmp(doc["decision"] | "", "once") == 0) return len;
  pendingApproved = strcmp(doc["decision"] | "", "once") == 0;
  const char* why = doc["reason"] | "";
  strncpy(pendingReason, why, sizeof(pendingReason) - 1);
  pendingReason[sizeof(pendingReason) - 1] = '\0';
  xSemaphoreGive(decided);
  return len;
}

// Only these reach the screen; everything else is answered instantly with no
// decision, so Claude Code's normal flow auto-approves it.
static const char* GATED[] = {
    "git push", "git commit", "git reset --hard", "rm -rf", "rm -f",
    "--force", "-force", "drop table", "truncate table", "prod",
    "kubectl delete", "terraform destroy", "terraform apply", "aws s3 rm",
    "shutdown", "mkfs", "dd if=",
};

static bool containsFold(const char* haystack, const char* needle) {
  for (const char* h = haystack; *h; h++) {
    const char *a = h, *b = needle;
    while (*a && *b && tolower((unsigned char)*a) == tolower((unsigned char)*b)) { a++; b++; }
    if (!*b) return true;
  }
  return false;
}

static bool shouldGate(const char* cmd) {
  if (!cmd || !*cmd) return false;
  for (size_t i = 0; i < sizeof(GATED) / sizeof(GATED[0]); i++) {
    if (containsFold(cmd, GATED[i])) return true;
  }
  return false;
}

static bool authorized(WebServer& w) {
  if (authToken[0] == '\0') return true;
  String header = w.header("Authorization");
  return header == String("Bearer ") + authToken;
}

/// Walks an AskUserQuestion's questions one at a time on the device, then
/// answers the tool through updatedInput so it never reaches the terminal.
/// Each question gets its own composite id so the UI treats it as a new prompt.
static void handleAsk(JsonDocument& in, const char* id) {
  JsonArray questions = in["tool_input"]["questions"];
  if (questions.isNull() || questions.size() == 0) { server.send(200, "application/json", "{}"); return; }

  JsonDocument answers;
  uint8_t nq = questions.size(), qi = 0;
  pendingIsAsk = true;
  for (JsonVariant qv : questions) {
    const char* q = qv["question"] | "";
    JsonDocument snap;
    snap["total"] = 1; snap["running"] = 0; snap["waiting"] = 1;
    snap["msg"] = "question";
    JsonObject prompt = snap["prompt"].to<JsonObject>();
    char qid[80];
    snprintf(qid, sizeof(qid), "%s#%u", id, qi);
    prompt["id"] = qid;
    prompt["tool"] = qv["header"] | "question";
    prompt["kind"] = "ask";
    prompt["hint"] = q;
    prompt["qi"] = qi; prompt["nq"] = nq;
    JsonArray opts = prompt["opts"].to<JsonArray>();
    for (JsonVariant o : qv["options"].as<JsonArray>()) opts.add(o["label"] | "");

    pendingAnswer[0] = 0; pendingReason[0] = 0;
    strncpy(pendingId, qid, sizeof(pendingId) - 1);
    pendingId[sizeof(pendingId) - 1] = '\0';
    xSemaphoreTake(decided, 0);
    String line; serializeJson(snap, line); line += "\n";
    rxPush((const uint8_t*)line.c_str(), line.length());

    bool answered = xSemaphoreTake(decided, pdMS_TO_TICKS(APPROVAL_TIMEOUT_MS)) == pdTRUE;
    bool ok = pendingApproved;
    pendingId[0] = '\0';
    if (!answered) { break; }
    if (!ok) {
      pendingIsAsk = false;
      pushIdle();
      server.send(200, "application/json",
        "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"deny\",\"permissionDecisionReason\":\"declined on Cardputer\"}}");
      return;
    }
    answers[q] = pendingAnswer;
    qi++;
  }
  pendingIsAsk = false;
  pushIdle();

  // Timeout mid-way: no decision, the terminal asks instead.
  if (qi < nq) { server.send(200, "application/json", "{}"); return; }

  JsonDocument out;
  JsonObject hook = out["hookSpecificOutput"].to<JsonObject>();
  hook["hookEventName"] = "PreToolUse";
  hook["permissionDecision"] = "allow";
  hook["permissionDecisionReason"] = "answered on Cardputer";
  JsonObject ui = hook["updatedInput"].to<JsonObject>();
  ui.set(in["tool_input"].as<JsonObjectConst>());
  ui["answers"] = answers.as<JsonObject>();
  String reply; serializeJson(out, reply);
  server.send(200, "application/json", reply);
}

/// Feeds the hook payload to the UI, then blocks until the keypress or timeout.
static void handleApprove() {
  if (!authorized(server)) { server.send(401, "application/json", "{}"); return; }
  String body = server.arg("plain");

  JsonDocument in;
  if (deserializeJson(in, body)) { server.send(400, "application/json", "{}"); return; }
  const char* id = in["tool_use_id"] | "";
  if (!id[0]) { server.send(200, "application/json", "{}"); return; }

  if (strcmp(in["tool_name"] | "", "AskUserQuestion") == 0) { handleAsk(in, id); return; }

  const char* cmd = in["tool_input"]["command"] | "";
  if (!shouldGate(cmd)) { server.send(200, "application/json", "{}"); return; }

  JsonDocument snap;
  snap["total"] = 1;
  snap["running"] = 0;
  snap["waiting"] = 1;
  snap["msg"] = String("approve: ") + (in["tool_name"] | "tool");
  JsonObject prompt = snap["prompt"].to<JsonObject>();
  prompt["id"] = id;
  prompt["tool"] = in["tool_name"] | "tool";
  prompt["hint"] = in["tool_input"]["command"] | (in["tool_input"]["file_path"] | "");

  // Show the project folder rather than the whole path — the leaf is what
  // identifies which repo this is.
  const char* cwd = in["cwd"] | "";
  const char* leaf = strrchr(cwd, '/');
  prompt["cwd"] = leaf && leaf[1] ? leaf + 1 : cwd;
  prompt["sess"] = in["permission_mode"] | "";

  pendingReason[0] = 0; pendingIsAsk = false;
  strncpy(pendingId, id, sizeof(pendingId) - 1);
  pendingId[sizeof(pendingId) - 1] = '\0';
  xSemaphoreTake(decided, 0);

  String line;
  serializeJson(snap, line);
  line += "\n";
  rxPush((const uint8_t*)line.c_str(), line.length());

  bool answered = xSemaphoreTake(decided, pdMS_TO_TICKS(APPROVAL_TIMEOUT_MS)) == pdTRUE;
  bool approved = pendingApproved;
  pendingId[0] = '\0';

  pushIdle();

  // Silence means no decision, so Claude Code falls back to the terminal prompt.
  if (!answered) { server.send(200, "application/json", "{}"); return; }

  JsonDocument out;
  JsonObject hook = out["hookSpecificOutput"].to<JsonObject>();
  hook["hookEventName"] = "PreToolUse";
  hook["permissionDecision"] = approved ? "allow" : "deny";
  // A typed reason reaches Claude verbatim, so a denial can explain itself.
  if (!approved && pendingReason[0]) {
    hook["permissionDecisionReason"] = pendingReason;
  } else {
    hook["permissionDecisionReason"] =
        approved ? "approved on Cardputer" : "denied on Cardputer";
  }
  String reply;
  serializeJson(out, reply);
  server.send(200, "application/json", reply);
}

static void handleState() {
  if (!authorized(stateServer)) { stateServer.send(401, "application/json", "{}"); return; }
  // An agent snapshot has no `prompt`, which the parser reads as "clear it".
  // While an approval is in flight, acknowledge but don't apply, so the
  // 5 s state feed can't wipe the question off the screen.
  if (pendingId[0]) { stateServer.send(200, "application/json", "{\"ok\":true,\"deferred\":true}"); return; }
  String body = stateServer.arg("plain");
  body += "\n";
  rxPush((const uint8_t*)body.c_str(), body.length());
  stateServer.send(200, "application/json", "{\"ok\":true}");
}

static void sendHealth(WebServer& w) {
  JsonDocument doc;
  doc["ok"] = true;
  doc["ip"] = ipStr;
  doc["rssi"] = WiFi.RSSI();
  doc["approvePort"] = HTTP_PORT;
  doc["statePort"] = STATE_PORT;
  String out;
  serializeJson(doc, out);
  w.send(200, "application/json", out);
}
static void handleHealth()      { sendHealth(server); }
static void handleStateHealth() { sendHealth(stateServer); }

static void handleDebug() {
  JsonDocument doc;
  doc["uptimeMs"]   = millis();
  doc["pendingId"]  = pendingId;
  doc["pendingAsk"] = (bool)pendingIsAsk;
  doc["rxQueued"]   = wifiAvailable();
  JsonObject last = doc["lastApplied"].to<JsonObject>();
  last["count"] = rxDbg.count; last["agoMs"] = millis() - rxDbg.atMs; last["len"] = rxDbg.len;
  last["head"] = rxDbg.head;
  JsonObject pr = doc["uiPrompt"].to<JsonObject>();
  pr["id"] = rxDbg.promptId; pr["kind"] = rxDbg.promptKind; pr["opts"] = rxDbg.nOpts; pr["tool"] = rxDbg.promptTool;
  String out; serializeJson(doc, out);
  stateServer.send(200, "application/json", out);
}

/// Owns the approval server. A prompt parks this task for up to the timeout,
/// which is why state and health live on their own server below.
static void httpTask(void*) {
  const char* headers[] = {"Authorization"};
  server.collectHeaders(headers, 1);
  server.on("/approve", HTTP_POST, handleApprove);
  server.on("/health", HTTP_GET, handleHealth);
  server.begin();
  for (;;) {
    server.handleClient();
    vTaskDelay(pdMS_TO_TICKS(2));
  }
}

/// Non-blocking server: the laptop agent's 5 s state pushes and health probes
/// keep working while an approval is waiting on a keypress.
static void stateTask(void*) {
  const char* headers[] = {"Authorization"};
  stateServer.collectHeaders(headers, 1);
  stateServer.on("/state", HTTP_POST, handleState);
  stateServer.on("/health", HTTP_GET, handleStateHealth);
  stateServer.on("/debug",  HTTP_GET, handleDebug);
  stateServer.begin();
  for (;;) {
    stateServer.handleClient();
    vTaskDelay(pdMS_TO_TICKS(2));
  }
}

void wifiInit(const char* ssid, const char* pass, const char* token) {
  if (token) {
    strncpy(authToken, token, sizeof(authToken) - 1);
    authToken[sizeof(authToken) - 1] = '\0';
  }
  decided = xSemaphoreCreateBinary();

  // Modem sleep must stay enabled: the ESP32 aborts if WiFi and BLE share the
  // radio without it.
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, pass);
  for (int i = 0; i < 60 && WiFi.status() != WL_CONNECTED; i++) delay(250);

  if (WiFi.status() == WL_CONNECTED) {
    strncpy(ipStr, WiFi.localIP().toString().c_str(), sizeof(ipStr) - 1);
    // Publish claude.local so the laptop keeps working across DHCP changes.
    if (MDNS.begin("claude")) MDNS.addService("http", "tcp", HTTP_PORT);
    Serial.printf("[wifi] %s  http://%s/  http://claude.local/\n", ssid, ipStr);
    xTaskCreatePinnedToCore(httpTask,  "httpd",  8192, nullptr, 1, nullptr, 0);
    xTaskCreatePinnedToCore(stateTask, "stated", 6144, nullptr, 1, nullptr, 0);
  } else {
    Serial.println("[wifi] connect failed");
  }
}

bool wifiConnected() { return WiFi.status() == WL_CONNECTED; }
const char* wifiIP() { return ipStr; }
uint16_t wifiRSSI() { return (uint16_t)abs((int)WiFi.RSSI()); }
