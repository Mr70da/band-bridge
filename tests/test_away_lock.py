import asyncio
import unittest
from unittest import mock

from bridge import away_lock
from bridge.away_lock import AwayLock


class Rig:
    def __init__(self, config=None):
        self.state, self.now, self.idle_for, self.locks = "connecting", 0.0, 1000.0, 0
        self.al = AwayLock(lambda: self.state, config, lock=self._lock, idle=lambda: self.idle_for, clock=lambda: self.now)

    def _lock(self):
        self.locks += 1

    def at(self, state, t):
        self.state, self.now = state, t
        self.al.check()


class AwayLockTest(unittest.TestCase):
    def test_never_ready_never_locks(self):
        r = Rig()
        r.at("connecting", 0)
        r.at("backoff", 500)
        self.assertEqual(r.locks, 0)

    def test_locks_after_threshold(self):
        r = Rig()
        r.at("ready", 0)
        r.at("backoff", 10)
        r.at("connecting", 69)
        self.assertEqual(r.locks, 0)
        r.at("backoff", 70)
        self.assertEqual(r.locks, 1)

    def test_active_user_not_locked_until_idle(self):
        r = Rig()
        r.at("ready", 0)
        r.idle_for = 5
        r.at("backoff", 10)
        r.at("backoff", 200)
        self.assertEqual(r.locks, 0)
        r.idle_for = 60
        r.at("backoff", 205)
        self.assertEqual(r.locks, 1)

    def test_once_per_absence_and_rearms(self):
        r = Rig()
        r.at("ready", 0)
        r.at("backoff", 1)
        r.at("backoff", 100)
        r.at("backoff", 200)
        self.assertEqual(r.locks, 1)
        r.at("ready", 210)
        r.at("backoff", 220)
        r.at("backoff", 300)
        self.assertEqual(r.locks, 2)

    def test_needs_pairing_and_stopped_do_not_lock(self):
        for s in ("needs_pairing", "stopped"):
            r = Rig()
            r.at("ready", 0)
            r.at(s, 1)
            r.at(s, 500)
            self.assertEqual(r.locks, 0, s)

    def test_custom_after(self):
        r = Rig({"after": 10})
        r.at("ready", 0)
        r.at("backoff", 1)
        r.at("backoff", 11)
        self.assertEqual(r.locks, 1)

    def test_disabled_returns_at_once(self):
        r = Rig({"enabled": False})
        asyncio.run(asyncio.wait_for(r.al.run(), 1))

    def test_failing_check_does_not_stop_run(self):
        calls = []

        def state():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            if len(calls) >= 3:
                raise asyncio.CancelledError
            return "ready"

        al = AwayLock(state, lock=lambda: None)

        async def go():
            with mock.patch.object(away_lock, "CHECK_EVERY", 0):
                await al.run()

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(go())
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
