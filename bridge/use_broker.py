"""Points Claude Code's PreToolUse hook at the local broker, or back again.

    python3 bridge/use_broker.py            # use the broker
    python3 bridge/use_broker.py --revert   # restore the previous hook

Writes a timestamped backup next to settings.json before changing anything.
"""

import argparse
import json
import shutil
import time
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
MATCHER = "Bash|Edit|Write|NotebookEdit|AskUserQuestion|WebFetch|Task"
BROKER_HOOK = [{
    "matcher": MATCHER,
    "hooks": [{"type": "http", "url": "http://127.0.0.1:8787/hook", "timeout": 60}],
}]
LEGACY_HOOK = [{
    "matcher": "Bash|AskUserQuestion",
    "hooks": [{"type": "http", "url": "http://192.168.0.170/approve", "timeout": 60}],
}]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revert", action="store_true",
                        help="restore the pre-broker hook (device on :80/approve)")
    args = parser.parse_args()

    config = json.loads(SETTINGS.read_text())
    backup = SETTINGS.with_suffix(f".json.bak-{int(time.time())}")
    shutil.copy2(SETTINGS, backup)

    config.setdefault("hooks", {})["PreToolUse"] = LEGACY_HOOK if args.revert else BROKER_HOOK
    SETTINGS.write_text(json.dumps(config, indent=2) + "\n")

    print(f"backup  {backup}")
    print(f"hook    {json.dumps(config['hooks']['PreToolUse'][0], indent=8)}")
    print("\nStart the broker if it is not running:")
    print("  .venv/bin/python -m bridge.broker.server --token <BROKER_TOKEN> --verbose")


if __name__ == "__main__":
    main()
