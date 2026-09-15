# CLAUDE.md

M5Stack hardware as a physical approval and monitoring surface for the Claude
Code **CLI**. See README.md for user-facing setup; this file is the working
context for editing the code.

## Build, flash, verify

```bash
.venv/bin/pio run -e cardputer-adv -d cardputer                      # build
.venv/bin/pio run -e cardputer-adv -d cardputer -t upload \
  --upload-port /dev/cu.usbmodem101                                  # flash
curl http://192.168.0.170:81/health                                  # alive?
curl http://192.168.0.170:81/debug                                   # parser state
.venv/bin/python bridge/m5agent.py --device 192.168.0.170 --verbose  # state feed
```

The device is at `192.168.0.170` (MAC `80:45:6b:77:5b:44`). After a flash it
takes ~12 s to rejoin WiFi — always sleep before probing.

## Architecture invariants

Break these and things fail in confusing ways.

- **Two ports, deliberately.** `/approve` on **:80** blocks for up to 60 s
  while a prompt waits for a keypress. `/state`, `/health`, `/debug` live on
  **:81** and must never block, or the info windows freeze during a prompt.
- **`{}` means "no decision", not "deny".** Claude Code then applies its normal
  rules. Every timeout and every non-gated command returns `{}`. This is what
  makes the whole system fail open.
- **`sendCmd()` in main.cpp is the only outbound choke point.** `wifiWrite()`
  inspects what passes through it to resolve the pending request, which is why
  answering a prompt needed no changes to the UI code.
- **BLE is untouched.** The WiFi transport is additive; both feed the same
  `_applyJson` in data.h. Don't "simplify" by removing the BLE path.
- **A snapshot without a `prompt` field clears the prompt.** That's why
  `handleState` defers agent pushes while `pendingId` is set — a 5 s state feed
  would otherwise wipe the question off the screen.

## Hard-won gotchas

Each of these cost real debugging time.

- **Never `WiFi.setSleep(false)`.** With BLE also running, the ESP32 aborts:
  `Should enable WiFi modem sleep when both WiFi and Bluetooth are enabled`.
  Boot-loops the device.
- **Never set `-DARDUINO_USB_CDC_ON_BOOT=1`.** `Serial.printf` then blocks when
  no host reads the port, and the firmware prints on every command — the device
  hangs on the welcome screen. There is effectively **no usable USB console** on
  this board; use `/debug` instead.
- **Never pass a literal byte count to `rxPush()`.** Use `strlen`. A count one
  short drops the terminating newline, the next line glues onto it, and both
  become unparseable JSON. Symptom: prompts silently never render. See
  `pushIdle()`.
- **Don't put `claude.local` in the hook URL.** mDNS resolves from Node but
  takes ~5 s, on every single call. Use the IP; mDNS is for rediscovery only.
- **This board has no PSRAM**, despite `-DBOARD_HAS_PSRAM` in platformio.ini
  (inherited from the StampS3 board def). Boot logs a PSRAM probe failure; it
  is harmless but means less headroom than the flag implies.
- **Flash is ~84% full** on the `no_ota.csv` layout. Change the partition table
  before adding anything large.

## Debugging a blank screen

`GET :81/debug` answers "did the UI actually see it?":

- `rxQueued` climbing while `lastApplied.count` is static → UI loop is stalled
  (almost always a blocking `Serial` write).
- `lastApplied.head` showing two `{` objects run together → a line was pushed
  without its newline.
- `pendingId` set but `uiPrompt.id` empty → the line arrived but failed to
  parse or was cleared.

## Testing

Approvals need a human keypress, so end-to-end tests cannot be fully automated.
The pattern that works: fire the request with `curl` in the background, ask the
user to press the key, then read the saved response.

```bash
nohup curl -s -m 100 -X POST http://192.168.0.170/approve \
  -H 'Content-Type: application/json' \
  -d '{"cwd":"/x","tool_name":"Bash","tool_use_id":"t1",
       "tool_input":{"command":"git push --force"}}' > /tmp/r.json &
```

Check `:81/debug` a few seconds later to confirm it rendered **before**
concluding a key mapping is broken — that distinction was the source of several
wrong diagnoses.

## Conventions

- Firmware follows the upstream style: terse function-level comments, no
  per-line commentary.
- `wifi_config.h` is gitignored and holds real credentials; edit
  `wifi_config.h.template` when adding fields.
- Device code differs between boards only in display and input drivers. The
  wire protocol is shared, so `cores3/` and `cores3se/` should reuse
  `wifi_bridge.*` unchanged.

## Upstream

Forked from `y88huang/claude-desktop-buddy-cardputer`, itself a port of
`anthropics/claude-desktop-buddy` (MIT, preserved). The BLE protocol is
documented in `cardputer/REFERENCE.md` — the WiFi transport carries the same
newline-delimited JSON, so that document describes both.
