import asyncio
import re
import tempfile
import unittest
from pathlib import Path

from band.auth import AuthError
from band.supervisor import Supervisor

FAST = dict(heartbeat=0.005, connect_timeout=0.05, backoff=(0.001, 0.002, 0.004), close_timeout=0.5)


class FakeSession:
    """Session stand-in. hang/error make start() fail; revoked/beat_error make the heartbeat fail."""

    def __init__(self, hang=False, error=None, revoked=False, beat_error=None, token="AA", close_time=0):
        self.hang, self.error, self.revoked, self.beat_error = hang, error, revoked, beat_error
        self.close_time = close_time
        self.config = {"auth_token": token}
        self.disconnected = asyncio.Event()
        self.battery, self.last_error = 99, None
        self.beats = 0
        self.closing = self.closed = False

    async def start(self):
        if self.hang:
            await asyncio.Event().wait()
        if self.error:
            raise self.error

    async def read_battery(self):
        self.beats += 1
        if self.beat_error:
            raise self.beat_error
        if self.revoked:
            self.last_error = 0x186A4
        return self.battery

    async def close(self):
        self.closing = True
        await asyncio.sleep(self.close_time)
        self.closed = True


def factory(*plans):
    """Each connect builds FakeSession(**plan), in order; the last plan repeats."""
    plans, made = list(plans), []

    def make(**_):
        made.append(FakeSession(**(plans.pop(0) if len(plans) > 1 else plans[0])))
        return made[-1]

    make.made = made
    return make


class SupervisorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log_dir = tmp.name
        self.states = []

    def sup(self, *plans, **timings) -> Supervisor:
        self.make = factory(*plans)
        s = Supervisor("unused.json", self.log_dir, on_state=lambda state, detail: self.states.append((state, detail)),
                       session_factory=self.make, **{**FAST, **timings})
        self.task = asyncio.create_task(s.run())
        self.addAsyncCleanup(s.stop)
        return s

    async def until(self, cond):
        async with asyncio.timeout(2):
            while not cond():
                await asyncio.sleep(0.001)

    def delays(self):
        return [float(re.search(r"retry in ([\d.]+)s", d).group(1)) for state, d in self.states if state == "backoff"]

    async def test_ready_and_heartbeat(self):
        s = self.sup({})
        await self.until(s.ready.is_set)
        first = self.make.made[0]
        self.assertEqual((s.state, s.stats["connects"], s.stats["disconnects"]), ("ready", 1, 0))
        self.assertIs(s.session, first)
        await self.until(lambda: first.beats >= 3)
        self.assertEqual(len(self.make.made), 1)
        self.assertGreater(s.stats["ready_seconds"], 0)  # counts the current stretch too

    async def test_disconnect_reconnects(self):
        s = self.sup({}, {})
        await self.until(s.ready.is_set)
        first = self.make.made[0]
        first.disconnected.set()
        await self.until(lambda: s.stats["connects"] == 2)
        self.assertTrue(first.closed)
        self.assertEqual([state for state, _ in self.states[:5]],
                         ["connecting", "ready", "backoff", "connecting", "ready"])
        self.assertEqual(s.stats["disconnects"], 1)
        self.assertIn("band disconnected", s.stats["last_error"])

    async def test_backoff_follows_the_sequence_and_caps(self):
        self.sup({"error": ConnectionError("down")})
        await self.until(lambda: len(self.delays()) >= 6)
        self.assertEqual(self.delays()[:6], [0.001, 0.002, 0.004, 0.004, 0.004, 0.004])

    async def test_backoff_resets_after_a_successful_connect(self):
        s = self.sup({"error": ConnectionError("a")}, {"error": OSError("b")}, {}, {})
        await self.until(s.ready.is_set)
        self.make.made[2].disconnected.set()
        await self.until(lambda: len(self.delays()) == 3)
        self.assertEqual(self.delays(), [0.001, 0.002, 0.001])

    async def test_hung_start_is_aborted_and_retried(self):
        s = self.sup({"hang": True}, {})
        await self.until(s.ready.is_set)
        self.assertTrue(self.make.made[0].closed)
        self.assertIn("TimeoutError", s.stats["last_error"])
        self.assertEqual(s.stats["connects"], 1)

    async def test_battery_error_code_means_revoked(self):
        s = self.sup({"revoked": True}, {})
        await self.until(lambda: s.stats["connects"] == 2)
        self.assertTrue(self.make.made[0].closed)
        self.assertIn("7F=186A4", s.stats["last_error"])

    async def test_heartbeat_timeout_reconnects(self):
        s = self.sup({"beat_error": TimeoutError("no reply to 01/08")}, {})
        await self.until(lambda: s.stats["connects"] == 2)
        self.assertTrue(self.make.made[0].closed)

    async def test_rejected_stored_key_needs_pairing_and_is_not_retried(self):
        s = self.sup({"error": AuthError("band rejected the stored key: remove auth_token from config/band.json")})
        await asyncio.wait_for(self.task, 1)
        self.assertEqual(s.state, "needs_pairing")
        self.assertEqual(len(self.make.made), 1)
        self.assertTrue(self.make.made[0].closed)

    async def test_failed_first_pairing_needs_pairing(self):
        s = self.sup({"error": AuthError("HiChain op 1 step 1: band returned errorCode 5"), "token": None})
        await asyncio.wait_for(self.task, 1)
        self.assertEqual(s.state, "needs_pairing")

    async def test_other_auth_errors_are_retried(self):
        s = self.sup({"error": AuthError("unsupported auth type 13")}, {})
        await self.until(s.ready.is_set)
        self.assertEqual(len(self.make.made), 2)

    async def test_on_ready_runs_before_ready_and_a_failure_reconnects(self):
        calls = []

        async def on_ready(session):
            calls.append((session, s.ready.is_set()))
            if len(calls) == 1:
                raise RuntimeError("setup failed")

        s = self.sup({}, {}, on_ready=on_ready)
        await self.until(s.ready.is_set)
        first, second = self.make.made
        self.assertEqual(calls, [(first, False), (second, False)])  # "ready" is only set once the setup passed
        self.assertTrue(first.closed)
        self.assertEqual(s.stats["connects"], 1)
        self.assertIn("setup failed", s.stats["last_error"])
        await self.until(lambda: second.beats >= 1)
        self.assertEqual(len(calls), 2)  # not repeated by the heartbeat

    async def test_stop_while_ready(self):
        s = self.sup({})
        await self.until(s.ready.is_set)
        await s.stop()
        self.assertEqual(s.state, "stopped")
        self.assertTrue(self.task.done() and self.make.made[0].closed)
        self.assertFalse(s.ready.is_set())
        self.assertIsNone(s.session)
        self.assertEqual(s.stats["disconnects"], 0)  # a clean stop is not a drop
        log = Path(s.log_path).read_text()
        for line in ("EVT state connecting", "EVT state ready battery 99%", "EVT state stopped"):
            self.assertIn(line, log)

    async def test_stop_while_connecting(self):
        s = self.sup({"hang": True}, connect_timeout=10)
        await self.until(lambda: self.make.made)
        await s.stop()
        self.assertEqual(s.state, "stopped")
        self.assertTrue(self.make.made[0].closed)

    async def test_stop_during_a_close_lets_it_finish(self):
        s = self.sup({"close_time": 0.05}, {})
        await self.until(s.ready.is_set)
        first = self.make.made[0]
        first.disconnected.set()
        await self.until(lambda: first.closing)
        await s.stop()
        self.assertTrue(first.closed)
        self.assertEqual(len(self.make.made), 1)


if __name__ == "__main__":
    unittest.main()
