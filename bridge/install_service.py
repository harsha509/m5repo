"""Installs the broker as a launchd user agent so it survives logout and reboot.

    python3 bridge/install_service.py --token <BROKER_TOKEN>
    python3 bridge/install_service.py --uninstall

The plist lands in ~/Library/LaunchAgents, never in the repo, because it carries
the broker token.
"""

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.m5.broker"
REPO = Path(__file__).resolve().parents[1]
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = Path.home() / "Library" / "Logs"


def interpreter() -> Path:
    venv = REPO / ".venv" / "bin" / "python"
    return venv if venv.exists() else Path(sys.executable)


def definition(token: str, port: int) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [str(interpreter()), "-m", "bridge.broker.server",
                             "--token", token, "--port", str(port)],
        "WorkingDirectory": str(REPO),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(LOG_DIR / f"{LABEL}.log"),
        "StandardErrorPath": str(LOG_DIR / f"{LABEL}.err"),
    }


def bootout() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                   capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.uninstall:
        bootout()
        PLIST.unlink(missing_ok=True)
        print(f"removed {PLIST}")
        return 0

    if not args.token:
        parser.error("--token is required (the BROKER_TOKEN from wifi_config.h)")

    payload = plistlib.dumps(definition(args.token, args.port))
    if args.dry_run:
        print(payload.decode())
        return 0

    PLIST.parent.mkdir(parents=True, exist_ok=True)
    bootout()
    PLIST.write_bytes(payload)
    PLIST.chmod(0o600)
    done = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)],
                          capture_output=True, text=True)
    if done.returncode != 0:
        print(f"launchctl bootstrap failed: {done.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"installed {PLIST}\nlogs     {LOG_DIR / (LABEL + '.log')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
