import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta

from band.supervisor import Supervisor

SUMMARY_EVERY = 600  # seconds


async def main():
    ap = argparse.ArgumentParser(description="Phase 5 live test: keep a session ready for hours and report stability.")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--heartbeat", type=float, default=60)
    args = ap.parse_args()
    os.makedirs("logs", exist_ok=True)
    report = open(datetime.now().strftime("logs/soak-%Y%m%d-%H%M%S.txt"), "a", encoding="utf-8")

    def say(text):
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}"
        print(line)
        report.write(line + "\n")
        report.flush()

    sup = Supervisor(heartbeat=args.heartbeat, on_prompt=lambda t: say(f"\a>>> {t.upper()} <<<"),
                     on_state=lambda state, detail: say(f"state {state} {detail}".rstrip()))
    t0 = time.monotonic()

    def summary(label):
        elapsed, s = time.monotonic() - t0, sup.stats
        battery = sup.session.battery if sup.session else None
        say(f"{label} elapsed {timedelta(seconds=int(elapsed))} state {sup.state} connects {s['connects']} "
            f"disconnects {s['disconnects']} ready {100 * s['ready_seconds'] / max(elapsed, 1):.1f}% "
            f"battery {'n/a' if battery is None else f'{battery}%'}")

    task = asyncio.create_task(sup.run())
    try:
        while not task.done() and time.monotonic() - t0 < args.hours * 3600:
            await asyncio.wait({task}, timeout=min(SUMMARY_EVERY, args.hours * 3600 - (time.monotonic() - t0)))
            summary("summary")
    except asyncio.CancelledError:  # Ctrl-C: asyncio.run cancels this task
        say("Ctrl-C, stopping")
    await sup.stop()  # no-op when run() already ended itself (needs_pairing keeps that state)
    summary("final")
    say(f"last error: {sup.stats['last_error']}")
    say(f"ble log {sup.log_path}")
    crashed = task.done() and task.exception()  # a bug in the supervisor itself; session failures never get this far
    if crashed:
        say(f"supervisor crashed: {crashed!r}")
    return 1 if sup.state == "needs_pairing" or crashed else 0


sys.stdout.reconfigure(errors="replace")  # the console may not be able to print the check mark
sys.exit(asyncio.run(main()))
