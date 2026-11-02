import asyncio
import os
import time
from datetime import datetime

from band.auth import AuthError
from band.session import Session


class Supervisor:
    """Keeps one ready Session alive: connect, heartbeat, and on any failure close it and reconnect with backoff."""

    def __init__(self, config_path="config/band.json", log_dir="logs", on_state=None, on_prompt=print,
                 on_message=None, heartbeat=60, connect_timeout=60, backoff=(2, 4, 8, 16, 30, 60),
                 session_factory=Session, close_timeout=10):
        self.config_path, self.log_dir = config_path, log_dir
        self.on_state, self.on_prompt, self.on_message = on_state, on_prompt, on_message
        self.heartbeat, self.connect_timeout, self.backoff = heartbeat, connect_timeout, backoff
        self.session_factory, self.close_timeout = session_factory, close_timeout
        self.state = "stopped"
        self.session = None  # the ready Session, else None
        self.ready = asyncio.Event()
        self.log_path = os.path.join(log_dir, datetime.now().strftime("ble-%Y%m%d-%H%M%S.log"))  # one per run
        self._stats = dict(connects=0, disconnects=0, last_ready_at=None, ready_seconds=0.0, started_at=None,
                           last_error=None)
        self._ready_since = None  # monotonic
        self._log = None
        self._task = None
        self._stopping = False

    @property
    def stats(self) -> dict:
        """A copy; ready_seconds includes the current ready stretch."""
        live = time.monotonic() - self._ready_since if self._ready_since is not None else 0
        return {**self._stats, "ready_seconds": self._stats["ready_seconds"] + live}

    def _write(self, kind, text):
        if self._log and not self._log.closed:
            self._log.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} {kind} {text}\n")
            self._log.flush()

    def _set(self, state, detail=""):
        self.state = state
        self._write("EVT", f"state {state} {detail}".rstrip())
        if self.on_state:
            try:
                self.on_state(state, detail)
            except Exception as e:  # a broken callback must not end an unattended run
                self._write("EVT", f"on_state error {e!r}")

    async def run(self):
        """Loops until stop(), or returns when only the user at the band can help (needs_pairing)."""
        self._task = asyncio.current_task()
        os.makedirs(self.log_dir, exist_ok=True)
        self._log = open(self.log_path, "a", encoding="utf-8")
        self._stats["started_at"] = time.time()
        failures = 0
        # After a FIRST pairing the band drops the link ~30 s later (SMP timeout, protocol.md section 1).
        # No special case: that is just a drop, and the reconnect (op 2 only) stays up.
        try:
            while True:
                session, err = None, None
                self._set("connecting")
                try:
                    session = self.session_factory(config_path=self.config_path, log_dir=self.log_dir,
                                                   on_prompt=self.on_prompt, on_message=self.on_message,
                                                   log_path=self.log_path)
                    # a hung connect or auth is aborted here and retried
                    await asyncio.wait_for(session.start(), self.connect_timeout)
                    failures = 0
                    self._ready(session)
                    await self._watch(session)
                except AuthError as e:
                    # never re-pair on our own: it needs the user at the band
                    if not session.config.get("auth_token") or "rejected the stored key" in str(e):
                        self._stats["last_error"] = repr(e)
                        self._set("needs_pairing", str(e))
                        return
                    err = e
                except Exception as e:  # BleakError, OSError, TimeoutError, ConnectionError, ...
                    err = e
                finally:
                    self._unready()
                    await self._close(session)
                delay = self.backoff[min(failures, len(self.backoff) - 1)]
                failures += 1
                self._stats["last_error"] = repr(err)
                self._set("backoff", f"{err!r}; retry in {delay:g}s")
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self._set("stopped")
            if not self._stopping:
                raise
        finally:
            self._log.close()

    def _ready(self, session):
        self.session = session
        self._ready_since = time.monotonic()
        self._stats["connects"] += 1
        self._stats["last_ready_at"] = time.time()
        self.ready.set()
        self._set("ready", f"battery {session.battery}%")

    def _unready(self):
        if self._ready_since is not None:
            self._stats["ready_seconds"] += time.monotonic() - self._ready_since
            self._ready_since = None
            if not self._stopping:
                self._stats["disconnects"] += 1
        self.session = None
        self.ready.clear()

    async def _watch(self, session):
        """Heartbeat until the session is dead. Only ever ends by raising."""
        dead = asyncio.create_task(session.disconnected.wait())  # a drop is noticed at once, not at the next beat
        try:
            while True:
                done, _ = await asyncio.wait({dead}, timeout=self.heartbeat)
                if done:
                    raise ConnectionError("band disconnected")
                await session.read_battery()  # raises when the link is dead
                if session.last_error is not None:  # an error code instead of a battery level: protocol.md 0x186A4
                    raise ConnectionError(f"band revoked the session, 7F={session.last_error:X}")
        finally:
            dead.cancel()

    async def _close(self, session):
        """Never leaves a client open: a cancel (stop()) during the close lets the close finish first."""
        if session is None:
            return
        job = asyncio.ensure_future(asyncio.wait_for(session.close(), self.close_timeout))
        try:
            await asyncio.shield(job)
        except asyncio.CancelledError:
            await asyncio.gather(job, return_exceptions=True)
            raise
        except Exception as e:
            self._write("EVT", f"close error {e!r}")

    async def stop(self):
        """Clean shutdown: run() closes the session, sets state stopped and returns."""
        if self._task and not self._task.done():
            self._stopping = True
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
