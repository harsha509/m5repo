"""Feeds live Claude Code state to an M5 device over WiFi.

Polls `claude agents --json` and the session transcripts, then POSTs a snapshot
to the device's /state endpoint. Without this the device shows "no claude
connected", since nothing else pushes it state over WiFi."""

import argparse
import json
import logging
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
MAX_ENTRIES = 8
ENTRY_WIDTH = 91
RUNNING_STATES = {"running", "active", "busy", "generating"}
WAITING_STATES = {"blocked", "waiting"}

log = logging.getLogger("m5agent")


def list_sessions():
    """Returns the active sessions, or an empty list if the CLI call fails."""
    try:
        out = subprocess.run(
            ["claude", "agents", "--json"],
            capture_output=True, text=True, timeout=15,
        )
        return json.loads(out.stdout) if out.stdout.strip() else []
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        log.debug("session list failed: %s", exc)
        return []


def token_totals(since_epoch):
    """Sums output tokens across transcripts touched since `since_epoch`."""
    total = 0
    for path in PROJECTS.glob("**/*.jsonl"):
        try:
            if path.stat().st_mtime < since_epoch:
                continue
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if '"usage"' not in line:
                        continue
                    try:
                        usage = json.loads(line).get("message", {}).get("usage", {})
                    except json.JSONDecodeError:
                        continue
                    total += usage.get("output_tokens", 0) or 0
        except OSError:
            continue
    return total


def midnight_epoch():
    now = datetime.now()
    return datetime(now.year, now.month, now.day).timestamp()


def describe(session):
    """One display line per session: state, name, and the leaf of its cwd."""
    state = session.get("state") or session.get("status") or "?"
    name = session.get("name") or session.get("id") or "session"
    leaf = Path(session.get("cwd", "")).name or "~"
    return f"[{state}] {name} - {leaf}"[:ENTRY_WIDTH]


def state_of(session):
    return session.get("state") or session.get("status") or "?"


def build_snapshot(sessions, tokens_today):
    """Active work leads the summary; a stale blocked session must not mask it."""
    busy = [s for s in sessions if state_of(s) in RUNNING_STATES]
    blocked = [s for s in sessions if state_of(s) in WAITING_STATES]
    if busy:
        msg = f"busy: {busy[0].get('name', 'session')}"
        if len(busy) > 1:
            msg += f" +{len(busy) - 1}"
    elif blocked:
        msg = f"{len(blocked)} blocked"
    else:
        msg = f"{len(sessions)} idle" if sessions else "no sessions"
    ordered = busy + blocked + [s for s in sessions if s not in busy and s not in blocked]
    return {
        "total": len(sessions),
        "running": len(busy),
        "waiting": len(blocked),
        "msg": msg,
        "entries": [describe(s) for s in ordered[:MAX_ENTRIES]],
        "tokens_today": tokens_today,
    }


def post(url, payload, token, timeout=8):
    body = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True, help="device IP or host")
    parser.add_argument("--port", type=int, default=81,
                        help="state port; 81 stays responsive while /approve blocks on port 80")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--token", default="")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    base = f"http://{args.device}:{args.port}"
    log.info("feeding %s every %.0fs", base, args.interval)

    offset = -time.altzone if time.localtime().tm_isdst else -time.timezone
    try:
        post(f"{base}/state", {"time": [int(time.time()), offset]}, args.token)
        log.info("clock synced")
    except (urllib.error.URLError, OSError) as exc:
        log.warning("clock sync failed: %s", exc)

    last_tokens = 0
    last_token_scan = 0.0
    while True:
        sessions = list_sessions()
        # Transcript scanning walks every project, so rate-limit it.
        if time.time() - last_token_scan > 30:
            last_tokens = token_totals(midnight_epoch())
            last_token_scan = time.time()
        snapshot = build_snapshot(sessions, last_tokens)
        try:
            post(f"{base}/state", snapshot, args.token)
            log.debug("sent %s", snapshot["msg"])
        except (urllib.error.URLError, OSError) as exc:
            log.warning("device unreachable: %s", exc)
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
