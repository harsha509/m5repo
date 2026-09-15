"""Bridges Claude Code CLI permission prompts to an M5Stack Cardputer running
the Hardware Buddy firmware, standing in for the Claude desktop app's BLE central."""

import argparse
import asyncio
import json
import logging
import re
import time
from collections import deque
from datetime import datetime

from bleak import BleakClient, BleakScanner

NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

DEVICE_PREFIX = "Claude"
HEARTBEAT_SECONDS = 10.0
SCAN_SECONDS = 8.0
RECENT_ENTRIES = 3

log = logging.getLogger("buddy")


class BuddyLink:
    """Owns the BLE connection and the newline-delimited JSON framing."""

    def __init__(self, owner_name):
        self.owner_name = owner_name
        self.client = None
        self.rx_buffer = bytearray()
        self.pending = {}
        self.active_prompt = None
        self.entries = deque(maxlen=RECENT_ENTRIES)
        self.approved = 0
        self.denied = 0

    @property
    def connected(self):
        return self.client is not None and self.client.is_connected

    async def connect_forever(self):
        """Reconnects indefinitely; the firmware re-advertises after every drop."""
        while True:
            try:
                device = await self._scan()
                if device is None:
                    log.warning("no %s* device found, retrying", DEVICE_PREFIX)
                    continue
                await self._session(device)
            except Exception as exc:
                log.warning("link error: %s", exc)
            finally:
                self.client = None
                self._fail_pending()
            await asyncio.sleep(3)

    async def _scan(self):
        log.info("scanning for %s*", DEVICE_PREFIX)
        found = await BleakScanner.discover(timeout=SCAN_SECONDS)
        for device in found:
            if device.name and device.name.startswith(DEVICE_PREFIX):
                log.info("found %s (%s)", device.name, device.address)
                return device
        return None

    async def _session(self, device):
        """Holds one connection open, pumping heartbeats until it drops."""
        async with BleakClient(device) as client:
            self.client = client
            log.info("connected to %s", device.name)
            await client.start_notify(NUS_TX, self._on_notify)
            await self._handshake()
            while client.is_connected:
                await self.send_heartbeat()
                await asyncio.sleep(HEARTBEAT_SECONDS)

    async def _handshake(self):
        offset = -time.altzone if time.localtime().tm_isdst else -time.timezone
        await self.send({"time": [int(time.time()), offset]})
        await self.send({"cmd": "owner", "name": self.owner_name})

    async def send(self, obj):
        if not self.connected:
            raise RuntimeError("not connected")
        payload = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        await self.client.write_gatt_char(NUS_RX, payload, response=False)

    async def send_heartbeat(self, prompt=None):
        """Sends the snapshot the firmware renders. Falls back to the active prompt so
        the periodic heartbeat re-asserts a pending approval instead of clearing it."""
        prompt = prompt if prompt is not None else self.active_prompt
        snapshot = {
            "total": 1,
            "running": 0 if prompt else 1,
            "waiting": 1 if prompt else 0,
            "msg": f"approve: {prompt['tool']}" if prompt else "idle",
            "entries": list(self.entries),
            "tokens": 0,
            "tokens_today": 0,
        }
        if prompt:
            snapshot["prompt"] = prompt
        await self.send(snapshot)

    def _on_notify(self, _characteristic, data):
        self.rx_buffer.extend(data)
        while b"\n" in self.rx_buffer:
            line, _, rest = self.rx_buffer.partition(b"\n")
            self.rx_buffer = bytearray(rest)
            if line.strip():
                self._on_line(line)

    def _on_line(self, raw):
        try:
            msg = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            log.debug("unparsed from device: %r", raw[:120])
            return
        log.debug("device -> %s", msg)
        if msg.get("cmd") == "permission":
            self._resolve(msg.get("id"), msg.get("decision"))

    def _resolve(self, request_id, decision):
        future = self.pending.pop(request_id, None)
        if future is None or future.done():
            return
        if decision == "once":
            self.approved += 1
        else:
            self.denied += 1
        future.set_result(decision)

    def _fail_pending(self):
        for future in self.pending.values():
            if not future.done():
                future.set_result(None)
        self.pending.clear()

    async def request_approval(self, request_id, tool, hint, timeout):
        """Returns "once", "deny", or None when the device does not answer in time."""
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        self.active_prompt = {"id": request_id, "tool": tool, "hint": hint}
        await self.send_heartbeat()
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self.pending.pop(request_id, None)
            self.active_prompt = None
            if self.connected:
                await self.send_heartbeat()

    def note_activity(self, text):
        self.entries.appendleft(f"{datetime.now():%H:%M} {text}"[:48])


GATED_PATTERNS = (
    r"\bgit\s+(commit|push)\b",
    r"\brm\s+-[a-z]*[rf]",
    r"\b(drop|truncate)\s+(table|database)\b",
    r"--force\b|--hard\b",
    r"\b(prod|production)\b",
    r"\b(kubectl|terraform|aws|gcloud)\s+(apply|destroy|delete)\b",
)


def should_gate(tool_name, hint, policy="all"):
    """Decides what reaches the device: every Bash call, or only the risky ones."""
    if tool_name != "Bash":
        return False
    if policy == "all":
        return True
    return any(re.search(p, hint, re.IGNORECASE) for p in GATED_PATTERNS)


def summarize(tool_name, tool_input):
    """One short line describing a tool call, for the device's small display."""
    if tool_name == "Bash":
        return tool_input.get("command", "")
    for key in ("file_path", "path", "url", "pattern"):
        if key in tool_input:
            return str(tool_input[key])
    return tool_name


class HookServer:
    """Minimal HTTP endpoint that Claude Code's PreToolUse http hook posts to."""

    def __init__(self, link, timeout):
        self.link = link
        self.timeout = timeout

    async def handle(self, reader, writer):
        try:
            body = await self._read_request(reader)
            decision = await self._decide(body)
            self._respond(writer, decision)
            await writer.drain()
        except Exception as exc:
            log.warning("hook error: %s", exc)
        finally:
            writer.close()

    async def _read_request(self, reader):
        headers = await reader.readuntil(b"\r\n\r\n")
        length = 0
        for line in headers.decode("latin-1").split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip())
        raw = await reader.readexactly(length) if length else b"{}"
        return json.loads(raw or b"{}")

    async def _decide(self, hook_input):
        tool = hook_input.get("tool_name", "unknown")
        hint = summarize(tool, hook_input.get("tool_input", {}))
        request_id = hook_input.get("tool_use_id") or str(time.time())
        if not should_gate(tool, hint):
            return None
        if not self.link.connected:
            log.info("device offline, deferring %s to the terminal", tool)
            return None
        log.info("asking device: %s %s", tool, hint[:60])
        verdict = await self.link.request_approval(request_id, tool, hint, self.timeout)
        self.link.note_activity(hint or tool)
        return verdict

    def _respond(self, writer, decision):
        """Silence means no decision, so Claude Code falls back to the normal prompt."""
        if decision == "once":
            output = {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "approved on Cardputer",
            }
        elif decision == "deny":
            output = {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "denied on Cardputer",
            }
        else:
            output = None
        payload = json.dumps({"hookSpecificOutput": output} if output else {}).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(payload)}\r\n\r\n".encode()
            + payload
        )


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--owner", default="Sri Harsha")
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    link = BuddyLink(args.owner)
    server = await asyncio.start_server(
        HookServer(link, args.timeout).handle, args.host, args.port
    )
    log.info("hook endpoint on http://%s:%d", args.host, args.port)
    await asyncio.gather(link.connect_forever(), server.serve_forever())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
