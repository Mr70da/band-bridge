import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import setup_claude as sc

PY, REPO = "C:/x/.venv/Scripts/python.exe", "C:/x"
FOREIGN = {"matcher": "Bash", "hooks": [{"type": "command", "command": "other.exe"}]}


def ours(settings):
    return [h["command"] for gs in settings.get("hooks", {}).values() for g in gs for h in g["hooks"]
            if sc.is_ours(h["command"])]


class SetupClaudeTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = Path(d.name) / "settings.json"

    def run_main(self, *args):
        with mock.patch.object(sc, "claude", return_value=None), contextlib.redirect_stdout(io.StringIO()) as out:
            sc.main(["--settings", str(self.path), *args])
        return out.getvalue()

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_fresh_install(self):
        s = sc.install({}, PY, REPO)
        self.assertEqual(s["env"]["MCP_TOOL_TIMEOUT"], "600000")
        self.assertEqual(len(ours(s)), 11)
        stop = s["hooks"]["Stop"][0]["hooks"]
        self.assertEqual(stop[0], {"type": "command", "timeout": 300,
                                   "command": f'"{PY}" "{REPO}/hooks/band_notify.py" done'})
        self.assertEqual(s["hooks"]["PreToolUse"][0]["matcher"], "Bash")

    def test_idempotent_and_env_kept(self):
        once = sc.install({"env": {"MCP_TOOL_TIMEOUT": "1"}}, PY, REPO)
        self.assertEqual(sc.install(once, PY, REPO), once)
        self.assertEqual(once["env"]["MCP_TOOL_TIMEOUT"], "1")

    def test_keeps_foreign_and_uninstall(self):
        base = {"model": "x", "hooks": {"PreToolUse": [FOREIGN], "Other": [FOREIGN]}}
        s = sc.install(base, PY, REPO)
        self.assertIn(FOREIGN, s["hooks"]["PreToolUse"])
        self.assertEqual(s["model"], "x")
        back = sc.strip_ours(s)
        self.assertEqual(back["hooks"], base["hooks"])
        self.assertEqual(ours(back), [])

    def test_no_guard(self):
        s = sc.install({}, PY, REPO, guard=False)
        self.assertNotIn("PreToolUse", s["hooks"])
        self.assertEqual(len(ours(s)), 10)

    def test_dry_run_writes_nothing(self):
        out = self.run_main("--dry-run")
        self.assertFalse(self.path.exists())
        self.assertIn("band_guard.py", out)

    def test_main_backup_and_uninstall(self):
        self.path.write_text(json.dumps({"hooks": {"Stop": [FOREIGN]}}), encoding="utf-8")
        self.run_main()
        self.assertEqual(len(list(self.path.parent.glob("settings.json.bak-*"))), 1)
        self.assertEqual(len(ours(self.read())), 11)
        self.run_main("--uninstall")
        self.assertEqual(self.read()["hooks"], {"Stop": [FOREIGN]})


if __name__ == "__main__":
    unittest.main()
