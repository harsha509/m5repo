"""Wraps the `claude` CLI so the device can see and act on live sessions.

`claude agents --json` is the only one of these confirmed to work without a TTY;
the rest are invoked the same way and their failures are reported, never raised.
"""

import json
import logging
import subprocess
import time
from pathlib import Path

CLI = "claude"
TIMEOUT = 20
MAX_ENTRIES = 8
ENTRY_WIDTH = 91
RUNNING = {"running", "active", "busy", "generating"}
WAITING = {"blocked", "waiting"}

log = logging.getLogger("broker.sessions")


def _run(args, cwd=None):
    try:
        done = subprocess.run([CLI, *args], capture_output=True, text=True,
                              timeout=TIMEOUT, cwd=cwd)
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("claude %s failed: %s", " ".join(args), exc)
        return None, str(exc)
    if done.returncode != 0:
        return None, (done.stderr or done.stdout or "").strip()[:200]
    return done.stdout, ""


def state_of(session: dict) -> str:
    return session.get("state") or session.get("status") or "?"


def list_sessions() -> list:
    out, _ = _run(["agents", "--json"])
    if not out or not out.strip():
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


def short_id(session: dict) -> str:
    return session.get("id") or (session.get("sessionId") or "")[:8]


def describe(session: dict) -> str:
    state = state_of(session)
    name = session.get("name") or short_id(session) or "session"
    return f"[{state}] {name} - {Path(session.get('cwd', '')).name or '~'}"[:ENTRY_WIDTH]


def snapshot() -> dict:
    """The state line the device renders in its Sessions window."""
    sessions = list_sessions()
    busy = [s for s in sessions if state_of(s) in RUNNING]
    blocked = [s for s in sessions if state_of(s) in WAITING]
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
        "msg": msg[:23],
        "entries": [describe(s) for s in ordered[:MAX_ENTRIES]],
        "ids": [short_id(s) for s in ordered[:MAX_ENTRIES]],
        "states": [state_of(s) for s in ordered[:MAX_ENTRIES]],
    }


def logs(session_id: str) -> str:
    out, err = _run(["logs", session_id])
    return out or err or "(no output)"


def lifecycle(action: str, session_id: str) -> str:
    """stop / respawn / rm, by the short id `claude agents` prints."""
    if action not in ("stop", "respawn", "rm"):
        return f"unknown action {action}"
    out, err = _run([action, session_id])
    return (out or "").strip() or err or f"{action} ok"


def say(session_id: str, prompt: str, cwd: str = None) -> str:
    """Continues a session in the background with a canned prompt. A session
    that is already running gets a copy rather than the message, per `--bg`,
    so callers should offer this only for sessions that are not running."""
    out, err = _run(["--bg", "--resume", session_id, prompt], cwd=cwd)
    return (out or "").strip() or err or "dispatched"


def start(prompt: str, cwd: str) -> str:
    name = f"m5-{int(time.time()) % 100000}"
    out, err = _run(["--bg", "-n", name, prompt], cwd=cwd)
    return (out or "").strip() or err or "started"
