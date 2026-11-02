"""Register Band Bridge with Claude Code for this checkout: the MCP server plus the hooks in settings.json.

    python setup_claude.py [--dry-run] [--uninstall] [--no-guard] [--settings PATH]

Idempotent: our hook entries are recognised by their hooks/band_*.py path and replaced, never duplicated.
Every unrelated key and hook in settings.json is kept; a backup is written before each change.
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
SETTINGS = Path.home() / ".claude" / "settings.json"
MCP_NAME = "band-bridge"
FILES = ("band_notify", "band_session", "band_permission", "band_guard", "band_stuck")

# (event, matcher, [(script, arg, timeout)]) -> one settings group each
HOOKS = [
    ("Notification", None, [("band_notify", "input", 5), ("band_session", None, 5)]),
    ("Stop", None, [("band_notify", "done", 300), ("band_session", None, 5)]),
    ("PermissionRequest", None, [("band_permission", None, 300)]),
    ("SessionStart", None, [("band_session", None, 5)]),
    ("UserPromptSubmit", None, [("band_session", None, 5)]),
    ("SessionEnd", None, [("band_session", None, 5)]),
    ("PreToolUse", "Bash", [("band_guard", None, 150)]),
    ("PostToolUse", "Bash", [("band_stuck", None, 300)]),
    ("PostToolUseFailure", "Bash", [("band_stuck", None, 300)]),
]


def is_ours(command) -> bool:
    c = str(command).replace("\\", "/")
    return any(f"hooks/{f}.py" in c for f in FILES)


def strip_ours(settings: dict) -> dict:
    """Copy of settings without our hook entries (empty groups and events are dropped)."""
    out = dict(settings)
    hooks = {}
    for event, groups in (settings.get("hooks") or {}).items():
        kept = []
        for g in groups:
            rest = [h for h in g.get("hooks", []) if not is_ours(h.get("command", ""))]
            if rest or "hooks" not in g:
                kept.append({**g, "hooks": rest} if "hooks" in g else g)
        if kept:
            hooks[event] = kept
    if hooks:
        out["hooks"] = hooks
    else:
        out.pop("hooks", None)
    return out


def install(settings: dict, python: str, repo: str, guard: bool = True) -> dict:
    out = strip_ours(settings)
    out["env"] = {**out.get("env", {})}
    out["env"].setdefault("MCP_TOOL_TIMEOUT", "600000")
    hooks = out.setdefault("hooks", {})
    for event, matcher, entries in HOOKS:
        if not guard and any(s == "band_guard" for s, _, _ in entries):
            continue
        cmds = [{"type": "command", "timeout": t,
                 "command": f'"{python}" "{repo}/hooks/{s}.py"' + (f" {a}" if a else "")}
                for s, a, t in entries]
        hooks.setdefault(event, []).append({**({"matcher": matcher} if matcher else {}), "hooks": cmds})
    return out


def posix(p) -> str:
    return str(p).replace("\\", "/")


def venv_python() -> str:
    p = REPO / ".venv" / "Scripts" / "python.exe"
    return posix(p if p.exists() else sys.executable)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def claude(*args):
    exe = shutil.which("claude")
    return subprocess.run([exe, *args], capture_output=True, text=True) if exe else None


def register_mcp(python: str, repo: str, dry: bool):
    cmd = ["mcp", "add", MCP_NAME, "--scope", "user", "--", python, f"{repo}/bridge/mcp_server.py"]
    shown = "claude " + " ".join(f'"{c}"' if " " in c else c for c in cmd)
    if dry:
        return print(f"[dry-run] would run: {shown}")
    got = claude("mcp", "get", MCP_NAME)
    if got is None:
        return print(f"'claude' not found on PATH. Run this yourself:\n  {shown}")
    if got.returncode == 0:
        return print(f"MCP server {MCP_NAME} already registered (remove it first to change the path).")
    r = claude(*cmd)
    print(r.stdout.strip() or r.stderr.strip() or f"claude mcp add exited {r.returncode}")


def unregister_mcp(dry: bool):
    cmd = f"claude mcp remove {MCP_NAME} --scope user"
    if dry:
        return print(f"[dry-run] would run: {cmd}")
    if claude("mcp", "get", MCP_NAME) is None:
        return print(f"'claude' not found on PATH. Run this yourself:\n  {cmd}")
    r = claude("mcp", "remove", MCP_NAME, "--scope", "user")
    print(r.stdout.strip() or r.stderr.strip())


def main(argv=None):
    ap = argparse.ArgumentParser(description="Register Band Bridge with Claude Code (MCP server + hooks).")
    ap.add_argument("--dry-run", action="store_true", help="print the resulting settings.json, change nothing")
    ap.add_argument("--uninstall", action="store_true", help="remove only Band Bridge's entries")
    ap.add_argument("--no-guard", action="store_true", help="skip the PreToolUse band_guard hook")
    ap.add_argument("--settings", type=Path, default=SETTINGS, help=f"settings file (default {SETTINGS})")
    a = ap.parse_args(argv)
    python, repo = venv_python(), posix(REPO)
    old = load(a.settings)
    new = strip_ours(old) if a.uninstall else install(old, python, repo, not a.no_guard)
    text = json.dumps(new, indent=2) + "\n"
    if a.dry_run:
        print(text, end="")
    elif new != old:
        if a.settings.exists():
            bak = a.settings.with_name(f"{a.settings.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(a.settings, bak)
            print(f"backup: {bak}")
        a.settings.parent.mkdir(parents=True, exist_ok=True)
        a.settings.write_text(text, encoding="utf-8")
        print(f"updated {a.settings}")
    else:
        print(f"{a.settings} already up to date")
    if a.uninstall:
        unregister_mcp(a.dry_run)
    else:
        register_mcp(python, repo, a.dry_run)


if __name__ == "__main__":
    main()
