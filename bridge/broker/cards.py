"""Turns a hook event into a card the device can render directly.

The device parses no tool JSON and holds no layout logic: every line arrives
already wrapped to the polling device's column count, which is why porting a
board is a display driver plus two numbers.
"""

import textwrap

MAX_LINES = 24
TITLE_MAX = 64

APPROVE_ACTIONS = ["allow", "deny", "reason"]
ASK_ACTIONS = ["pick", "other", "deny"]


def wrap(text: str, cols: int, max_lines: int = MAX_LINES) -> list:
    """Word-wraps, but breaks long unbroken tokens (paths, URLs) rather than
    overflowing the screen."""
    if not text:
        return []
    lines = []
    for paragraph in str(text).splitlines() or [""]:
        if not paragraph.strip():
            lines.append("")
            continue
        lines.extend(textwrap.wrap(paragraph, width=max(cols, 8),
                                   break_long_words=True, break_on_hyphens=False))
        if len(lines) > max_lines:
            break
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + ["... (truncated)"]
    return lines


def leaf(path: str) -> str:
    """Project folder rather than the whole path — the leaf identifies the repo."""
    if not path:
        return ""
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def _body(tool_name: str, tool_input: dict) -> str:
    if not isinstance(tool_input, dict):
        return ""
    if tool_name == "Bash":
        return tool_input.get("command", "") or ""
    for key in ("file_path", "notebook_path", "path", "url", "pattern", "prompt"):
        if tool_input.get(key):
            return str(tool_input[key])
    return ""


def approve_card(event: dict, cols: int) -> dict:
    """A yes/no card for a tool call held at PreToolUse."""
    tool_name = event.get("tool_name", "tool")
    tool_input = event.get("tool_input", {}) or {}
    body = _body(tool_name, tool_input)
    return {
        "id": event.get("tool_use_id", ""),
        "kind": "approve",
        "tool": tool_name,
        "title": body.splitlines()[0][:TITLE_MAX] if body else tool_name,
        "cwd": leaf(event.get("cwd", "")),
        "sess": event.get("permission_mode", ""),
        "lines": wrap(body, cols),
        "actions": APPROVE_ACTIONS,
    }


def ask_card(event: dict, question: dict, index: int, total: int, cols: int) -> dict:
    """One question of an AskUserQuestion. Each gets its own card id so the
    device treats it as a new prompt rather than a redraw of the last."""
    options = [str(o.get("label", "")) for o in question.get("options", []) if isinstance(o, dict)]
    return {
        "id": f"{event.get('tool_use_id', '')}#{index}",
        "kind": "ask",
        "tool": question.get("header", "question") or "question",
        "title": str(question.get("question", ""))[:TITLE_MAX],
        "cwd": leaf(event.get("cwd", "")),
        "sess": event.get("permission_mode", ""),
        "lines": wrap(question.get("question", ""), cols),
        "opts": options[:4],
        "qi": index,
        "nq": total,
        "actions": ASK_ACTIONS,
    }
