"""Session board: one band notification summarizing all running Claude Code sessions.

Wire it with `app.include_router(sessions.router(lambda: sup.session, notifier))`.
Hooks POST /sessions/event (hooks/band_session.py); GET /sessions lists the state.
"""
import asyncio
import time

from fastapi import APIRouter
from pydantic import BaseModel

STATES = {"SessionStart": "idle", "UserPromptSubmit": "working", "Notification": "waiting", "Stop": "done"}
ICON = {"waiting": "⏳", "done": "✅", "working": "⚙", "idle": "💤"}
ORDER = ["waiting", "done", "working", "idle"]  # most urgent first
TITLE, STALE, DEBOUNCE, WORKING_EVERY, BODY_LIMIT = "Claude sessions", 6 * 3600, 3, 60, 480


class Event(BaseModel):
    session_id: str
    project: str = ""
    event: str
    message: str = ""


def age(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m}m" if m < 60 else f"{m // 60}h"


class Board:
    def __init__(self, get_session, notifier, clock=time.time, sleep=asyncio.sleep, away=lambda: True):
        self.get_session, self.notifier, self.clock, self.sleep = get_session, notifier, clock, sleep
        self.away = away  # the app passes bridge.idle.away: no board buzzes while the user is at the PC
        self.sessions: dict[str, dict] = {}
        self.sent: dict[str, str] = {}  # id -> state as shown on the band
        self.nid = None
        self.last_send = 0.0
        self.task = None

    def event(self, sid: str, project: str, name: str):
        now = self.clock()
        if name == "SessionEnd":
            self.sessions.pop(sid, None)
        elif name in STATES:
            self.sessions[sid] = {"project": project or "claude", "state": STATES[name], "since": now}
        else:
            return
        self.schedule(DEBOUNCE)

    def schedule(self, delay):
        if self.task and not self.task.done():
            return  # a pending update already covers this burst
        self.task = asyncio.get_running_loop().create_task(self._later(delay))

    async def _later(self, delay):
        await self.sleep(delay)
        wait = await self.update()
        if wait:  # only working changes pending, too soon after the last send
            await self._later(wait)

    def purge(self):
        cut = self.clock() - STALE
        for sid in [s for s, v in self.sessions.items() if v["since"] < cut]:
            del self.sessions[sid]

    def listing(self) -> list[dict]:
        self.purge()
        names = [v["project"] for v in self.sessions.values()]
        now = self.clock()
        rows = [{"session_id": s, "project": v["project"] + (f"-{s[:4]}" if names.count(v["project"]) > 1 else ""),
                 "state": v["state"], "since": v["since"], "age": age(now - v["since"])}
                for s, v in self.sessions.items()]
        return sorted(rows, key=lambda r: (ORDER.index(r["state"]), r["since"]))

    def body(self, rows) -> str:
        lines = [f"{ICON[r['state']]} {r['project']}: {r['state']}" + (f" {r['age']}" if r["state"] in ("done", "working") else "")
                 for r in rows]
        return "\n".join(lines)[:BODY_LIMIT]

    async def update(self) -> float:
        """Send the board if the policy says so. Returns seconds to wait when a working-only change is held back, else 0."""
        rows = self.listing()
        now = {r["session_id"]: r["state"] for r in rows}
        if now == self.sent:
            return 0
        session = self.get_session()
        if session is None:
            return 0  # no band: keep state, nothing to send
        if not self.away():
            return 0  # at the PC: the terminal shows it; the next event while away sends the board
        old = self.sent
        loud = set(now) != set(old) or any(s in ("waiting", "done") and old[i] != s for i, s in now.items())
        if not loud and (wait := self.last_send + WORKING_EVERY - self.clock()) > 0:
            return wait
        try:
            if self.nid is not None:
                await self.notifier.remove(session, self.nid)
                self.nid = None
            if rows:
                self.nid = await self.notifier.send(session, TITLE, self.body(rows), reply=False)
        except Exception:  # band hiccup: state is kept, the next event retries
            return 0
        self.sent, self.last_send = now, self.clock()
        return 0


def router(get_session, notifier, **kw) -> APIRouter:
    board, r = Board(get_session, notifier, **kw), APIRouter()

    @r.post("/sessions/event")
    async def session_event(e: Event):
        board.event(e.session_id, e.project, e.event)
        return {"ok": True}

    @r.get("/sessions")
    def sessions():
        return board.listing()

    r.board = board
    return r
