"""Claude Code PermissionRequest hook: when the user is away, ask Allow/Deny on the band.
Prints a decision only on a clear Allow or Deny; every other outcome (timeout, band offline, error, user
back at the PC) prints nothing so Claude Code shows its normal dialog. Always exits 0."""
import json
import os
import sys
import threading
import urllib.request

from band_notify import bridge_url, present, project, read_event  # the script's own dir is on sys.path

BACK_AFTER = 5  # seconds of input after which the user is considered back
POLL = 1.0
KEYS = {"Bash": "command", "Edit": "file_path", "Write": "file_path", "Read": "file_path", "WebFetch": "url"}


def summary(tool, tool_input) -> str:
    if isinstance(tool_input, dict) and isinstance(tool_input.get(KEYS.get(tool)), str):
        text = tool_input[KEYS[tool]]
    else:
        text = json.dumps(tool_input, separators=(",", ":"), ensure_ascii=False)
    return " ".join(text.split())[:150]


def decision(reply: dict):
    """'allow', 'deny' or None (unclear: let the user decide at the terminal)."""
    answer = str(reply.get("answer", "")).strip().lower()
    if reply.get("option") == 1 or answer in ("allow", "yes"):
        return "allow"
    if reply.get("option") == 2 or answer in ("deny", "no", "stop"):
        return "deny"
    return None


def ask(question: str, timeout: float, out: dict):
    body = json.dumps({"question": question, "options": ["Allow", "Deny"], "timeout": timeout}).encode()
    req = urllib.request.Request(bridge_url("/ask"), body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout + 10) as r:
            out["reply"] = json.load(r)
    except Exception:  # 503, 504, bridge down, bad JSON: no decision
        pass


def main():
    event = read_event()
    if present():
        return
    from bridge.idle import idle_seconds  # band_notify put ROOT on sys.path
    timeout = float(os.environ.get("BAND_BRIDGE_PERMISSION_TIMEOUT", 240))
    question = f"{project(event)}: allow {event.get('tool_name')}? {summary(event.get('tool_name'), event.get('tool_input'))}"
    out = {}
    t = threading.Thread(target=ask, args=(question, timeout, out), daemon=True)
    t.start()
    while t.is_alive():
        t.join(POLL)
        if t.is_alive() and os.environ.get("BAND_BRIDGE_ALWAYS") != "1" and idle_seconds() < BACK_AFTER:
            return  # back at the PC: the terminal dialog takes over
    verdict = decision(out["reply"]) if "reply" in out else None
    if verdict:
        result = {"behavior": verdict}
        if verdict == "deny":
            result["message"] = "Denied on the band"
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": result}}))


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # never break Claude Code, never allow on an error
        pass
    sys.stdout.flush()
    os._exit(0)  # don't wait for the daemon thread
