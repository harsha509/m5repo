# m5repo — M5Stack Cardputer as a Claude Code control surface

Physical approval, question-answering and session control for the Claude Code
**CLI**, over WiFi.

## Layout

```
firmware/      Cardputer Adv (ESP32-S3) — the device.
bridge/broker/ Laptop daemon: hook endpoint, policy, session control.
bridge/policy.json   What reaches the screen. Edit freely; hot-reloaded.
```

## How it works

A broker daemon on the laptop owns the `PreToolUse` hook and the `claude` CLI.
The device long-polls it and never listens for anything, so there is no inbound
port and no address for the laptop to discover.

```
~/.claude/settings.json ──http──► broker 127.0.0.1:8787/hook
                                    │  policy.json → allow / escalate / deny
                                    │
                        LAN :8787 + bearer token
                                    │
                   Cardputer ──GET /device/poll (held open)──►
                             ◄─POST /device/verdict · /device/control─
```

- **Not escalated** → broker answers `{}` instantly, Claude proceeds normally
- **Escalated** → the command (or a diff, for `Edit`/`Write`) renders on screen
  and blocks until `Y` or `N`
- **AskUserQuestion** → each question renders with its options; the answer goes
  back through `updatedInput`, so it never reaches the terminal

**Everything fails open.** A timeout, a missing device, a dead broker or any
exception returns `{}` — "no decision" — and Claude Code's normal permission
flow takes over. Nothing hangs waiting on hardware.

What escalates lives in `bridge/policy.json`: `git push`, `git commit`,
`rm -rf`, `--force`, `drop table`, `terraform destroy`, writes to `.env` and
`.claude/`, and similar. Edit and save — no restart, no reflash.

## Keys

| Key | Where | Action |
| --- | --- | --- |
| `Y` / `Enter` | approval | approve |
| `N` | approval / question | deny / skip |
| `R` | approval | type a denial reason — sent to Claude verbatim |
| `;` `.` | approval | scroll a long command |
| `;` `.` `Enter` | question | move highlight, pick the option |
| `O` | question | type a free-text answer |
| `S` / `U` | anywhere | Sessions window / Usage window |
| `;` `.` `Enter` | Sessions | select a session, open its actions |
| `` ` `` | modals | back / cancel |
| `,` `/` | info pages | previous / next page |
| `]` `[` | anywhere | brightness up / down |
| `=` `-` | anywhere | volume up / down |
| `M` | anywhere | menu |

`S` and `U` are locked out while a prompt is on screen, so an approval is never
hidden behind another window.

In the Sessions window, rows marked `*` are interactive sessions. `claude
stop/logs/respawn` only address background jobs, so those rows are read-only
and `Enter` will not open an action sheet for them.

## Setup

1. Config:
   ```bash
   cp firmware/src/wifi_config.h.template firmware/src/wifi_config.h
   ```
   Fill in SSID, password, your laptop's IP as `BROKER_HOST`, and a
   `BROKER_TOKEN` of your choosing. The file is gitignored.

2. Flash — confirm the board first, since port numbers move and more than one
   ESP32-S3 may be attached:
   ```bash
   ioreg -p IOUSB -l -w 0 | grep "USB Serial Number"
   .venv/bin/pio run -e cardputer-adv -d firmware -t upload \
     --upload-port /dev/cu.usbmodemXXX
   ```

3. Start the broker with the same token:
   ```bash
   .venv/bin/python -m bridge.broker.server --token <BROKER_TOKEN>
   ```
   To keep it running across reboots:
   ```bash
   python3 bridge/install_service.py --token <BROKER_TOKEN>
   ```

4. Point Claude Code at it:
   ```bash
   python3 bridge/use_broker.py          # writes the hook, backs up settings.json
   python3 bridge/use_broker.py --revert  # undo
   ```

5. Check it:
   ```bash
   curl http://127.0.0.1:8787/health      # broker + device telemetry
   .venv/bin/python -m bridge.broker.selftest
   ```

## Broker endpoints

| Route | Bind | Purpose |
| --- | --- | --- |
| `/hook` | loopback only | The Claude Code `PreToolUse` hook. |
| `/device/poll` | LAN + token | Held open; returns one card or a state snapshot. |
| `/device/verdict` | LAN + token | The answer to a card, by its id. |
| `/device/control` | LAN + token | `stop` / `respawn` / `rm` / `logs` / `say`. |
| `/health` | any | Liveness, open cards, per-device telemetry. |

The device has no server of its own. What a `/debug` endpoint would show rides
the poll query string and surfaces at `/health` — which keeps it observable even
when the board is unreachable.

Control commands carry only a session id and a macro key. No path, prompt or
command string ever crosses the wire.

## Hardware notes

- Cardputer Adv has **no PSRAM** despite the community `-DBOARD_HAS_PSRAM` flag.
- WiFi modem sleep must stay enabled; the ESP32 aborts if WiFi and BLE share the
  radio without it.
- Flash sits at ~59% of a 3 MB OTA slot.
- **OTA does not work yet.** The partitions and manifest exist, but the transfer
  dies partway. Flash over the cable.
- Brightness and volume persist in NVS (`s_bri`, `s_vol`) and survive reboot and
  screen-sleep.

## Credits

Firmware forked from [y88huang/claude-desktop-buddy-cardputer](https://github.com/y88huang/claude-desktop-buddy-cardputer),
itself a port of [anthropics/claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy).
BLE still works unchanged; the WiFi transport is additive.
