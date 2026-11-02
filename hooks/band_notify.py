"""Claude Code hook: `band_notify.py input` (Notification hook) or `band_notify.py done` (Stop hook).
Sends nothing while the user is at the PC (BAND_BRIDGE_ALWAYS=1 to always send).
Always exits 0 and quickly: a hook must never block or break Claude Code."""
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)  # hooks run from any cwd
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def read_event() -> dict:
    try:
        data = json.load(sys.stdin)
        return data if isinstance(data, dict) else {}
    except Exception:  # no or bad JSON on stdin
        return {}


def project(event: dict) -> str:
    cwd = event.get("cwd")
    return (Path(cwd).name if isinstance(cwd, str) else "") or "Claude Code"


def first_line(text, limit: int) -> str:
    if not isinstance(text, str):
        return ""
    return next((line.strip() for line in text.splitlines() if line.strip()), "")[:limit]


def present() -> bool:
    """True when the user is at the PC and the band should stay quiet."""
    if os.environ.get("BAND_BRIDGE_ALWAYS") == "1":
        return False
    try:
        from bridge.idle import away
        return not away()
    except Exception:  # can't tell: better a buzz too many than a missed one
        return False


def bridge_url(path: str) -> str:
    return f"http://127.0.0.1:{os.environ.get('BAND_BRIDGE_PORT', '8765')}{path}"


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "done"
    event = read_event()
    if present():
        return
    text = "Task done"
    if kind == "input":
        text = "Claude needs your input"
        message = event.get("message")
        if isinstance(message, str) and message.strip():
            text += ": " + message.strip()[:200]
    else:
        last = first_line(event.get("last_assistant_message"), 150)
        if last:
            text += ": " + last
    body = json.dumps({"text": text, "title": project(event)}).encode()
    req = urllib.request.Request(bridge_url("/notify"), body, {"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=2).close()
    except Exception:  # bridge not running, band not connected, ...
        pass


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.exit(0)
