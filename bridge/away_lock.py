"""Lock the PC when the user walks away with the band: the band stays disconnected and there is no keyboard/mouse input."""
import asyncio
import ctypes
import logging
import time

from bridge.idle import idle_seconds

log = logging.getLogger("bridge")
CHECK_EVERY = 5.0
LEFT = ("connecting", "backoff")  # needs_pairing/stopped are not the user leaving


def lock_workstation():
    ctypes.windll.user32.LockWorkStation()


class AwayLock:
    def __init__(self, get_state, config=None, lock=lock_workstation, idle=idle_seconds, clock=time.monotonic):
        self.get_state, self.lock, self.idle, self.clock = get_state, lock, idle, clock
        cfg = config or {}
        self.enabled = cfg.get("enabled", True)
        self.after = float(cfg.get("after", 60))
        self._armed = self._locked = False  # armed once the band has been ready
        self._lost: float | None = None  # when the band was first seen gone

    def check(self):
        state = self.get_state()
        if state == "ready":
            self._armed, self._locked, self._lost = True, False, None
            return
        if not self._armed or state not in LEFT:
            self._lost = None
            return
        now = self.clock()
        if self._lost is None:
            self._lost = now
        # idle too: a BLE glitch while the user types must not lock them out
        if not self._locked and now - self._lost >= self.after and self.idle() >= self.after:
            self._locked = True
            log.info("band gone and no input for %ss: locking the PC", int(self.after))
            self.lock()

    async def run(self):
        if not self.enabled:
            return
        while True:
            try:
                self.check()
            except Exception:
                log.exception("away lock check failed")
            await asyncio.sleep(CHECK_EVERY)
