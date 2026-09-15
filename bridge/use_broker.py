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



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revert", action="store_true",
                        help="remove the PreToolUse hook entirely")
    args = parser.parse_args()

    config = json.loads(SETTINGS.read_text())
    backup = SETTINGS.with_suffix(f".json.bak-{int(time.time())}")
    shutil.copy2(SETTINGS, backup)

    hooks = config.setdefault("hooks", {})
    if args.revert:
        # The device serves no /approve any more, so there is no earlier hook to
        # go back to — removing it returns Claude Code to its own prompting.
        hooks.pop("PreToolUse", None)
    else:
        hooks["PreToolUse"] = BROKER_HOOK
    SETTINGS.write_text(json.dumps(config, indent=2) + "\n")

    print(f"backup  {backup}")
    print("hook    removed" if args.revert
          else f"hook    {json.dumps(hooks['PreToolUse'][0], indent=8)}")
    print("\nStart the broker if it is not running:")
    print("  .venv/bin/python -m bridge.broker.server --token <BROKER_TOKEN> --verbose")


if __name__ == "__main__":
    main()
