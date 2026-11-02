import argparse
import asyncio
import sys
import time
from datetime import date, datetime, timedelta

from band.fitness_store import DB, FitnessStore
from band.services.fitness import FitnessService
from band.session import Session


def prompt(text):
    print(f"\a\n>>> {text.upper()} <<<\n")


def trace(session):
    """Print every request/reply and send at a high level."""
    request, send = session.request, session.send

    async def traced_request(svc, cmd, tlv, *a, **kw):
        print(f"  -> {svc:02X}/{cmd:02X} {str(tlv)[:90]}")
        try:
            reply = await request(svc, cmd, tlv, *a, **kw)
        except Exception as e:
            print(f"  <- {svc:02X}/{cmd:02X} {e!r}")
            raise
        print(f"  <- {svc:02X}/{cmd:02X} {len(reply.body)} bytes {str(reply.tlv)[:90]}")
        return reply

    async def traced_send(svc, cmd, tlv, *a, **kw):
        print(f"  -> {svc:02X}/{cmd:02X} (no reply expected) {str(tlv)[:90]}")
        return await send(svc, cmd, tlv, *a, **kw)
    session.request, session.send = traced_request, traced_send


def stamp(ts):
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def summary(store, fs):
    today = date.today()
    print("\n== store ==")
    with store._lock:
        counts = {t: store._db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("samples", "sleep", "totals", "spo2", "stress")}
        last = store._db.execute("SELECT * FROM samples ORDER BY ts DESC LIMIT 5").fetchall()
    print("rows:", ", ".join(f"{t} {n}" for t, n in counts.items()), "| last_sync", store.meta("last_sync"))
    d = store.day(today.isoformat())
    print(f"today {d['day']}: {d['steps']} steps, {d['calories']} kcal, {d['distance']} m (from {d['source']})")
    print("last samples (ts, steps, kcal, m, hr):")
    for ts, *rest in reversed(last):
        print(f"  {stamp(ts)} {rest}")
    for day in (today, today - timedelta(days=1)):
        night = store.sleep(day.isoformat())
        if night:
            print(f"sleep, night ending {day}:")
            for seg in night:
                print(f"  {stamp(seg['start'])} - {stamp(seg['end'])} {seg['stage']} ({(seg['end'] - seg['start']) // 60} min)")
            break
    else:
        print("sleep: no segments for the last two nights")
    for name, rows in (("SpO2", store.spo2(today.isoformat())), ("stress", store.stress(today.isoformat()))):
        print(f"{name} today: {len(rows)} readings" + (f", last {rows[-1][1]} at {stamp(rows[-1][0])}" if rows else ""))
    print("\n== steps ==")
    for name, result in fs.report.items():
        print(f"  {name}: {result}")


async def main():
    ap = argparse.ArgumentParser(description="Live check of the fitness sync: one pass into data/fitness.db.")
    ap.add_argument("--days", type=float, help="fetch this many days back instead of since the last sync")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()
    store = FitnessStore(args.db)
    fs = FitnessService(store)
    s = Session(on_prompt=prompt, on_message=lambda m: fs.on_message(m))
    t0 = time.monotonic()
    try:
        await s.start()
        print(f"{s.model} firmware {s.config['firmware']} battery {s.battery}%")
        trace(s)
        fs.attach(s)
        since = int(time.time() - args.days * 86400) if args.days else None
        await fs.sync(s, since=since)
        summary(store, fs)
        return 0 if all(r.startswith("ok") for n, r in fs.report.items() if n != "sleep (07/0C-0D)") else 1
    except Exception as e:
        print(f"failed after {time.monotonic() - t0:.0f} s: {e!r}")
        return 1
    finally:
        await s.close()
        if s.transport:
            print(f"log {s.transport.log_path}")


sys.stdout.reconfigure(errors="replace")
sys.exit(asyncio.run(main()))
