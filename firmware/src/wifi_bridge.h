#pragma once
#include <stdint.h>
#include <stddef.h>

// Long-poll client for the laptop broker. The device dials out and never
// listens for approvals, so there is no IP to discover from the laptop side and
// the same connection carries control commands upward.
//
//   GET  /device/poll?dev=&cols=&wait=   held open; returns one snapshot line
//   POST /device/verdict                 the answer to a card, by its id
//   POST /device/control                 stop / respawn / rm / logs / say
//
// Poll bodies are pushed into a ring buffer that dataPoll() drains through the
// same _applyJson path as USB and BLE, so the snapshot shape is unchanged.
// Outgoing writes go through wifiWrite(), which is what sendCmd() already
// calls — answering a card needed no change to the UI code.
//
// GET :81/health and :81/debug stay served locally; they are the only way to
// observe this board, which has no usable USB console.

void wifiInit(const char* ssid, const char* pass, const char* brokerHost,
              uint16_t brokerPort, const char* brokerToken);
bool wifiConnected();
const char* wifiIP();
uint16_t wifiRSSI();

size_t wifiAvailable();
int wifiRead();
size_t wifiWrite(const uint8_t* data, size_t len);

// Queues a control command for POST /device/control. Non-blocking: the UI task
// must never park on the network.
void wifiControl(const char* json);

// Last result line from a control command, for the UI to show.
const char* wifiControlResult();

// Called by the UI drain after each applied line so GET /debug can report what
// the parser actually saw.
void wifiNoteRx(size_t len, const char* line, const char* promptId,
                uint8_t promptKind, uint8_t nOpts, const char* promptTool);
