"""Claude Code hook: `band_notify.py input` (Notification hook) or `band_notify.py done` (Stop hook).
Sends nothing while the user is at the PC (BAND_BRIDGE_ALWAYS=1 to always send).
Stop hook "wrist remote": when away, asks "Next?" on the band and prints {"decision":"block","reason":...} so
Claude continues; any other outcome prints nothing (BAND_BRIDGE_REMOTE=0: plain "Task done" notify instead).
Always exits 0 and quickly: a hook must never block or break Claude Code."""
import json
import os
import sys
import threading
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


BACK_AFTER = 5  # seconds of input after which the user is considered back
POLL = 1.0
OPTIONS = ["Keep going", "Run tests", "Commit", "Stop"]
REASONS = {
    "keep going": "keep going with the task.",
    "run tests": "run the project's tests and fix any failures.",
    "commit": "commit the current work with a clear message.",
}
ALIASES = {"yes": "keep going", "continue": "keep going"}


def next_step(reply: dict):
    """The Stop-hook reason for the band's reply, or None (stop)."""
    answer = str(reply.get("answer", "")).strip().lower()
    option = reply.get("option")
    key = OPTIONS[option - 1].lower() if option in (1, 2, 3, 4) else ALIASES.get(answer, answer)  # option is 1-based
    return "The user says from their band: " + REASONS[key] if key in REASONS else None


def ask(question: str, timeout: float, out: dict):
    body = json.dumps({"question": question, "options": OPTIONS, "timeout": timeout}).encode()
    req = urllib.request.Request(bridge_url("/ask"), body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout + 10) as r:
            out["reply"] = json.load(r)
    except Exception:  # 503, 504, bridge down, bad JSON: Claude just stops
        pass


def remote(event: dict):
    """Ask what to do next; wait for the answer unless the user comes back to the PC."""
    from bridge.idle import idle_seconds
    timeout = float(os.environ.get("BAND_BRIDGE_REMOTE_TIMEOUT", 240))
    question = f"{project(event)}: {first_line(event.get('last_assistant_message'), 150)}  Next?"
    out = {}
    t = threading.Thread(target=ask, args=(question, timeout, out), daemon=True)
    t.start()
    while t.is_alive():
        t.join(POLL)
        if t.is_alive() and os.environ.get("BAND_BRIDGE_ALWAYS") != "1" and idle_seconds() < BACK_AFTER:
            return
    reason = next_step(out["reply"]) if isinstance(out.get("reply"), dict) else None
    if reason:  # stop_hook_active is ignored on purpose: chained steps are wanted, Claude Code caps loops itself
        print(json.dumps({"decision": "block", "reason": reason}))


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "done"
    event = read_event()
    if present():
        return
    if kind == "done" and os.environ.get("BAND_BRIDGE_REMOTE") != "0":
        return remote(event)
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
    except BaseException:  # never break Claude Code, never continue on an error
        pass
    sys.stdout.flush()
    os._exit(0)  # don't wait for the daemon thread
