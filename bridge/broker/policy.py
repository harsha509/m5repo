"""Decides which hook events reach the device, from bridge/policy.json.

This replaces the firmware's hardcoded GATED[] array, so changing what prompts
is an edit to a JSON file rather than a recompile and a reflash.
"""

import fnmatch
import json
import logging
import re
import threading
from pathlib import Path

ALLOW = "allow"
ESCALATE = "escalate"
DENY = "deny"

# Which tool_input field a pattern's glob is matched against.
SUBJECT_FIELDS = {
    "Bash": "command",
    "Edit": "file_path",
    "Write": "file_path",
    "Read": "file_path",
    "NotebookEdit": "notebook_path",
}

_PATTERN = re.compile(r"^(?P<tool>[A-Za-z_][\w.:-]*)(?:\((?P<glob>.*)\))?$", re.DOTALL)

log = logging.getLogger("broker.policy")


def subject_of(tool_name: str, tool_input: dict) -> str:
    """The string a policy glob is matched against for this tool."""
    if not isinstance(tool_input, dict):
        return ""
    value = tool_input.get(SUBJECT_FIELDS.get(tool_name, ""), "")
    return value if isinstance(value, str) else ""


def matches(pattern: str, tool_name: str, subject: str) -> bool:
    parsed = _PATTERN.match(pattern.strip())
    if not parsed:
        log.warning("ignoring malformed policy pattern %r", pattern)
        return False
    if parsed.group("tool").lower() != tool_name.lower():
        return False
    glob = parsed.group("glob")
    if glob is None:
        return True
    return fnmatch.fnmatch(subject.lower(), glob.lower())


class Policy:
    """Reloads itself when policy.json's mtime changes."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._mtime = 0.0
        self._rules = {"default": ALLOW, "deny": [], "escalate": [], "allow": []}
        self.reload()

    def reload(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return
        with self._lock:
            if mtime == self._mtime:
                return
            try:
                loaded = json.loads(self.path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                # Keep the last good rules: a typo mid-edit must not silently
                # open the gate or start denying everything.
                log.error("policy.json unreadable, keeping previous rules: %s", exc)
                return
            self._rules = loaded
            self._mtime = mtime
            log.info("policy loaded: %d escalate, %d deny, default=%s",
                     len(loaded.get("escalate", [])), len(loaded.get("deny", [])),
                     loaded.get("default", ALLOW))

    def decide(self, tool_name: str, tool_input: dict) -> str:
        self.reload()
        with self._lock:
            rules = self._rules
        subject = subject_of(tool_name, tool_input)
        for bucket in (DENY, ESCALATE, ALLOW):
            for pattern in rules.get(bucket, []):
                if matches(pattern, tool_name, subject):
                    return bucket
        return rules.get("default", ALLOW)
