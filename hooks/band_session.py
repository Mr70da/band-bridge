"""Claude Code hook for the session board (SessionStart, UserPromptSubmit, Notification, Stop, SessionEnd).
Forwards the event to the bridge. Always exits 0 and quickly, prints nothing."""
import json
import os
import sys
import urllib.request
from pathlib import Path


def build(event: dict) -> dict:
    cwd, message = event.get("cwd"), event.get("message")
    return {"session_id": str(event.get("session_id") or ""),
            "project": (Path(cwd).name if isinstance(cwd, str) else "") or "claude",
            "event": str(event.get("hook_event_name") or ""),
            "message": message if isinstance(message, str) else ""}


def main():
    try:
        event = json.load(sys.stdin)
        payload = build(event if isinstance(event, dict) else {})
    except Exception:
        return
    if not payload["session_id"] or not payload["event"]:
        return
    url = f"http://127.0.0.1:{os.environ.get('BAND_BRIDGE_PORT', '8765')}/sessions/event"
    try:
        urllib.request.urlopen(urllib.request.Request(url, json.dumps(payload).encode(),
                                                      {"Content-Type": "application/json"}), timeout=2).close()
    except Exception:  # bridge not running
        pass


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.exit(0)
