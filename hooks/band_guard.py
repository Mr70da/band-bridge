"""Claude Code PreToolUse hook (matcher Bash): dangerous commands need a band tap, even at the PC.
Safe commands: exit 0, no output. Dangerous: ask Allow/Deny on the band; anything but a clear Allow
(Deny, timeout, bridge down, error) DENIES (fail closed). BAND_BRIDGE_GUARD=0 disables the hook.
Always exits 0. Patterns match anywhere in the command, so `grep "rm -rf"` is flagged too (accepted).
Extend by adding to DANGEROUS."""
import json
import os
import re
import sys
import threading
import urllib.request

from band_notify import bridge_url, project, read_event  # the script's own dir is on sys.path

DANGEROUS = [re.compile(p, f) for p, f in (
    (r"\bgit\s+push\b.*(\s--force(-with-lease)?\b|\s-[a-z]*f[a-z]*\b)", re.I),  # force push
    (r"\bgit\s+push\b.*[\s:/](main|master)\b", re.I),                          # push to main/master
    (r"\bgit\s+reset\s+.*--hard\b", re.I),
    (r"\bgit\s+clean\s+.*-[a-z]*f", re.I),
    (r"\bgit\s+branch\s+.*-D\b", 0),                                           # case-sensitive: -d is safe
    (r"\brm\s+(-\w+\s+)*-\w*r\w*\b", re.I),                                    # rm -r / -rf / -fr / -Rf
    (r"\brm\s+.*--recursive\b", re.I),
    (r"\bRemove-Item\b.*-Recurse\b", re.I),
    (r"\bdel\b.*\s/s\b", re.I),
    (r"\brmdir\b.*\s/s\b", re.I),
    (r"\bformat\s+[a-z]:", re.I),
    (r"\bDROP\s+(TABLE|DATABASE)\b", re.I),
    (r"\bTRUNCATE\s+\w", 0),  # SQL as usually written; lowercase "truncate" is too common in prose
    (r"\btruncate\s+table\b", re.I),
    (r"\bkubectl\s+delete\b", re.I),
    (r"\bterraform\s+(destroy|apply)\b", re.I),
    (r"\bdocker\s+system\s+prune\b", re.I),
    (r"\bnpm\s+publish\b", re.I),
    (r"(^|[;&|]\s*)\S*deploy\S*(\s|$)|\b(npm|yarn|pnpm)\s+run\s+deploy\b|\bmake\s+deploy\b", re.I),  # a deploy command, not the word in a message
)]


def dangerous(command) -> bool:
    return isinstance(command, str) and any(p.search(command) for p in DANGEROUS)


def verdict(reply: dict) -> bool:
    """True only for a clear Allow."""
    return reply.get("option") == 1 or str(reply.get("answer", "")).strip().lower() in ("allow", "yes")


def ask(question: str, timeout: float, out: dict):
    body = json.dumps({"question": question, "options": ["Allow", "Deny"], "timeout": timeout}).encode()
    req = urllib.request.Request(bridge_url("/ask"), body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout + 10) as r:
            out["reply"] = json.load(r)
    except Exception:  # 503, 504, bridge down, bad JSON: no tap
        pass


def emit(decision: str, reason: str):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": decision, "permissionDecisionReason": reason}}))


def main():
    if os.environ.get("BAND_BRIDGE_GUARD") == "0":
        return
    event = read_event()
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not dangerous(command):
        return
    timeout = float(os.environ.get("BAND_BRIDGE_GUARD_TIMEOUT", 120))
    out = {}
    question = f"{project(event)}: run this? {' '.join(command.split())[:150]}"
    t = threading.Thread(target=ask, args=(question, timeout, out), daemon=True)
    t.start()
    t.join(timeout + 12)
    reply = out.get("reply")
    if not isinstance(reply, dict):
        emit("deny", "Blocked: needs confirmation on the user's band (no answer)")
    elif verdict(reply):
        emit("allow", "Allowed on the band")
    else:
        emit("deny", "Denied on the band")


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # an error on a dangerous command must not allow it
        try:
            emit("deny", "Blocked: needs confirmation on the user's band (no answer)")
        except BaseException:
            pass
    sys.stdout.flush()
    os._exit(0)
