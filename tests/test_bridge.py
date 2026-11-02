import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from bridge import api, mcp_server

ROOT = Path(__file__).resolve().parent.parent


class FakeSession:
    battery, model = 77, "Band 11"
    config = {"firmware": "1.2.3"}


class FakeSupervisor:
    def __init__(self, ready=True):
        self.ready, self.state = asyncio.Event(), "ready" if ready else "backoff"
        self.session = FakeSession() if ready else None
        self.stats = {"connects": 1}
        if ready:
            self.ready.set()


class FakeNotifier:
    """Records sends and quick-reply pushes (None = the default list; `order` tells which came first).
    reply_for(body) -> (delay, text) or None makes the 'band' answer after a delay."""

    def __init__(self, reply_for=lambda body: None, push_error=None):
        self.reply_for, self.on_reply, self.push_error = reply_for, None, push_error
        self.sent, self.removed, self.pushes, self.order = [], [], [], []

    async def set_replies(self, session, replies=None):
        self.order.append("push")
        self.pushes.append(replies)
        if self.push_error:
            raise self.push_error
        return 0xCF

    async def send(self, session, title, text, reply=True):
        nid = 100 + len(self.sent)
        self.order.append("send")
        self.sent.append(dict(id=nid, title=title, text=text, reply=reply))
        if (answer := self.reply_for(text)) is not None:
            asyncio.get_running_loop().call_later(answer[0], self.on_reply, nid, answer[1], f"key{nid}")
        return nid

    async def remove(self, session, notification_id):
        self.removed.append(notification_id)
        return True


def client(reply_for=lambda body: None, ready=True, push_error=None):
    notifier = FakeNotifier(reply_for, push_error)
    return TestClient(api.create_app(FakeSupervisor(ready), notifier)), notifier


class FormatTest(unittest.TestCase):
    def test_numbers_options(self):
        title, body = api.format_question("Deploy?", ["now", "later"])
        self.assertEqual((title, body), ("Claude asks", "Deploy?\n1) now\n2) later"))

    def test_no_options_is_just_the_question(self):
        self.assertEqual(api.format_question("Continue?", [])[1], "Continue?")

    def test_long_question_is_trimmed_not_the_options(self):
        title, body = api.format_question("q" * 2000, ["a" * 300, "b", "c"])
        self.assertLessEqual(len(body), api.BODY_LIMIT)
        lines = body.split("\n")
        self.assertTrue(lines[0].endswith("…"))
        self.assertEqual([l[:3] for l in lines[1:]], ["1) ", "2) ", "3) "])
        self.assertEqual(lines[2:], ["2) b", "3) c"])

    def test_map_reply(self):
        self.assertEqual(api.map_reply("2", ["x", "y"]), ("y", 2))
        self.assertEqual(api.map_reply("3", ["x", "y"]), ("3", None))  # no such option
        self.assertEqual(api.map_reply(" Yes ", []), ("yes", None))

    def test_map_reply_by_option_text(self):
        long = "Deploy to production now, please " * 2
        options = ["Red", long, "yes"]
        self.assertEqual(api.map_reply(" RED ", options), ("Red", 1))
        self.assertEqual(api.map_reply(api.shorten(long, api.REPLY_LIMIT), options), (long, 2))  # the clipped form
        self.assertEqual(api.map_reply(long.upper(), options), (long, 2))
        self.assertEqual(api.map_reply("yes", options), ("yes", 3))
        self.assertEqual(api.map_reply("stop", options), ("stop", None))
        self.assertEqual(api.map_reply("4", options), ("4", None))
        self.assertEqual(api.map_reply("5", list("abcde")), ("e", 5))

    def test_quick_replies_are_options_then_the_fixed_ones(self):
        self.assertEqual(api.quick_replies(["red", "blue"]), ["red", "blue", "yes", "no", "continue", "stop"])

    def test_quick_replies_clip_dedupe_and_cap(self):
        clipped = api.quick_replies(["x" * 100])[0]
        self.assertEqual((len(clipped), clipped[-1]), (api.REPLY_LIMIT, "…"))
        self.assertEqual(api.quick_replies(["Yes", "NO", "a", "A", " "]), ["Yes", "NO", "a", "continue", "stop"])
        with mock.patch.object(api, "REPLY_COUNT", 5):
            self.assertEqual(api.quick_replies(["a", "b", "c"]), ["a", "b", "c", "yes", "no"])
        self.assertEqual(len(api.quick_replies(list("abcde"))), 9)
        self.assertLessEqual(api.MAX_OPTIONS + 2, api.REPLY_COUNT)  # yes/no always fit


class ApiTest(unittest.TestCase):
    def test_too_many_options_is_422(self):
        c, notifier = client()
        with c:
            r = c.post("/ask", json={"question": "q", "options": list("abcdef")})
        self.assertEqual(r.status_code, 422)
        self.assertIn("at most 5", r.json()["error"])
        self.assertEqual((notifier.sent, notifier.pushes), ([], []))

    def test_option_answer(self):
        c, notifier = client(lambda body: (0.01, "2"))
        with c:
            r = c.post("/ask", json={"question": "Which?", "options": ["red", "blue"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"answer": "blue", "reply": "2", "option": 2, "notification_id": 100})
        self.assertEqual(notifier.sent[0]["text"], "Which?\n1) red\n2) blue")
        self.assertTrue(notifier.sent[0]["reply"])
        self.assertEqual(notifier.removed, [100])

    def test_options_are_pushed_before_the_ask_and_restored_after(self):
        c, notifier = client(lambda body: (0.01, "blue"))
        with c:
            r = c.post("/ask", json={"question": "Which?", "options": ["red", "blue"]})
        self.assertEqual((r.json()["answer"], r.json()["option"]), ("blue", 2))
        self.assertEqual(notifier.pushes, [["red", "blue", "yes", "no", "continue", "stop"], None])
        self.assertEqual(notifier.order, ["push", "send", "push"])

    def test_timeout_restores_the_default_list(self):
        c, notifier = client()
        with c:
            r = c.post("/ask", json={"question": "Which?", "options": ["red"], "timeout": 0.05})
        self.assertEqual(r.status_code, 504)
        self.assertEqual(notifier.pushes, [["red", "yes", "no", "continue", "stop"], None])

    def test_failed_push_falls_back_to_numbers(self):
        c, notifier = client(lambda body: (0.01, "2"), push_error=TimeoutError("no ack"))
        with c, self.assertLogs("bridge", "WARNING") as logs:
            r = c.post("/ask", json={"question": "Which?", "options": ["red", "blue"]})
        self.assertEqual((r.status_code, r.json()["answer"], r.json()["option"]), (200, "blue", 2))
        self.assertEqual(notifier.sent[0]["text"], "Which?\n1) red\n2) blue")
        self.assertTrue(any("quick replies" in line and "no ack" in line for line in logs.output))

    def test_overlapping_asks_keep_the_latest_list_and_restore_once(self):
        def reply_for(body):
            return (0.4, "x") if body.startswith("first") else (0.05, "2")

        c, notifier = client(reply_for)
        with c, ThreadPoolExecutor(2) as pool:
            a = pool.submit(c.post, "/ask", json={"question": "first", "options": ["x"]})
            while not notifier.sent:
                threading.Event().wait(0.01)
            b = pool.submit(c.post, "/ask", json={"question": "second", "options": ["y", "z"]})
            a, b = a.result().json(), b.result().json()
        self.assertEqual((a["answer"], a["option"]), ("x", 1))  # each ask maps by its own options
        self.assertEqual((b["answer"], b["option"]), ("z", 2))
        self.assertEqual(notifier.pushes, [["x", "yes", "no", "continue", "stop"],
                                           ["y", "z", "yes", "no", "continue", "stop"], None])

    def test_yes_without_options(self):
        c, notifier = client(lambda body: (0.01, "yes"))
        with c:
            r = c.post("/ask", json={"question": "Proceed?"})
        self.assertEqual((r.json()["answer"], r.json()["option"]), ("yes", None))
        self.assertEqual(notifier.removed, [100])
        self.assertEqual(notifier.pushes, [])  # no options: the default list stays

    def test_timeout_is_504_and_removes(self):
        c, notifier = client()
        with c:
            r = c.post("/ask", json={"question": "Hello?", "timeout": 0.05})
            pending = c.get("/status").json()["pending_asks"]
        self.assertEqual(r.status_code, 504)
        self.assertIn("no answer within", r.json()["error"])
        self.assertEqual((notifier.removed, pending), ([100], 0))

    def test_not_ready_is_503(self):
        c, notifier = client(ready=False)
        with mock.patch.object(api, "ASK_WAIT", 0.01), mock.patch.object(api, "NOTIFY_WAIT", 0.01), c:
            for path, body in (("/ask", {"question": "q"}), ("/notify", {"text": "t"})):
                r = c.post(path, json=body)
                self.assertEqual(r.status_code, 503)
                self.assertEqual(r.json(), {"error": "band not connected", "state": "backoff"})
        self.assertEqual(notifier.sent, [])

    def test_notify_is_one_way(self):
        c, notifier = client()
        with c:
            r = c.post("/notify", json={"text": "Task done"})
        self.assertEqual(r.json(), {"notification_id": 100})
        self.assertEqual(notifier.sent, [dict(id=100, title="Claude Code", text="Task done", reply=False)])

    def test_status(self):
        c, _ = client()
        with c:
            self.assertEqual(c.get("/status").json(), {
                "state": "ready", "battery": 77, "model": "Band 11", "firmware": "1.2.3",
                "stats": {"connects": 1}, "pending_asks": 0})

    def test_concurrent_asks_resolve_independently(self):
        def reply_for(body):
            return (0.3, "1") if body.startswith("first") else (0.05, "no")

        c, notifier = client(reply_for)
        with c, ThreadPoolExecutor(2) as pool:
            a = pool.submit(c.post, "/ask", json={"question": "first", "options": ["x"]})
            b = pool.submit(c.post, "/ask", json={"question": "second"})
            a, b = a.result().json(), b.result().json()
        self.assertEqual((a["answer"], a["option"]), ("x", 1))
        self.assertEqual((b["answer"], b["option"]), ("no", None))
        self.assertNotEqual(a["notification_id"], b["notification_id"])
        self.assertCountEqual(notifier.removed, [100, 101])

    def test_unknown_reply_is_ignored(self):
        c, notifier = client()
        with c:
            notifier.on_reply(999, "yes", "k")  # must not raise


class WiringTest(unittest.TestCase):
    def test_service_run_lives_as_long_as_the_app(self):
        class Service:
            started = cancelled = False

            async def run(self):
                Service.started = True
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    Service.cancelled = True
                    raise
        with TestClient(api.create_app(FakeSupervisor(), FakeNotifier(), services=[Service()])):
            pass
        self.assertTrue(Service.started and Service.cancelled)

    def test_fan_out_survives_a_failing_service(self):
        seen = []

        class Broken:
            async def setup(self, session):
                raise OSError("x")

            def on_message(self, msg):
                raise ValueError("y")

        class Good:
            async def setup(self, session):
                seen.append("setup")

            def on_message(self, msg):
                seen.append(msg)

        class Notifier:
            async def setup(self, session):
                seen.append("notifier")

        on_ready, on_message = api.fan_out(Notifier(), [Broken(), Good()])
        asyncio.run(on_ready(None))
        on_message("m")
        self.assertEqual(seen, ["notifier", "setup", "m"])


class FakeHttp:
    """Answers every POST with status/body; remembers the requests."""

    def __init__(self, status=200, body=None, delay=0):
        self.requests = requests = []
        status_, body_, delay_ = status, body or {}, delay

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                threading.Event().wait(delay_)
                data = json.dumps(body_).encode()
                self.send_response(status_)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.server.handle_error = lambda *args: None  # a client that gave up is expected
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def on_port(port):
    return mock.patch.dict(os.environ, {"BAND_BRIDGE_PORT": str(port)})


class McpServerTest(unittest.TestCase):
    def ask(self, **kw):
        return asyncio.run(mcp_server.ask_on_band("Pick?", ["a", "b"], **kw))

    def test_answer(self):
        body = {"answer": "b", "reply": "2", "option": 2, "notification_id": 5}
        with FakeHttp(200, body) as http, on_port(http.port):
            self.assertEqual(self.ask(), 'User answered on the band: "b" (option 2)')
        self.assertEqual(http.requests, [("/ask", {"question": "Pick?", "options": ["a", "b"], "timeout": 300})])

    def test_answer_without_option(self):
        with FakeHttp(200, {"answer": "yes", "reply": "yes", "option": None}) as http, on_port(http.port):
            self.assertEqual(self.ask(), 'User answered on the band: "yes"')

    def test_bridge_not_running(self):
        with on_port(closed_port()):
            self.assertIn("not running", self.ask())
            self.assertIn("not running", asyncio.run(mcp_server.notify_band("hi")))

    def test_not_connected(self):
        with FakeHttp(503, {"error": "band not connected", "state": "backoff"}) as http, on_port(http.port):
            self.assertEqual(self.ask(), "Band bridge error (503): band not connected")

    def test_timed_out(self):
        with FakeHttp(504, {"error": "no answer within 5 s"}) as http, on_port(http.port):
            self.assertEqual(self.ask(timeout_seconds=5), "Band bridge error (504): no answer within 5 s")

    def test_bridge_hangs(self):
        with FakeHttp(200, {}, delay=1) as http, on_port(http.port), \
                mock.patch.object(mcp_server, "_post", wraps=mcp_server._post) as post:
            # the client gives up after timeout_seconds + 15; shrink that for the test
            self.assertIn("did not answer", asyncio.run(mcp_server.call("/ask", {}, 0.1)))
            post.assert_called_once()

    def test_notify(self):
        with FakeHttp(200, {"notification_id": 1}) as http, on_port(http.port):
            self.assertEqual(asyncio.run(mcp_server.notify_band("hi")), "Sent to the band.")
        self.assertEqual(http.requests, [("/notify", {"text": "hi"})])


class HookTest(unittest.TestCase):
    def run_hook(self, kind, stdin, port):
        env = {**os.environ, "BAND_BRIDGE_PORT": str(port), "BAND_BRIDGE_ALWAYS": "1"}  # send even when at the PC
        return subprocess.run([sys.executable, str(ROOT / "hooks" / "band_notify.py"), kind], input=stdin,
                              text=True, env=env, timeout=10, capture_output=True)

    def test_exits_0_when_nothing_listens(self):
        for kind, stdin in (("input", '{"message": "x"}'), ("done", ""), ("input", "not json")):
            self.assertEqual(self.run_hook(kind, stdin, closed_port()).returncode, 0)

    def test_posts_text(self):
        with FakeHttp(200, {"notification_id": 1}) as http:
            self.assertEqual(self.run_hook("input", json.dumps({"message": "Allow Bash?"}), http.port).returncode, 0)
            self.assertEqual(self.run_hook("done", "{}", http.port).returncode, 0)
        self.assertEqual([r[1]["text"] for r in http.requests],
                         ["Claude needs your input: Allow Bash?", "Task done"])


if __name__ == "__main__":
    unittest.main()
