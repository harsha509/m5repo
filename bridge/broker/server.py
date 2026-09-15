"""Broker between Claude Code and the M5 devices.

One listener on 0.0.0.0 serves both sides. `/hook` is the single URL in
settings.json and is restricted to loopback callers; `/device/*` is what the
devices long-poll, behind a bearer token.

Fail-open is the invariant: every path that cannot produce a real answer returns
`{}` — no decision — and Claude Code's normal permission flow takes over.
"""

import argparse
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import cards, sessions
from .ota import read_image
from .policy import ESCALATE, DENY, Policy
from .registry import Card, Registry

IMAGE_CHUNK = 8192
CARD_TTL = 55.0
MAX_POLL_WAIT = 25.0

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIRMWARE = REPO_ROOT / "firmware" / ".pio" / "build" / "cardputer-adv" / "firmware.bin"
DEFAULT_POLICY = REPO_ROOT / "bridge" / "policy.json"

NO_DECISION = {}

# esp_reset_reason(), so a crash loop names itself instead of being guessed at.
RESET_REASONS = {"1": "poweron", "3": "software", "4": "PANIC", "5": "int-watchdog",
                 "6": "task-watchdog", "7": "watchdog", "8": "deepsleep",
                 "9": "BROWNOUT", "12": "usb-jtag"}

log = logging.getLogger("broker")


def hook_reply(decision: str, reason: str, updated_input=None) -> dict:
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                  "permissionDecision": decision,
                                  "permissionDecisionReason": reason}}
    if updated_input is not None:
        out["hookSpecificOutput"]["updatedInput"] = updated_input
    return out


class Broker:
    def __init__(self, firmware: Path, token: str, policy_path: Path, ttl: float):
        self.firmware = firmware
        self.token = token
        self.policy = Policy(policy_path)
        self.registry = Registry()
        self.ttl = ttl
        self._snapshot = {"total": 0, "running": 0, "waiting": 0, "msg": "starting",
                          "entries": [], "ids": [], "states": []}
        self._snap_lock = threading.Lock()
        self.telemetry = {}

    def image(self):
        return read_image(self.firmware)

    def snapshot(self) -> dict:
        with self._snap_lock:
            return dict(self._snapshot)

    def refresh_sessions(self) -> None:
        try:
            snap = sessions.snapshot()
        except Exception as exc:                      # never kill the poller
            log.warning("session refresh failed: %s", exc)
            return
        with self._snap_lock:
            self._snapshot = snap

    # -- the hook side ----------------------------------------------------

    def handle_hook(self, event: dict) -> dict:
        tool = event.get("tool_name", "")
        tool_input = event.get("tool_input", {}) or {}
        verdict = self.policy.decide(tool, tool_input)
        if verdict == DENY:
            return hook_reply("deny", "denied by policy.json")
        if verdict != ESCALATE:
            return NO_DECISION
        if tool == "AskUserQuestion":
            return self._ask(event)
        return self._approve(event)

    def _approve(self, event: dict) -> dict:
        card = Card(lambda cols: cards.approve_card(event, cols), self.ttl,
                    card_id=event.get("tool_use_id", ""))
        self.registry.open(card)
        try:
            answer = self.registry.wait(card)
        finally:
            self.registry.close(card.id)
        if not answer:
            return NO_DECISION                        # timeout -> terminal asks
        if answer.get("v") == "allow":
            return hook_reply("allow", "approved on device")
        return hook_reply("deny", answer.get("text") or "denied on device")

    def _ask(self, event: dict) -> dict:
        """Walks the questions one at a time, then answers the tool through
        updatedInput so it never reaches the terminal."""
        questions = (event.get("tool_input", {}) or {}).get("questions") or []
        if not questions:
            return NO_DECISION
        answers = {}
        for index, question in enumerate(questions):
            card = Card(lambda cols, q=question, i=index:
                        cards.ask_card(event, q, i, len(questions), cols),
                        self.ttl,
                        card_id=f"{event.get('tool_use_id', '')}#{index}")
            card.is_ask = True
            self.registry.open(card)
            try:
                answer = self.registry.wait(card)
            finally:
                self.registry.close(card.id)
            if not answer:
                return NO_DECISION
            if answer.get("v") == "deny":
                return hook_reply("deny", answer.get("text") or "declined on device")
            answers[str(question.get("question", ""))] = answer.get("text", "")
        updated = dict(event.get("tool_input", {}) or {})
        updated["answers"] = answers
        return hook_reply("allow", "answered on device", updated)


class _Handler(BaseHTTPRequestHandler):
    server_version = "m5broker/2.0"
    protocol_version = "HTTP/1.1"

    @property
    def broker(self) -> Broker:
        return self.server.broker

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return {}

    def _authorized(self) -> bool:
        return self.headers.get("Authorization", "") == f"Bearer {self.broker.token}"

    def _local(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _query(self) -> dict:
        return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

    def do_POST(self):
        route = urlsplit(self.path).path
        if route == "/hook":
            if not self._local():
                self._json(403, NO_DECISION)
                return
            try:
                self._json(200, self.broker.handle_hook(self._body()))
            except Exception:
                # Fail open: a broker bug must never block a tool call.
                log.exception("hook handler failed; answering no-decision")
                self._json(200, NO_DECISION)
            return
        if not route.startswith("/device/") or not self._authorized():
            self._json(401, {"error": "bad token"})
            return
        if route == "/device/verdict":
            self._verdict()
        elif route == "/device/control":
            self._control()
        else:
            self._json(404, {"error": "no such route"})

    def do_GET(self):
        route = urlsplit(self.path).path
        if route == "/health":
            image = self.broker.image()
            self._json(200, {"ok": True, "cards": self.broker.registry.open_count(),
                             "build": image.build if image else None,
                             "devices": self.broker.telemetry})
            return
        if not route.startswith("/device/") or not self._authorized():
            self._json(401, {"error": "bad token"})
            return
        if route == "/device/poll":
            self._poll()
        elif route == "/device/ota/manifest":
            self._ota_manifest()
        elif route == "/device/ota/firmware.bin":
            self._ota_image()
        else:
            self._json(404, {"error": "no such route"})

    # -- the device side --------------------------------------------------

    def _poll(self):
        """Returns exactly the snapshot shape data.h already parses: a line
        carrying `prompt` when something needs an answer, otherwise plain state.
        A snapshot with no `prompt` key is how the device clears a stale one."""
        query = self._query()
        device = query.get("dev", "anon")
        # The device has no inbound server any more; what :81/debug used to
        # serve rides the poll instead, which keeps reporting even when the
        # board's inbound path is unreachable.
        self.broker.telemetry[device] = {
            "seen": time.strftime("%H:%M:%S"), "ip": self.client_address[0],
            "upSec": int(query.get("up", 0) or 0),
            "heapFree": int(query.get("heap", 0) or 0),
            "heapBlk": int(query.get("blk", 0) or 0),
            "sprite": query.get("spr") == "1",
            "polls": int(query.get("polls", 0) or 0),
            "applied": int(query.get("applied", 0) or 0),
            "pollFails": int(query.get("fails", 0) or 0),
            "uiPrompt": query.get("prompt", "-"),
            "resetReason": RESET_REASONS.get(query.get("rst", ""), query.get("rst", "?")),
        }
        cols = max(int(query.get("cols", 40) or 40), 8)
        wait = min(float(query.get("wait", 20) or 20), MAX_POLL_WAIT)

        card = self.broker.registry.next_for(device, wait)
        if card is None:
            card = self.broker.registry.pending_for(device)
        if card is None:
            self._json(200, self.broker.snapshot())
            return
        payload = card.payload_for(cols)
        line = {"total": 1, "running": 0, "waiting": 1,
                "msg": f"approve: {payload.get('tool', 'tool')}"[:23],
                "prompt": {"id": payload["id"], "tool": payload.get("tool", ""),
                           "hint": "\n".join(payload.get("lines", [])) or payload.get("title", ""),
                           "cwd": payload.get("cwd", ""), "sess": payload.get("sess", ""),
                           "kind": payload.get("kind", "approve")}}
        if payload.get("kind") == "ask":
            line["prompt"].update({"opts": payload.get("opts", []),
                                   "qi": payload.get("qi", 0),
                                   "nq": payload.get("nq", 1)})
            line["msg"] = "question"
        self._json(200, line)

    def _verdict(self):
        """Accepts the device's native sendCmd shape, so the firmware's existing
        answering path needed no change."""
        body = self._body()
        card_id = body.get("id", "")
        command = body.get("cmd", "")
        if command == "answer":
            ok = self.broker.registry.resolve(card_id, {"v": "answer", "text": body.get("a", "")})
        elif command == "permission":
            allowed = body.get("decision") == "once"
            ok = self.broker.registry.resolve(
                card_id,
                {"v": "allow"} if allowed else {"v": "deny", "text": body.get("reason", "")})
        else:
            ok = False
        self._json(200, {"ok": ok})

    def _control(self):
        body = self._body()
        command = body.get("cmd", "")
        target = body.get("id", "")
        try:
            if command in ("stop", "respawn", "rm"):
                result = sessions.lifecycle(command, target)
            elif command == "logs":
                result = sessions.logs(target)[-400:]
            elif command == "say":
                macro = self.broker.policy.macros().get(body.get("macro", ""))
                result = sessions.say(target, macro) if macro else "unknown macro"
            elif command == "refresh":
                self.broker.refresh_sessions()
                result = "ok"
            else:
                result = f"unknown command {command}"
        except Exception as exc:
            log.exception("control %s failed", command)
            result = str(exc)[:200]
        self._json(200, {"ok": True, "result": result})

    def _ota_manifest(self):
        image = self.broker.image()
        if image is None:
            self._json(503, {"error": "no built firmware"})
            return
        self._json(200, {"build": image.build, "size": image.size,
                         "url": "/device/ota/firmware.bin"})

    def _ota_image(self):
        image = self.broker.image()
        if image is None:
            self._json(503, {"error": "no built firmware"})
            return
        log.info("serving %s bytes of %s to %s", image.size, image.build[:16], self.address_string())
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(image.size))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        sent = 0
        try:
            with image.path.open("rb") as handle:
                while chunk := handle.read(IMAGE_CHUNK):
                    self.wfile.write(chunk)
                    sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            log.warning("device dropped after %d/%d bytes", sent, image.size)
            return
        log.info("sent %d bytes to %s", sent, self.address_string())


def _session_poller(broker: Broker, interval: float):
    while True:
        broker.refresh_sessions()
        threading.Event().wait(interval)


def serve(bind: str, port: int, broker: Broker) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((bind, port), _Handler)
    httpd.daemon_threads = True
    httpd.broker = broker
    return httpd


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware", type=Path, default=DEFAULT_FIRMWARE)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--token", required=True)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--ttl", type=float, default=CARD_TTL)
    parser.add_argument("--session-interval", type=float, default=5.0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    broker = Broker(args.firmware.resolve(), args.token, args.policy.resolve(), args.ttl)
    threading.Thread(target=_session_poller, args=(broker, args.session_interval),
                     daemon=True).start()
    httpd = serve(args.bind, args.port, broker)
    log.info("broker on %s:%d  policy=%s", args.bind, args.port, args.policy)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
