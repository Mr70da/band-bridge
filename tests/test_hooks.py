import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))
import band_notify  # noqa: E402
import band_permission  # noqa: E402


class Reply:
    def __init__(self, body=b"{}"):
        self.body = body

    def read(self, *_):
        return self.body

    def close(self):
        pass

    __enter__ = lambda self: self
    __exit__ = lambda self, *a: None


def run(module, event, argv=("hook",), env=None):
    """Run module.main() in-process with `event` on stdin; returns captured stdout."""
    stdin = event if isinstance(event, str) else json.dumps(event)
    out = io.StringIO()
    with mock.patch.dict(os.environ, env or {}), mock.patch.object(sys, "argv", list(argv)), \
            mock.patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out):
        module.main()
    return out.getvalue()


def sent(urlopen, n=0):
    req = urlopen.call_args_list[n].args[0]
    return req.full_url, json.loads(req.data)


@mock.patch.dict(os.environ, {"BAND_BRIDGE_PORT": "9999"})
class NotifyTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("BAND_BRIDGE_ALWAYS", None)
        patches = [mock.patch("urllib.request.urlopen", return_value=Reply()),
                   mock.patch("bridge.idle.away", return_value=True)]
        self.urlopen, self.away = (p.start() for p in patches)
        for p in patches:
            self.addCleanup(p.stop)

    def test_present_user_gets_nothing(self):
        self.away.return_value = False
        for kind in ("input", "done"):
            run(band_notify, {"cwd": "C:/x/proj"}, ("hook", kind))
        self.urlopen.assert_not_called()

    def test_always_overrides_presence(self):
        self.away.return_value = False
        run(band_notify, {}, ("hook", "done"), {"BAND_BRIDGE_ALWAYS": "1"})
        self.urlopen.assert_called_once()

    def test_input_names_project_and_message(self):
        run(band_notify, {"cwd": "C:\\Users\\me\\band-bridge", "message": " needs permission "}, ("hook", "input"))
        url, body = sent(self.urlopen)
        self.assertEqual(url, "http://127.0.0.1:9999/notify")
        self.assertEqual(body, {"text": "Claude needs your input: needs permission", "title": "band-bridge"})

    def test_done_uses_first_line_of_last_message(self):
        run(band_notify, {"cwd": "/a/proj", "last_assistant_message": "\n\nFixed the bug.\nMore detail"})
        self.assertEqual(sent(self.urlopen)[1]["text"], "Task done: Fixed the bug.")
        run(band_notify, {"last_assistant_message": "x" * 400})
        self.assertEqual(sent(self.urlopen, 1)[1]["text"], "Task done: " + "x" * 150)

    def test_done_fallbacks(self):
        run(band_notify, {"last_assistant_message": "  "})
        self.assertEqual(sent(self.urlopen)[1], {"text": "Task done", "title": "Claude Code"})

    def test_bad_stdin_and_bridge_down_do_not_raise(self):
        self.urlopen.side_effect = OSError("refused")
        run(band_notify, "not json", ("hook", "input"))
        run(band_notify, "[1]", ("hook", "done"))
        self.assertEqual(self.urlopen.call_count, 2)


class PermissionTest(unittest.TestCase):
    EVENT = {"cwd": "C:/x/proj", "tool_name": "Bash", "tool_input": {"command": "npm test"}}

    def setUp(self):
        os.environ.pop("BAND_BRIDGE_ALWAYS", None)
        patches = [mock.patch("urllib.request.urlopen"), mock.patch("bridge.idle.away", return_value=True),
                   mock.patch("bridge.idle.idle_seconds", return_value=300.0),
                   mock.patch.object(band_permission, "POLL", 0.01)]
        self.urlopen, self.away, self.idle, _ = (p.start() for p in patches)
        for p in patches:
            self.addCleanup(p.stop)

    def answer(self, **reply):
        self.urlopen.return_value = Reply(json.dumps(reply).encode())
        out = run(band_permission, self.EVENT)
        return json.loads(out)["hookSpecificOutput"] if out else None

    def test_present_user_gets_normal_dialog(self):
        self.away.return_value = False
        self.assertEqual(run(band_permission, self.EVENT), "")
        self.urlopen.assert_not_called()

    def test_asks_the_band(self):
        self.answer(answer="Allow", option=1)
        url, body = sent(self.urlopen)
        self.assertEqual(url, "http://127.0.0.1:8765/ask")
        self.assertEqual(body, {"question": "proj: allow Bash? npm test", "options": ["Allow", "Deny"], "timeout": 240})

    def test_timeout_from_env(self):
        self.urlopen.return_value = Reply()
        run(band_permission, self.EVENT, env={"BAND_BRIDGE_PERMISSION_TIMEOUT": "30"})
        self.assertEqual(sent(self.urlopen)[1]["timeout"], 30)

    def test_allow(self):
        allow = {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}
        for reply in ({"answer": "Allow", "option": 1}, {"answer": "yes", "option": None}, {"answer": "x", "option": 1}):
            self.assertEqual(self.answer(**reply), allow)

    def test_deny(self):
        deny = {"hookEventName": "PermissionRequest", "decision": {"behavior": "deny", "message": "Denied on the band"}}
        for reply in ({"answer": "Deny", "option": 2}, {"answer": "no", "option": None},
                      {"answer": "stop", "option": None}, {"answer": "x", "option": 2}):
            self.assertEqual(self.answer(**reply), deny)

    def test_unclear_answer_falls_back_to_dialog(self):
        for reply in ({"answer": "continue", "option": None}, {"answer": "", "option": None}, {}, {"option": 3}):
            self.assertIsNone(self.answer(**reply))

    def test_errors_never_decide(self):
        http = urllib.error.HTTPError("u", 503, "band not connected", {}, None)
        for failure in (http, OSError("refused"), TimeoutError()):
            self.urlopen.side_effect = failure
            self.assertEqual(run(band_permission, self.EVENT), "")
        self.urlopen.side_effect = None
        self.urlopen.return_value = Reply(b"garbage")
        self.assertEqual(run(band_permission, self.EVENT), "")

    def test_user_coming_back_cancels_the_wait(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.urlopen.side_effect = lambda *a, **k: release.wait(10) and Reply(b'{"option": 1}')
        self.idle.return_value = 0.5
        start = time.monotonic()
        self.assertEqual(run(band_permission, self.EVENT), "")
        self.assertLess(time.monotonic() - start, 3)

    def test_always_keeps_waiting_while_user_is_active(self):
        self.away.return_value = False
        self.idle.return_value = 0.5
        self.urlopen.return_value = Reply(b'{"option": 1}')
        out = run(band_permission, self.EVENT, env={"BAND_BRIDGE_ALWAYS": "1"})
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["decision"]["behavior"], "allow")

    def test_summary(self):
        s = band_permission.summary
        self.assertEqual(s("Bash", {"command": "a\n  b   c"}), "a b c")
        self.assertEqual(s("Edit", {"file_path": "/a/b.py", "old_string": "x"}), "/a/b.py")
        self.assertEqual(s("WebFetch", {"url": "http://e.com", "prompt": "p"}), "http://e.com")
        self.assertEqual(s("Grep", {"pattern": "x"}), '{"pattern":"x"}')
        self.assertEqual(len(s("Bash", {"command": "x" * 999})), 150)
        self.assertEqual(s("Bash", None), "null")


class SmokeTest(unittest.TestCase):
    def test_scripts_exit_0_quickly_with_bridge_down(self):
        with __import__("socket").socket() as sock:  # a port nobody listens on
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {**os.environ, "BAND_BRIDGE_PORT": str(port), "BAND_BRIDGE_ALWAYS": "1"}
        event = json.dumps(PermissionTest.EVENT)
        for script, args in (("band_notify.py", ["done"]), ("band_notify.py", ["input"]), ("band_permission.py", [])):
            start = time.monotonic()
            p = subprocess.run([sys.executable, str(HOOKS / script), *args], input=event, text=True,
                               capture_output=True, env=env, cwd=os.environ.get("TEMP"), timeout=20)
            self.assertEqual((p.returncode, p.stdout), (0, ""), p.stderr)
            self.assertLess(time.monotonic() - start, 10)


if __name__ == "__main__":
    unittest.main()
