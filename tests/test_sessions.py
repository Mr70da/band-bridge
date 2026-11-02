import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bridge import sessions
from bridge.sessions import Board

HOOK = str(Path(__file__).resolve().parent.parent / "hooks" / "band_session.py")


class FakeNotifier:
    def __init__(self):
        self.calls = []

    async def send(self, session, title, text, reply=False):
        self.calls.append(("send", title, text, reply))
        return len([c for c in self.calls if c[0] == "send"])

    async def remove(self, session, nid):
        self.calls.append(("remove", nid))


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def make(session=object()):
    n, c = FakeNotifier(), Clock()
    return Board(lambda: session, n, clock=c), n, c


class BoardTest(unittest.IsolatedAsyncioTestCase):
    async def test_state_machine(self):
        b, _, _ = make()
        for ev, st in [("SessionStart", "idle"), ("UserPromptSubmit", "working"), ("Notification", "waiting"), ("Stop", "done")]:
            b.event("s1", "api", ev)
            self.assertEqual(b.sessions["s1"]["state"], st)
        b.event("s1", "api", "SessionEnd")
        self.assertEqual(b.sessions, {})
        b.event("s1", "api", "Bogus")
        self.assertEqual(b.sessions, {})
        b.task.cancel()

    async def test_order_format_and_suffix(self):
        b, _, c = make()
        for sid, p, ev in [("aaaa1", "web", "UserPromptSubmit"), ("bbbb2", "api", "Stop"), ("cccc3", "band-bridge", "Notification"),
                           ("dddd4", "x", "UserPromptSubmit"), ("eeee5", "x", "SessionStart")]:
            b.event(sid, p, ev)
        b.task.cancel()
        c.t += 125
        self.assertEqual(b.body(b.listing()), "⏳ band-bridge: waiting\n✅ api: done 2m\n⚙ web: working 2m\n"
                         "⚙ x-dddd: working 2m\n💤 x-eeee: idle")

    async def test_stale_dropped(self):
        b, _, c = make()
        b.event("s", "p", "Stop")
        b.task.cancel()
        c.t += 6 * 3600 + 1
        self.assertEqual(b.listing(), [])

    async def test_remove_then_send_and_policy(self):
        b, n, c = make()
        b.sessions["s"] = {"project": "p", "state": "working", "since": c.t}
        self.assertEqual(await b.update(), 0)  # new session: sent
        self.assertEqual(n.calls, [("send", "Claude sessions", "⚙ p: working 0m", False)])
        self.assertEqual(await b.update(), 0)  # nothing changed
        self.assertEqual(len(n.calls), 1)
        b.sessions["t"] = {"project": "q", "state": "waiting", "since": c.t}
        await b.update()  # waiting: removes the old one first, then sends
        self.assertEqual([x[0] for x in n.calls], ["send", "remove", "send"])
        self.assertEqual(n.calls[1], ("remove", 1))
        # working-only change inside the minute is held back, then allowed
        b.sessions["s"]["state"] = "idle"
        b.sessions["s"]["state"] = "working"
        b.sessions["t"]["state"] = "waiting"
        b.sent["s"] = "idle"
        c.t += 10
        self.assertEqual(await b.update(), 50)
        self.assertEqual(len(n.calls), 3)
        c.t += 50
        self.assertEqual(await b.update(), 0)
        self.assertEqual(len(n.calls), 5)
        # all sessions end: remove, nothing sent
        b.sessions.clear()
        await b.update()
        self.assertEqual(n.calls[-1], ("remove", 3))
        self.assertEqual(len(n.calls), 6)

    async def test_no_band(self):
        b, n, _ = make(session=None)
        b.event("s", "p", "Stop")
        self.assertEqual(await b.update(), 0)
        self.assertEqual(n.calls, [])
        self.assertEqual(b.sessions["s"]["state"], "done")
        b.task.cancel()

    async def test_debounce_burst_one_update(self):
        n, c, slept = FakeNotifier(), Clock(), []

        async def sleep(d):
            slept.append(d)
        b = Board(lambda: object(), n, clock=c, sleep=sleep)
        for ev in ["SessionStart", "UserPromptSubmit", "Notification"]:
            b.event("s", "p", ev)
        await b.task
        self.assertEqual(slept, [3])
        self.assertEqual(len(n.calls), 1)
        self.assertIn("waiting", n.calls[0][2])


class RouterTest(unittest.TestCase):
    def test_endpoints(self):
        n = FakeNotifier()
        r = sessions.router(lambda: None, n)
        app = FastAPI()
        app.include_router(r)
        with TestClient(app) as cl:
            self.assertEqual(cl.post("/sessions/event", json={"session_id": "s1", "project": "api", "event": "Stop"}).json(), {"ok": True})
            got = cl.get("/sessions").json()
            self.assertEqual((got[0]["project"], got[0]["state"]), ("api", "done"))


class HookTest(unittest.TestCase):
    def run_hook(self, event, port):
        return subprocess.run([sys.executable, HOOK], input=json.dumps(event), text=True, capture_output=True,
                              env={**os.environ, "BAND_BRIDGE_PORT": str(port)}, timeout=20)

    def test_posts_json(self):
        got = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        p = self.run_hook({"session_id": "s1", "cwd": "C:/x/band-bridge", "hook_event_name": "Notification", "message": "hi"}, srv.server_port)
        srv.shutdown()
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""))
        self.assertEqual(got, [{"session_id": "s1", "project": "band-bridge", "event": "Notification", "message": "hi"}])

    def test_closed_port_and_bad_stdin(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        p = self.run_hook({"session_id": "s1", "hook_event_name": "Stop"}, port)
        self.assertEqual((p.returncode, p.stdout), (0, ""))
        p = subprocess.run([sys.executable, HOOK], input="garbage", text=True, capture_output=True)
        self.assertEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()


class AwayTest(unittest.IsolatedAsyncioTestCase):
    async def test_present_user_gets_no_board_until_away(self):
        sent, here = [], [True]

        class N:
            async def send(self, session, title, text, reply=False):
                sent.append(text)
                return 1

            async def remove(self, session, nid):
                pass
        board = sessions.Board(lambda: object(), N(), away=lambda: not here[0])
        board.event("s1", "p", "Stop")
        self.assertEqual(await board.update(), 0)
        self.assertEqual(sent, [])
        here[0] = False
        await board.update()
        self.assertEqual(len(sent), 1)

