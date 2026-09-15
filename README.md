# m5repo — M5Stack devices as a Claude Code control surface

Physical approval, question-answering and monitoring hardware for the Claude
Code **CLI**, over WiFi.

## Layout

```
cardputer/     Cardputer Adv (ESP32-S3) — approval remote.  DONE
cores3/        CoreS3 — desk dashboard.                     planned
cores3se/      CoreS3 SE — always-on wall panel.            planned
bridge/        Laptop-side Python: state feeder + BLE fallback.
```

Each device folder is a self-contained PlatformIO project. The wire protocol
(newline-delimited JSON) is shared, so device code differs only in display and
input drivers.

## How it works

Claude Code's `PreToolUse` hook is configured as `type: "http"` and POSTs each
matching tool call straight to the device. No laptop daemon sits in the
approval path.

- **Bash, not risky** → device answers `{}` instantly, Claude proceeds normally
- **Bash, risky** → command renders on screen, blocks until `Y` or `N`
- **AskUserQuestion** → the question and its options render; pick one, or type
  a free-text answer. The answer goes back through `updatedInput`, so the
  question is settled before it ever reaches the terminal.

Timeouts fail open: if the device is off, the call falls back to the normal
terminal prompt rather than hanging.

```
Claude Code CLI ──POST /approve──► Cardputer ──keys──► {"permissionDecision": ...}
```

The gate list lives in `cardputer/src/wifi_bridge.cpp` (`GATED[]`): `git push`,
`git commit`, `rm -rf`, `--force`, `drop table`, `prod`, `terraform destroy`,
and similar. Everything else is auto-approved silently.

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
| `,` `/` | info pages | previous / next page |
| `]` `[` | anywhere | brightness up / down |
| `=` `-` | anywhere | volume up / down |
| `M` | anywhere | menu |

`S` and `U` are locked out while a prompt is on screen, so an approval is never
hidden behind another window.

## Cardputer setup

1. `cp src/wifi_config.h.template src/wifi_config.h` and fill in SSID/password
   (gitignored).
2. `pio run -e cardputer-adv -t upload`
3. Find it: `curl http://<device-ip>/health` — or `http://claude.local/health`
   via mDNS if the address changed.
4. Point Claude Code at it in `~/.claude/settings.json`:

```json
"hooks": {
  "PreToolUse": [
    { "matcher": "Bash|AskUserQuestion",
      "hooks": [{ "type": "http", "url": "http://<device-ip>/approve", "timeout": 60 }] }
  ]
}
```

Use the IP, not `claude.local`, in the hook: mDNS resolution from Node adds
~5 s to every call. Give the device a DHCP reservation instead.

5. Feed it session state so the Sessions and Usage windows are live:

```
python3 -m venv .venv && .venv/bin/pip install bleak
.venv/bin/python bridge/m5agent.py --device <device-ip>
```

Approvals work without the agent; only the info windows need it.

## Endpoints

| Route | Port | Method | Purpose |
| --- | --- | --- | --- |
| `/approve` | 80 | POST | Hook payload in, verdict (or answered `updatedInput`) out. Blocks on keypress. |
| `/state` | 81 | POST | Sessions and usage snapshot for the info windows. |
| `/health` | 80 and 81 | GET | Liveness, IP, RSSI. |
| `/debug` | 81 | GET | Last line the UI parser applied, pending prompt, unread queue depth. |

`/debug` is what to check when the screen shows nothing: if `rxQueued` keeps
growing while `lastApplied.count` stays put, the UI loop is stalled; if
`lastApplied.head` holds two `{` objects run together, a line was pushed
without its terminating newline.

`/approve` holds its connection open while a prompt waits for you, so it has a
port to itself. Port 81 never blocks: the agent's state pushes and health
probes keep working mid-prompt, and the Sessions window stays live.

## Hardware notes

- Cardputer Adv has **no PSRAM** despite the community `-DBOARD_HAS_PSRAM` flag.
- WiFi modem sleep must stay enabled; the ESP32 aborts if WiFi and BLE share the
  radio without it.
- Flash is at ~84% with the `no_ota.csv` layout — switch partitions before
  adding much more.
- Brightness and volume persist in NVS (`s_bri`, `s_vol`) alongside the stock
  settings, and survive reboot and screen-sleep.

## Credits

Firmware forked from [y88huang/claude-desktop-buddy-cardputer](https://github.com/y88huang/claude-desktop-buddy-cardputer),
itself a port of [anthropics/claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy).
The WiFi transport (`wifi_bridge.*`) is additive — BLE still works unchanged.
