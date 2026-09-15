# M5Stack × Claude Code — Approval Remote & Agentic Control

> Original planning document, kept for history. It predates the discovery of
> Anthropic's desktop-buddy firmware and describes a from-scratch build that was
> not taken. **README.md describes what actually shipped.**

Hardware in hand: Cardputer Adv (ESP32-S3), CoreS3, CoreS3 SE.
Laptop: macOS, Claude Code 2.1.272, `defaultMode: auto`, LAN 192.168.0.145.

## 1. What is proven, not assumed

Verified on this machine before planning (test rig: local HTTP server standing in for
the device, hook injected via `--settings` so global config was untouched):

- A `PreToolUse` hook of `type: "http"` fires **in auto mode**, POSTs the tool call as
  JSON, and the response body decides the call.
- It **overrides the allow-list**. `Bash(ls *)` is allow-listed in `settings.json`; the
  hook denied it anyway and the reason string surfaced in the session:
  `"The command was blocked — it returned 'DENIED BY M5 CARDPUTER (simulated)'"`.
- Payload is **675 bytes** for a Bash call. Exact keys captured:
  `session_id, transcript_path, cwd, prompt_id, permission_mode, effort,
  hook_event_name, tool_name, tool_input, tool_use_id`.

From official docs (code.claude.com/docs/en/hooks):

- `PermissionRequest` is **skipped in auto mode** — unusable here. `PreToolUse` is the gate.
- Hook timeout **fails open**: *"A timed-out hook doesn't block the tool call. The call
  continues through the normal permission flow."* A dead Cardputer degrades to the normal
  laptop prompt instead of hanging the session. This is the safety net.
- HTTP hooks support `headers` with `$VAR` interpolation, gated by `allowedEnvVars`.

## 2. The one structural conflict in the chosen design

Chosen: *hook POSTs the device directly, no bridge daemon.*

That works completely for **approvals** — proven above. It cannot carry **control**.
Hooks are outbound-only: Claude Code POSTs when *it* decides to fire a hook. Nothing in
the hook system listens for inbound commands, so "launch a session", "send this prompt",
"stop that agent" have no receiver on the laptop.

These are two different directions, and only one is covered:

| Path | Direction | Mechanism | Daemon needed |
|---|---|---|---|
| A — Approvals | laptop → device | `PreToolUse` http hook | **No** |
| B — Control | device → laptop | must be received by something | **Yes, minimal** |

Path A ships exactly as chosen. Path B needs a small laptop-side listener — not a bridge
for approvals, just a receiver for control. Phase 4 is written so you can decide then;
Phases 1–3 do not depend on it.

## 3. Architecture

### Path A — Approval remote (Cardputer Adv is an HTTP server)

```
Claude Code  --POST 675B JSON-->  Cardputer :80/approve
                                    renders command on 240x135
                                    waits for keypress
             <--{permissionDecision}--  y = allow / n = deny
```

Hook config (goes in `~/.claude/settings.json`):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Write|Edit",
        "hooks": [
          {
            "type": "http",
            "url": "http://192.168.0.61/approve",
            "timeout": 30,
            "headers": { "Authorization": "Bearer $M5_TOKEN" },
            "allowedEnvVars": ["M5_TOKEN"]
          }
        ]
      }
    ]
  }
}
```

Device response contract:

```json
{ "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "permissionDecisionReason": "approved on Cardputer" } }
```

Returning `{}` means *no decision* — the call falls through to the normal flow. That is
the correct answer for anything the device chooses not to rule on.

### Path B — Control plane (Cardputer is an HTTP client)

```
Cardputer --POST /dispatch {"prompt","cwd"}--> laptop listener
                                                 claude --bg / -p --output-format stream-json
Cardputer --GET  /sessions -------------------> claude agents --json
```

`claude agents --json` prints active interactive *and* background sessions without a TTY —
that is the session feed, no parsing of terminal output. `claude --bg` returns a short id
that `attach`, `logs`, `stop`, `rm` all take.

### Device roles

- **Cardputer Adv** — primary approval remote. 56-key board (`y`/`n`), 1750mAh, pocketable.
- **CoreS3** — desk dashboard. 320×240 capacitive touch fits a real command + diff snippet;
  touch Approve/Deny. Dual mic is a later voice-prompt path.
- **CoreS3 SE** — no battery bottom, no camera/IMU. USB-powered always-on wall panel:
  fleet status, or a dedicated console for high-risk approvals only.

## 4. Phases

**Phase 0 — Bench bring-up.** PlatformIO + M5Unified/M5Cardputer. WiFi join, static DHCP
reservation for the Cardputer, serve `GET /health`. Confirm the laptop reaches it.
*Done when:* `curl http://<ip>/health` returns from the laptop.

**Phase 1 — Approval remote (Path A).** HTTP server on `/approve`, ArduinoJson parse,
render `tool_name` + `tool_input.command`, block on keypress, respond. Wire the hook above.
*Done when:* a real `git push` in a live session is held until you press a key on the
Cardputer, and the reason string appears in the session.

**Phase 2 — Device-side policy.** Without this the device buzzes on every `cat`. Rules live
on the device (the trusted physical object): auto-`{}` the safe, prompt only on risky —
`git push`, `rm -rf`, writes outside the repo, anything matching `prod`. Mirrors your
global Rules 1 & 2, which is the natural fit for a physical approve button.

**Phase 3 — Session visibility.** Device polls the laptop for `claude agents --json`;
renders session list, cwd, status. Read-only. *This is the first piece needing a listener,
but only a read-only one.*

**Phase 4 — Control (Path B).** Adds `POST /dispatch`. **This is the security-critical
step** — an open dispatch endpoint is remote code execution on your laptop. Requires
shared-token HMAC, bind to LAN interface only, device IP allowlist. Decide scope here:
canned prompts from a menu are far safer than free-text on a 56-key board, and probably
more useful in practice.

**Phase 5 — CoreS3 dashboard.** Port the renderer to 320×240 touch; SE as the always-on panel.

## 5. Risks

1. **Dispatch endpoint = RCE.** Phase 4's biggest exposure. Token + interface bind +
   allowlist are not optional.
2. **Battery.** 1750mAh against ~132mA WiFi-active is roughly 13h always-on *(estimate,
   not measured)*. Needs modem sleep, or accept daily charging.
3. **Large `tool_input`.** A Bash call is 675B, but a `Write` carries the full file body —
   potentially tens of KB. Cap the request size and truncate for display; do not assume
   675B. **Speculation:** the Cardputer Adv page lists 8MB Flash and does not state PSRAM —
   confirm PSRAM on the board before sizing buffers.
4. **IP churn.** Direct-POST has no discovery. Use a DHCP reservation. `.local` mDNS may
   work from macOS but is unverified for Claude Code's HTTP client — do not rely on it untested.
5. **Latency tax.** Every matched tool call waits on the device. Keep `timeout: 30` so a
   flat battery costs 30s, not the 600s default, before falling open.

## 6. Open questions

- Phase 4 free-text prompts, or a fixed menu of canned prompts?
- Should the device gate *all* sessions, or only ones launched with a marker env var?
- One shared token, or per-device tokens for Cardputer vs CoreS3?
