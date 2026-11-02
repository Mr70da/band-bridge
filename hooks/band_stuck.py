"""Claude Code hook for Bash, events PostToolUse AND PostToolUseFailure: detect Claude looping on failures.
A non-zero Bash exit fires PostToolUseFailure (error "Exit code N..."), a success fires PostToolUse.
Counts consecutive failures per session in %TEMP%\\band-bridge\\stuck-<session_id>.json; a success resets.
At 3, only if the user is away, asks on the band what to do, then resets. Always exits 0."""
import json
import os
import re
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

from band_notify import bridge_url, project, read_event  # also puts the repo root on sys.path

LIMIT = 3
OPTIONS = ["Keep going", "Try another way", "Stop"]
CONTEXT = {2: "The user (from their band) says: stop retrying the same thing; step back and try a different approach.",
           3: "The user (from their band) says: stop and summarize what is failing; do not continue."}


def failed(event: dict) -> bool:
    if event.get("hook_event_name") == "PostToolUseFailure" or event.get("error"):
        return not event.get("is_interrupt")
    resp = event.get("tool_response")
    if isinstance(resp, dict):
        code = resp.get("exitCode", resp.get("exit_code"))
        return bool(resp.get("error")) or (isinstance(code, int) and code != 0)
    return isinstance(resp, str) and bool(re.match(r"\s*Error|Exit code [1-9]", resp))


def counter_path(event: dict) -> Path:
    sid = re.sub(r"[^\w.-]", "_", str(event.get("session_id") or "nosession"))[:80]
    return Path(tempfile.gettempdir()) / "band-bridge" / f"stuck-{sid}.json"


def bump(event: dict, fail: bool) -> int:
    """Update the counter; returns the new value (0 after a success)."""
    path = counter_path(event)
    try:
        n = int(json.loads(path.read_text()).get("count", 0))
    except Exception:
        n = 0
    n = n + 1 if fail else 0
    if fail or path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"count": n}))
    return n


def ask(question: str, out: dict):
    body = json.dumps({"question": question, "options": OPTIONS, "timeout": 240}).encode()
    req = urllib.request.Request(bridge_url("/ask"), body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=250) as r:
            out["reply"] = json.load(r)
    except Exception:
        pass


def choice(reply: dict) -> int:
    """1 keep going, 2 try another way, 3 stop, 0 unclear."""
    if reply.get("option") in (1, 2, 3):
        return reply["option"]
    return {"keep going": 1, "try another way": 2, "stop": 3}.get(str(reply.get("answer", "")).strip().lower(), 0)


def main():
    event = read_event()
    if bump(event, failed(event)) < LIMIT:
        return
    bump(event, False)  # reset after asking
    try:
        from bridge.idle import away
        if not away():
            return
    except Exception:
        return
    tool_input = event.get("tool_input")
    command = " ".join(str(tool_input.get("command", "") if isinstance(tool_input, dict) else "").split())[:80]
    out = {}
    t = threading.Thread(target=ask, args=(f"{project(event)}: Claude failed 3 times in a row: {command}. What now?", out), daemon=True)
    t.start()
    t.join(255)
    pick = choice(out["reply"]) if isinstance(out.get("reply"), dict) else 0
    name = event.get("hook_event_name") or "PostToolUse"
    if pick == 2:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": name, "additionalContext": CONTEXT[2]}}))
    elif pick == 3:
        print(json.dumps({"continue": False, "stopReason": CONTEXT[3],
                          "hookSpecificOutput": {"hookEventName": name, "additionalContext": CONTEXT[3]}}))


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.stdout.flush()
    os._exit(0)
