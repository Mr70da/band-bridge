import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # config/, logs/ and data/ are relative to the repo root

import argparse
import asyncio
import sys
import time

from band.session import Session


def show(msg):
    print(f"  band -> {msg.service:02X}/{msg.command:02X} {str(msg.tlv or msg.body.hex())[:100]}")


def prompt(text):
    print(f"\a\n>>> {text.upper()} <<<\n")


async def main():
    ap = argparse.ArgumentParser(description="Phase 4 live test: connect, pair or reconnect, stay alive.")
    ap.add_argument("--minutes", type=float, default=12)
    minutes = ap.parse_args().minutes
    s = Session(on_prompt=prompt, on_message=show)
    first = not s.config.get("auth_token")
    t0 = time.monotonic()
    try:
        await s.start()
        caps = bytes.fromhex(s.config.get("expand_caps", ""))
        bit = lambda n: int(n // 8 < len(caps) and caps[n // 8] >> (n % 8) & 1)
        print(f"{s.model} firmware {s.config['firmware']} battery {s.battery}% "
              f"({'first pairing' if first else 'reconnect'})")
        print(f"caps: canned replies (82) = {bit(82)}, reply (89) = {bit(89)}")
        while time.monotonic() - t0 < minutes * 60:
            try:
                await asyncio.wait_for(s.disconnected.wait(), 60)
                print("band disconnected")
                return 1
            except TimeoutError:
                pass
            await s.read_battery()  # any reply, even an error code, proves the link is alive
            m, sec = divmod(int(time.monotonic() - t0), 60)
            state = f"battery {s.battery}%" if s.battery is not None else f"battery rejected 7F={s.last_error:X}"
            print(f"alive {m:02d}:{sec:02d} {state}")
        return 0
    except Exception as e:
        print(f"failed after {time.monotonic() - t0:.0f} s: {e!r}")
        return 1
    finally:
        await s.close()
        if s.transport:
            print(f"log {s.transport.log_path}")


sys.stdout.reconfigure(errors="replace")  # the console may not be able to print the check mark
sys.exit(asyncio.run(main()))
