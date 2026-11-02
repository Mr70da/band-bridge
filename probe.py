import argparse
import asyncio
import json
import sys

from band.transport import SERVICE, Transport

LINK_PARAMS = bytes.fromhex("5A000B0001010100020003000400F13B")  # real Band 11 capture


async def wait_reply(frames):
    while True:
        frame = await frames.get()
        if frame[4:6] == b"\x01\x01":  # service 0x01, command 0x01
            return frame


async def main():
    ap = argparse.ArgumentParser(description="Connect to the band, send Link Params, print the reply.")
    ap.add_argument("--address", help="band BLE address (default: config/band.json)")
    args = ap.parse_args()
    address = args.address or json.load(open("config/band.json"))["address"]

    t = Transport(address)
    try:
        async with t:
            for svc in t.client.services:
                print(f"service {svc.uuid}  ({len(svc.characteristics)} chars)")
                if svc.uuid == SERVICE:
                    for ch in svc.characteristics:
                        print(f"  char {ch.uuid}  {','.join(ch.properties)}")
            print(f"mtu {t.client.mtu_size}")
            await t.send(LINK_PARAMS)
            try:
                reply = await asyncio.wait_for(wait_reply(t.frames), 10)
            except TimeoutError:
                print("No Link Params reply within 10 s.")
                return 1
            print(f"reply {reply.hex().upper()}")
            return 0
    finally:
        print(f"log {t.log_path}")


sys.exit(asyncio.run(main()))
