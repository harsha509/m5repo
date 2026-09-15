# CLAUDE.md

M5Stack Cardputer as a physical approval and monitoring surface for the Claude
Code **CLI**. See README.md for user-facing setup; this file is the working
context for editing the code.

## Build, flash, verify

```bash
.venv/bin/pio run -e cardputer-adv -d firmware                       # build
.venv/bin/pio run -e cardputer-adv -d firmware -t upload \
  --upload-port /dev/cu.usbmodem101                                  # flash
.venv/bin/python -m bridge.broker.server --token <BROKER_TOKEN>      # broker
.venv/bin/python -m bridge.broker.selftest                           # checks
curl http://127.0.0.1:8787/health                                    # state
```

The device is at `192.168.0.170` (MAC `80:45:6b:77:5b:44`). After a flash it
takes ~12 s to rejoin WiFi — always sleep before probing.

**Confirm the board before flashing.** More than one ESP32-S3 may be attached
and the port numbers move. `ioreg -p IOUSB -l -w 0 | grep "USB Serial Number"`
gives each board's MAC; only `80:45:6b:77:5b:44` is the Cardputer. Always pass
`--upload-port` explicitly.

## Architecture invariants

Break these and things fail in confusing ways.

- **The device runs no server.** It long-polls `GET /device/poll` on the broker
  and answers with `POST /device/verdict` / `/device/control`. There is no
  inbound port, no mDNS, no IP for the laptop to discover.
- **`{}` means "no decision", not "deny".** Every timeout, every non-escalated
  tool and every broker exception returns it, and Claude Code applies its normal
  rules. This is what makes the whole system fail open.
- **All policy lives in `bridge/policy.json`**, hot-reloaded on mtime. Changing
  what prompts is a JSON edit, never a recompile. A malformed file keeps the
  last good rules rather than opening the gate.
- **All wrapping lives in `cards.py`.** Lines arrive pre-wrapped to the polling
  device's `cols`, so the firmware parses no tool JSON and holds no layout.
- **A snapshot without a `prompt` field clears the prompt.** `Registry.pending_for()`
  re-sends an unresolved card on every idle poll for exactly this reason.
- **Session actions are gated on `bg`.** `claude agents --json` lists interactive
  sessions, but `logs`/`stop`/`respawn` only address background jobs and answer
  "No job matching" for the rest. The firmware fails closed: a snapshot with no
  `bg` key leaves every row read-only, so firmware and broker must move together.
- **BLE is untouched.** Both transports feed the same `_applyJson` in data.h.
  Don't "simplify" by removing the BLE path.

## Hard-won gotchas

Each of these cost real debugging time.

- **`Stream::timedRead()` busy-loops without yielding.** Using `readBytes*` on a
  poll the broker holds open starves IDLE0 and the task watchdog reboots the
  board. `waitData()`/`readLine()` in wifi_bridge.cpp yield with `vTaskDelay`.
- **`WiFiClient::setTimeout()` takes SECONDS**; `HTTPClient::setTimeout()` takes
  ms and only reaches the socket once already connected.
- **The sprite needs 64,800 contiguous bytes** (240x135x2). Claim it *before*
  `startBt()` — BLE fragments the heap and `createSprite` then fails silently,
  giving a blank screen. `spriteReady` guards the render and retries.
- **Enter fires both `halBtnA()` and `HalKey::Approve`**, and the BtnA handler
  runs earlier in `loop()`. Any new modal surface must be excluded from the BtnA
  branches or Enter acts twice. See `onSessionsPage()`.
- **Never `WiFi.setSleep(false)`.** With BLE also running the ESP32 aborts and
  boot-loops: `Should enable WiFi modem sleep when both WiFi and Bluetooth are
  enabled`.
- **Never set `-DARDUINO_USB_CDC_ON_BOOT=1`.** `Serial.printf` then blocks when
  no host reads the port. There is effectively **no usable USB console**.
- **This board has no PSRAM**, despite `-DBOARD_HAS_PSRAM` in platformio.ini
  (inherited from the StampS3 board def). The boot PSRAM probe failure is
  harmless but means less headroom than the flag implies.
- **OTA does not work.** The partition table has two 3 MB slots and the broker
  serves a manifest, but three transfer attempts died partway. Boot-time
  checking is removed; `Settings > update` remains. Iterate over the cable.

## Debugging

The board has no inbound server, so `GET /health` on the **broker** is the only
observation point. Telemetry rides the poll query string:

- `polls` climbing with `applied` flat → lines arrive but fail to parse.
- `sprite: false` → `createSprite` lost the heap race; screen is blank.
- `heapBlk` falling toward 64,800 → fragmentation will kill the next sprite.
- `resetReason: task-watchdog` → something blocked without yielding.

## Testing

Approvals need a human keypress, so end-to-end tests cannot be fully automated.
`selftest.py` covers everything up to the keypress, including two concurrent
approvals answered in reverse order. For the rest: fire the request in the
background, ask the user to press the key, then read the saved response.

## Conventions

- Firmware follows the upstream style: terse function-level comments, no
  per-line commentary.
- `wifi_config.h` is gitignored and holds real credentials; edit
  `wifi_config.h.template` when adding fields.
- The broker is standard library only. No new Python dependency.

## Upstream

Forked from `y88huang/claude-desktop-buddy-cardputer`, itself a port of
`anthropics/claude-desktop-buddy` (MIT, see LICENSE). The BLE protocol is
documented in `firmware/REFERENCE.md`; the WiFi transport carries the same
newline-delimited JSON, so that document describes both.
