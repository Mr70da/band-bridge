import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))
import band_guard  # noqa: E402
import band_stuck  # noqa: E402


class Reply:
    def __init__(self, body=b"{}"):
        self.body = body

    def read(self, *_):
        return self.body

    def close(self):
        pass

    __enter__ = lambda self: self
    __exit__ = lambda self, *a: None


def run(module, event, env=None):
    out = io.StringIO()
    with mock.patch.dict(os.environ, env or {}), mock.patch.object(sys, "stdin", io.StringIO(json.dumps(event))), \
            contextlib.redirect_stdout(out):
        module.main()
    return out.getvalue()


DANGER = ["git push --force", "git push -f origin x", "git push --force-with-lease", "git push origin main",
          "git push origin HEAD:master", "git reset --hard HEAD~1", "git clean -fd", "git branch -D old",
          "rm -rf /x", "rm -fr x", "rm -r x", "rm -Rf x", "rm -f -r x", "Remove-Item x -Recurse -Force",
          "del /s /q x", "rmdir /s x", "format c:", "psql -c 'drop table users'", "TRUNCATE users",
          "kubectl delete pod x", "terraform destroy", "terraform apply", "docker system prune -a",
          "npm publish", "./deploy.sh", 'grep "rm -rf" notes.txt']
SAFE = ["rm file.txt", "rm -f file.txt", "git push", "git push origin feature/x", "git push -u origin fix-main-bug",
        "git reset --soft HEAD~1", "git branch -d old", "git clean -n", "ls -la", "npm test", "del file.txt",
        "terraform plan", "docker ps", "echo format"]


class GuardTest(unittest.TestCase):
    EVENT = {"cwd": "C:/x/proj", "tool_name": "Bash", "tool_input": {"command": "rm -rf build"}}

    def setUp(self):
        p = mock.patch("urllib.request.urlopen")
        self.urlopen = p.start()
        self.addCleanup(p.stop)

    def result(self, **kw):
        out = run(band_guard, self.EVENT, **kw)
        return json.loads(out)["hookSpecificOutput"] if out else None

    def test_table(self):
        for c in DANGER:
            self.assertTrue(band_guard.dangerous(c), c)
        for c in SAFE:
            self.assertFalse(band_guard.dangerous(c), c)

    def test_safe_is_silent_and_never_asks(self):
        self.assertEqual(run(band_guard, {"tool_input": {"command": "ls"}}), "")
        self.urlopen.assert_not_called()

    def test_asks_and_allows(self):
        self.urlopen.return_value = Reply(b'{"answer": "Allow", "option": 1}')
        r = self.result()
        self.assertEqual((r["hookEventName"], r["permissionDecision"]), ("PreToolUse", "allow"))
        req = self.urlopen.call_args.args[0]
        self.assertEqual(json.loads(req.data),
                         {"question": "proj: run this? rm -rf build", "options": ["Allow", "Deny"], "timeout": 120})

    def test_deny_and_failures_fail_closed(self):
        self.urlopen.return_value = Reply(b'{"answer": "Deny", "option": 2}')
        self.assertEqual(self.result()["permissionDecisionReason"], "Denied on the band")
        for failure in (urllib.error.HTTPError("u", 503, "x", {}, None), urllib.error.HTTPError("u", 504, "x", {}, None),
                        OSError("refused"), TimeoutError()):
            self.urlopen.side_effect = failure
            r = self.result()
            self.assertEqual(r["permissionDecision"], "deny")
            self.assertIn("no answer", r["permissionDecisionReason"])
        self.urlopen.side_effect = None
        for body in (b"garbage", b"{}"):
            self.urlopen.return_value = Reply(body)
            self.assertEqual(self.result()["permissionDecision"], "deny")

    def test_disabled(self):
        self.assertEqual(run(band_guard, self.EVENT, {"BAND_BRIDGE_GUARD": "0"}), "")
        self.urlopen.assert_not_called()

    def test_timeout_env(self):
        self.urlopen.return_value = Reply(b'{"option": 1}')
        self.result(env={"BAND_BRIDGE_GUARD_TIMEOUT": "30"})
        self.assertEqual(json.loads(self.urlopen.call_args.args[0].data)["timeout"], 30)


class StuckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patches = [mock.patch("tempfile.gettempdir", return_value=self.tmp.name),
                   mock.patch("urllib.request.urlopen", return_value=Reply(b'{"option": 3}')),
                   mock.patch("bridge.idle.away", return_value=True)]
        self.gettemp, self.urlopen, self.away = (p.start() for p in patches)
        for p in patches:
            self.addCleanup(p.stop)

    def ev(self, fail=True, sid="s1"):
        if fail:
            return {"session_id": sid, "cwd": "C:/x/proj", "hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
                    "tool_input": {"command": "npm test"}, "error": "Exit code 1\nboom"}
        return {"session_id": sid, "cwd": "C:/x/proj", "hook_event_name": "PostToolUse", "tool_name": "Bash",
                "tool_input": {"command": "npm test"}, "tool_response": {"stdout": "ok", "stderr": "", "interrupted": False}}

    def count(self, sid="s1"):
        return json.loads(band_stuck.counter_path({"session_id": sid}).read_text())["count"]

    def test_failed_detection(self):
        f = band_stuck.failed
        self.assertTrue(f(self.ev(True)))
        self.assertFalse(f(self.ev(False)))
        self.assertTrue(f({"tool_response": {"stdout": "", "error": "x"}}))
        self.assertTrue(f({"tool_response": {"exitCode": 2}}))
        self.assertFalse(f({"hook_event_name": "PostToolUseFailure", "is_interrupt": True}))

    def test_counter_increments_resets_and_is_per_session(self):
        run(band_stuck, self.ev(True))
        run(band_stuck, self.ev(True))
        run(band_stuck, self.ev(True, "s2"))
        self.assertEqual((self.count(), self.count("s2")), (2, 1))
        run(band_stuck, self.ev(False))
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.count("s2"), 1)
        self.urlopen.assert_not_called()

    def trigger(self, reply=None, away=True):
        self.away.return_value = away
        if reply is not None:
            self.urlopen.return_value = Reply(json.dumps(reply).encode())
        out = ""
        for _ in range(3):
            out = run(band_stuck, self.ev(True))
        return json.loads(out) if out else None

    def test_triggers_at_three_when_away_and_resets(self):
        self.trigger({"option": 1})
        req = self.urlopen.call_args.args[0]
        self.assertEqual(json.loads(req.data), {
            "question": "proj: Claude failed 3 times in a row: npm test. What now?",
            "options": ["Keep going", "Try another way", "Stop"], "timeout": 240})
        self.assertEqual(self.count(), 0)

    def test_not_asked_when_present(self):
        self.assertIsNone(self.trigger(away=False))
        self.urlopen.assert_not_called()

    def test_answer_mapping(self):
        self.assertIsNone(self.trigger({"option": 1}))
        r = self.trigger({"answer": "Try another way", "option": 2})
        self.assertIn("try a different approach", r["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(r["hookSpecificOutput"]["hookEventName"], "PostToolUseFailure")
        self.assertNotIn("continue", r)
        r = self.trigger({"answer": "Stop", "option": 3})
        self.assertIs(r["continue"], False)
        self.assertIn("summarize", r["stopReason"])

    def test_errors_and_unclear_do_nothing(self):
        for failure in (urllib.error.HTTPError("u", 504, "x", {}, None), OSError("refused")):
            self.urlopen.side_effect = failure
            self.assertIsNone(self.trigger())
        self.urlopen.side_effect = None
        self.assertIsNone(self.trigger({"answer": "dunno"}))


if __name__ == "__main__":
    unittest.main()
