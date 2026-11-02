import argparse
import asyncio
import sys

from band.services.notifications import Notifier
from band.session import Session


def prompt(text):
    print(f"\a\n>>> {text.upper()} <<<\n")


async def main():
    ap = argparse.ArgumentParser(description="Phase 6 live test: show a notification, wait for a quick reply.")
    ap.add_argument("--source-app", default="org.telegram.messenger")
    ap.add_argument("--wait", type=float, default=120, help="seconds to wait for a reply")
    ap.add_argument("--no-reply", action="store_true", help="send without a reply key (no quick-reply button)")
    args = ap.parse_args()
    replies = asyncio.Queue()
    notifier = Notifier(on_reply=lambda nid, text, key: replies.put_nowait((nid, text, key)))
    s = Session(on_prompt=prompt, on_message=notifier.on_message)
    got = None
    try:
        await s.start()
        print(f"{s.model} firmware {s.config['firmware']} battery {s.battery}%")
        await notifier.setup(s)
        print(f"notification capabilities: {notifier.capabilities}, content limit: {notifier.content_limit}")
        print(f"quick replies on the band: {notifier.quick_replies}")
        nid = await notifier.send(s, "Band Bridge test", "Pick a quick reply: 1/2/3/yes/no/continue/stop",
                                  reply=not args.no_reply, source_app=args.source_app)
        print(f"sent notification {nid} as {args.source_app}")
        print("\a\n>>> CHECK THE BAND: open the notification and tap a quick reply <<<\n")
        try:
            got = await asyncio.wait_for(replies.get(), args.wait)
            print(f"REPLY id={got[0]} text={got[1]} key={got[2]}")
        except TimeoutError:
            print(f"no reply within {args.wait:g} s")
        await notifier.remove(s, nid)
    except Exception as e:
        print(f"failed: {e!r}")
    finally:
        await s.close()
        if s.transport:
            print(f"log {s.transport.log_path}")
    if not got:
        print("Nothing came back. Try --source-app com.whatsapp, then check the log for 02/01 rejected or 02/10.")
    return 0 if got else 1


sys.stdout.reconfigure(errors="replace")  # the console may not be able to print the check mark
sys.exit(asyncio.run(main()))
