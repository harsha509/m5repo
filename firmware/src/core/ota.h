#pragma once
#include <stdint.h>

// Pulls firmware from the broker over WiFi so iteration stops needing a cable.
// Build identity is the running app's app_elf_sha256 compared against the one
// the broker parses out of the served .bin, so nothing is tracked in NVS.

enum OtaResult : uint8_t { OTA_UP_TO_DATE, OTA_UNREACHABLE, OTA_FAILED };

void otaInit(const char* host, uint16_t port, const char* token);

// Blocking; downloads and reboots into the new image, so a successful update
// never returns. `fromMenu` clears the guard that holds off the boot-time check
// after an attempt that never finished.
OtaResult otaCheck(bool fromMenu);

const char* otaRunningBuild();
const char* otaStatus();
