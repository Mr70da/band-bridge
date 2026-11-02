"""Claude Code hook: `band_notify.py input` (Notification hook) or `band_notify.py done` (Stop hook).
Always exits 0 and quickly: a hook must never block or break Claude Code."""
import json
import os
import sys
import urllib.request


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "done"
    text = "Task done"
    if kind == "input":
        text = "Claude needs your input"
        try:
            message = json.load(sys.stdin).get("message")
            if isinstance(message, str) and message.strip():
                text += ": " + message.strip()[:200]
        except Exception:  # no or bad JSON on stdin
            pass
    port = os.environ.get("BAND_BRIDGE_PORT", "8765")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/notify", json.dumps({"text": text}).encode(),
                                 {"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=2).close()
    except Exception:  # bridge not running, band not connected, ...
        pass


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.exit(0)
