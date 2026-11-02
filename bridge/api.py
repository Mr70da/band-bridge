import argparse
import asyncio
import json
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from band.services.music import MusicService
from band.services.weather import WeatherService
from band.services.notifications import Notifier
from band.supervisor import Supervisor
from bridge.away_lock import AwayLock
from bridge.win_notify import NotificationMirror

CONFIG = "config/band.json"

log = logging.getLogger("bridge")

BODY_LIMIT = 480  # the band takes 500 characters; some room for its own clipping
REPLY_COUNT = 10  # Gadgetbridge assumes 10 quick-reply slots (HuaweiState.getCannedRepliesSlotCount)
REPLY_LIMIT = 30  # assumed: Gadgetbridge states no length limit; check on the band
EXTRA_REPLIES = ["yes", "no", "continue", "stop"]  # always offered after the options
MAX_OPTIONS = min(5, REPLY_COUNT - 2)  # room is left for yes/no
OPTION_LIMIT = 100
ASK_WAIT, NOTIFY_WAIT = 15, 5  # seconds to wait for a connected band
ASK_TITLE = "Claude asks"


def shorten(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:max(limit - 1, 0)].rstrip() + "…"


def format_question(question: str, options: list[str]) -> tuple[str, str]:
    """(title, body) for the band, at most BODY_LIMIT characters. The question is trimmed, never the numbers."""
    lines = [f"{i}) {shorten(o, OPTION_LIMIT)}" for i, o in enumerate(options, 1)]
    block = "\n".join(lines)
    room = BODY_LIMIT - (len(block) + 1 if block else 0)
    return ASK_TITLE, "\n".join([shorten(question, room)] + lines)


def quick_replies(options: list[str]) -> list[str]:
    """What the band shows as quick replies: the options (clipped), then yes/no/continue/stop; no duplicates."""
    out, seen = [], set()
    for text in [*(shorten(o, REPLY_LIMIT) for o in options), *EXTRA_REPLIES]:
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out[:REPLY_COUNT]


def map_reply(reply: str, options: list[str]) -> tuple[str, int | None]:
    """The raw quick reply -> (answer, 1-based option or None): a number, or the option's text (clipped or not)."""
    key = reply.strip().lower()
    if key in [str(i) for i in range(1, len(options) + 1)]:
        return options[int(key) - 1], int(key)
    for i, option in enumerate(options, 1):
        if key in (option.strip().lower(), shorten(option, REPLY_LIMIT).lower()):
            return option, i
    return key, None


class Ask(BaseModel):
    question: str
    options: list[str] = []
    timeout: float = Field(300, gt=0)


class Notify(BaseModel):
    text: str
    title: str = "Claude Code"


def error(status, message, **extra):
    return JSONResponse({"error": message, **extra}, status_code=status)


def chain_on_state(extra=None):
    """Log every state change, then call extra(state, detail) (the tray) if given."""
    def on_state(state, detail):
        log.info("band %s %s", state, detail)
        if extra:
            extra(state, detail)
    return on_state


def fan_out(notifier, services):
    """(on_ready, on_message) for the Supervisor. A service may have async setup(session) and on_message(msg);
    its failure is logged, never fatal: only the notifier's setup can fail a connect."""
    async def on_ready(session):
        await notifier.setup(session)
        for s in services:
            if hasattr(s, "setup"):
                try:
                    await s.setup(session)
                except Exception as e:
                    log.warning("%s setup failed: %r", type(s).__name__, e)

    def on_message(msg):
        for h in (notifier, *services):
            if hasattr(h, "on_message"):
                try:
                    h.on_message(msg)
                except Exception as e:
                    log.warning("%s on_message failed: %r", type(h).__name__, e)
    return on_ready, on_message


def load_config(path=CONFIG) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def create_app(supervisor=None, notifier=None, on_state=None, services=None) -> FastAPI:
    """Without arguments it talks to the real band (with music control and the notification mirror); tests pass
    fakes. on_state only reaches a Supervisor made here. A service's run() lives as long as the app."""
    own = supervisor is None
    notifier = notifier or Notifier()
    cfg = load_config() if own else {}
    services = list(services if services is not None else
                    [MusicService(), WeatherService(cfg.get("weather"))] if own else [])
    on_ready, on_message = fan_out(notifier, services)
    sup = supervisor or Supervisor(on_message=on_message, on_ready=on_ready, on_state=chain_on_state(on_state))
    if own:
        async def mirror_send(title, text):
            if sup.session is not None:
                await notifier.send(sup.session, title, shorten(text, BODY_LIMIT), reply=False)
        services += [NotificationMirror(mirror_send, cfg.get("mirror_notifications")),
                     AwayLock(lambda: sup.state, cfg.get("lock_on_leave"))]
    pending: dict[int, asyncio.Future] = {}  # notification id -> raw reply text
    push_lock = asyncio.Lock()
    active = 0  # asks between start and end (pending only counts those already sent)
    custom = False  # the band may hold an ask's list instead of the default

    def on_reply(nid, text, key):
        future = pending.get(nid)
        if future is None:
            log.warning("reply %r for unknown notification %s (%s)", text, nid, key)
        elif not future.done():
            future.set_result(text)

    notifier.on_reply = on_reply

    @asynccontextmanager
    async def lifespan(_):
        task = asyncio.create_task(sup.run()) if own else None
        runs = [asyncio.create_task(s.run()) for s in services if hasattr(s, "run")]
        yield
        for r in runs:
            r.cancel()
        await asyncio.gather(*runs, return_exceptions=True)
        if task:
            await sup.stop()
            await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(title="Band Bridge", lifespan=lifespan)

    async def ready_session(wait):
        try:
            await asyncio.wait_for(sup.ready.wait(), wait)
        except TimeoutError:
            return None
        return sup.session

    def not_connected():
        return error(503, "band not connected", state=sup.state)

    async def push_replies(session, replies):
        """replies=None restores the default, but only when no ask is active. A failure is logged: numbers still work."""
        nonlocal custom
        async with push_lock:
            if replies is None:
                if active or not custom:
                    return
                custom = False
            else:
                custom = True
            try:
                await notifier.set_replies(session, replies)
            except Exception as e:
                log.warning("quick replies %s failed: %r", replies or "restore", e)

    @app.post("/ask")
    async def ask(req: Ask):
        nonlocal active
        if len(req.options) > MAX_OPTIONS:
            return error(422, f"at most {MAX_OPTIONS} options")
        session = await ready_session(ASK_WAIT)
        if session is None:
            return not_connected()
        title, body = format_question(req.question, req.options)
        log.info("ask %r options %s", req.question, req.options)
        active += 1
        try:
            if req.options:
                await push_replies(session, quick_replies(req.options))
            nid = await notifier.send(session, title, body, reply=True)
            future = pending[nid] = asyncio.get_running_loop().create_future()
            try:
                reply = await asyncio.wait_for(future, req.timeout)
            except TimeoutError:
                log.info("ask %s: no answer within %g s", nid, req.timeout)
                return error(504, f"no answer within {req.timeout:g} s")
            finally:
                del pending[nid]
                try:  # best effort: the band may be gone, or the answer already cleared it
                    await notifier.remove(sup.session or session, nid)
                except Exception as e:
                    log.warning("remove %s failed: %r", nid, e)
        finally:
            active -= 1
            await push_replies(sup.session or session, None)
        answer, option = map_reply(reply, req.options)
        log.info("answer %s: %r (option %s)", nid, answer, option)
        return {"answer": answer, "reply": reply, "option": option, "notification_id": nid}

    @app.post("/notify")
    async def notify(req: Notify):
        session = await ready_session(NOTIFY_WAIT)
        if session is None:
            return not_connected()
        log.info("notify %r", req.text)
        return {"notification_id": await notifier.send(session, req.title, shorten(req.text, BODY_LIMIT),
                                                       reply=False)}

    @app.get("/status")
    async def status():
        s = sup.session
        return {"state": sup.state, "battery": getattr(s, "battery", None), "model": getattr(s, "model", None),
                "firmware": s.config.get("firmware") if s else None, "stats": sup.stats,
                "pending_asks": len(pending)}

    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Band Bridge: local HTTP API in front of the band.")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port)  # 127.0.0.1 only: no auth on this API
