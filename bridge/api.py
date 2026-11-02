import argparse
import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from band.services.notifications import Notifier
from band.supervisor import Supervisor

log = logging.getLogger("bridge")

BODY_LIMIT = 480  # the band takes 500 characters; some room for its own clipping
MAX_OPTIONS = 3  # the band's quick replies are fixed: 1, 2, 3, yes, no, continue, stop
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


def map_reply(reply: str, options: list[str]) -> tuple[str, int | None]:
    """The raw quick reply -> (answer, 1-based option or None)."""
    key = reply.strip().lower()
    if key in ("1", "2", "3") and int(key) <= len(options):
        return options[int(key) - 1], int(key)
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


def create_app(supervisor=None, notifier=None) -> FastAPI:
    """Without arguments it talks to the real band; tests pass fakes."""
    own = supervisor is None
    notifier = notifier or Notifier()
    sup = supervisor or Supervisor(
        on_message=notifier.on_message, on_ready=notifier.setup,
        on_state=lambda state, detail: log.info("band %s %s", state, detail))
    pending: dict[int, asyncio.Future] = {}  # notification id -> raw reply text

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
        yield
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

    @app.post("/ask")
    async def ask(req: Ask):
        if len(req.options) > MAX_OPTIONS:
            return error(422, f"at most {MAX_OPTIONS} options (the band has quick replies 1, 2, 3)")
        session = await ready_session(ASK_WAIT)
        if session is None:
            return not_connected()
        title, body = format_question(req.question, req.options)
        log.info("ask %r options %s", req.question, req.options)
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
