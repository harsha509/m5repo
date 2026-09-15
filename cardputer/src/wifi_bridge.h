#pragma once
#include <stdint.h>
#include <stddef.h>

// WiFi transport carrying the same newline-delimited JSON as the BLE bridge,
// so Claude Code's PreToolUse http hook can POST this device directly with no
// laptop-side daemon in the approval path.
//
//   POST /approve   hook payload in, permission verdict out (blocks until you
//                   press Y/N on the device, or the timeout expires)
//   POST /state     sessions + usage snapshot, answered immediately
//   GET  /health    liveness probe
//
// Incoming bodies are pushed into a ring buffer that dataPoll() drains through
// the same _applyJson path as USB and BLE. Outgoing writes are inspected by
// wifiWrite(): a permission verdict matching the in-flight request resolves it.

void wifiInit(const char* ssid, const char* pass, const char* token);
bool wifiConnected();
const char* wifiIP();
uint16_t wifiRSSI();

size_t wifiAvailable();
int wifiRead();
size_t wifiWrite(const uint8_t* data, size_t len);

// Called by the UI drain after each applied line so GET /debug (port 81) can
// report what the parser actually saw — observable without a USB console.
void wifiNoteRx(size_t len, const char* line, const char* promptId,
                uint8_t promptKind, uint8_t nOpts, const char* promptTool);
