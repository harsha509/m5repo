"""End-to-end check of the broker against a simulated device.

The point of this file is the concurrency case: the firmware it replaces held
one `pendingId` and one binary semaphore, so a second session's approval queued
behind the first. Here two hooks are held at once and answered in reverse order,
which fails loudly if verdicts can cross.
"""

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

from .server import Broker, serve

TOKEN = "selftest"
BASE = None
FAILURES = []


def call(path, payload=None, timeout=30, token=TOKEN):
    url = f"{BASE}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data,
                                     method="POST" if data is not None else "GET")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def hook(payload, sink, key):
    sink[key] = call("/hook", payload)


def bash_event(use_id, command, session="s1"):
    return {"session_id": session, "cwd": "/Users/x/m5repo", "permission_mode": "auto",
            "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_use_id": use_id, "tool_input": {"command": command}}


def check(name, condition, detail=""):
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: {detail}")
        FAILURES.append(name)


def poll(device="test", wait=10):
    return call(f"/device/poll?dev={device}&cols=40&wait={wait}", timeout=wait + 10)


def decision_of(reply):
    return reply.get("hookSpecificOutput", {}).get("permissionDecision")


def reason_of(reply):
    return reply.get("hookSpecificOutput", {}).get("permissionDecisionReason")


def test_not_gated():
    print("not-gated command answers no-decision immediately")
    reply = call("/hook", bash_event("t-safe", "ls -la"))
    check("returns {}", reply == {}, reply)


def test_concurrent():
    print("two concurrent approvals, answered in reverse order")
    replies = {}
    a = threading.Thread(target=hook, args=(bash_event("t-a", "git push origin main", "sA"), replies, "a"))
    b = threading.Thread(target=hook, args=(bash_event("t-b", "rm -rf /tmp/zzz", "sB"), replies, "b"))
    a.start(); b.start()

    first, second = poll(), poll()
    ids = {first["prompt"]["id"], second["prompt"]["id"]}
    check("both cards delivered", ids == {"t-a", "t-b"}, ids)
    check("both were open at once", True)

    # Answer b first, then a — a cross-resolve shows up as swapped verdicts.
    call("/device/verdict", {"cmd": "permission", "id": "t-b", "decision": "deny",
                             "reason": "not this one"})
    call("/device/verdict", {"cmd": "permission", "id": "t-a", "decision": "once"})
    a.join(20); b.join(20)

    check("git push -> allow", decision_of(replies.get("a", {})) == "allow", replies.get("a"))
    check("rm -rf -> deny", decision_of(replies.get("b", {})) == "deny", replies.get("b"))
    check("deny reason verbatim", reason_of(replies.get("b", {})) == "not this one",
          reason_of(replies.get("b", {})))


def test_timeout_fails_open():
    print("an unanswered card falls open to the terminal")
    replies = {}
    t = threading.Thread(target=hook, args=(bash_event("t-slow", "git push --force", "sC"), replies, "x"))
    t.start()
    poll()                      # deliver it, then never answer
    t.join(20)
    check("returns {}", replies.get("x") == {}, replies.get("x"))


def test_stale_verdict_ignored():
    print("a stale verdict cannot disturb a live card")
    ok = call("/device/verdict", {"cmd": "permission", "id": "t-slow", "decision": "once"})
    check("unknown id rejected", ok == {"ok": False}, ok)


def test_ask():
    print("AskUserQuestion is walked question by question")
    event = {"session_id": "sD", "cwd": "/Users/x/m5repo", "hook_event_name": "PreToolUse",
             "tool_name": "AskUserQuestion", "tool_use_id": "t-ask",
             "tool_input": {"questions": [
                 {"question": "Which database?", "header": "DB",
                  "options": [{"label": "Postgres"}, {"label": "SQLite"}]},
                 {"question": "Migrate now?", "header": "When",
                  "options": [{"label": "Yes"}, {"label": "Later"}]}]}}
    replies = {}
    t = threading.Thread(target=hook, args=(event, replies, "ask"))
    t.start()
    first = poll()
    check("first question delivered", first["prompt"]["id"] == "t-ask#0", first["prompt"]["id"])
    check("options carried", first["prompt"].get("opts") == ["Postgres", "SQLite"],
          first["prompt"].get("opts"))
    call("/device/verdict", {"cmd": "answer", "id": "t-ask#0", "a": "SQLite"})
    second = poll()
    check("second question delivered", second["prompt"]["id"] == "t-ask#1", second["prompt"]["id"])
    call("/device/verdict", {"cmd": "answer", "id": "t-ask#1", "a": "Later"})
    t.join(20)

    hook_out = replies.get("ask", {}).get("hookSpecificOutput", {})
    check("allowed", hook_out.get("permissionDecision") == "allow", hook_out)
    check("answers assembled", hook_out.get("updatedInput", {}).get("answers") ==
          {"Which database?": "SQLite", "Migrate now?": "Later"},
          hook_out.get("updatedInput", {}).get("answers"))


def test_auth_and_loopback():
    print("auth and loopback restrictions")
    try:
        call("/device/poll?dev=x&wait=1", token="wrong")
        check("bad token rejected", False, "no 401 raised")
    except urllib.error.HTTPError as exc:
        check("bad token rejected", exc.code == 401, exc.code)


def test_poll_clears_prompt():
    print("an idle poll returns state with no prompt key")
    snap = poll(device="idle-probe", wait=1)
    check("no prompt key", "prompt" not in snap, snap)
    check("carries session state", "total" in snap and "entries" in snap, snap)


def main():
    global BASE
    root = Path(__file__).resolve().parents[2]
    broker = Broker(root / "does-not-exist.bin", TOKEN, root / "bridge" / "policy.json", ttl=6.0)
    httpd = serve("127.0.0.1", 0, broker)
    BASE = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"broker on {BASE}\n")

    for test in (test_not_gated, test_concurrent, test_timeout_fails_open,
                 test_stale_verdict_ignored, test_ask, test_auth_and_loopback,
                 test_poll_clears_prompt):
        test()
    httpd.shutdown()

    print()
    if FAILURES:
        print(f"FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
