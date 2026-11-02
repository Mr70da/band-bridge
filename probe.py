import argparse
import asyncio
import json
import sys
from pathlib import Path

from band import crypto
from band.packet import TLV, Message, Reassembler, build_frames
from band.transport import Transport

CONFIG = Path("config/band.json")


async def main():
    ap = argparse.ArgumentParser(description="Phase 3 live check: Link Params, Security Negotiation, PIN.")
    ap.add_argument("--address", help="band BLE address (default: config/band.json)")
    args = ap.parse_args()
    cfg = json.loads(CONFIG.read_text())
    if "android_id" not in cfg:  # our persistent identity for HiChain (Phase 4)
        cfg["android_id"] = crypto.random_bytes(16).hex().upper()
        CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")
    t = Transport(args.address or cfg["address"])
    asm = Reassembler()

    async def request(svc, cmd, tlv) -> Message:
        for frame in build_frames(svc, cmd, tlv.serialize()):
            await t.send(frame)
        async with asyncio.timeout(10):
            while True:
                msg = asm.feed(await t.frames.get())
                if msg and (msg.service, msg.command) == (svc, cmd):
                    return msg
                if msg:
                    print(f"ignoring {msg.service:02X}/{msg.command:02X}")

    step = "connect"
    try:
        async with t:
            print(f"ATT mtu {t.client.mtu_size}")

            step = "Link Params"
            p = (await request(0x01, 0x01, TLV().put(1).put(2).put(3).put(4))).tlv
            mtu = p.get_int(3)
            print(f"{step}: protocolVersion={p.get_int(1)} sliceSize={p.get_int(2)} mtu={mtu} "
                  f"interval={p.get_int(4)} authVersion={p.get(5)[1]} deviceSupportType={p.get_int(7)} "
                  f"bondState={p.get_int(9)} encryptMethod={p.get_int(0x0C, 0)}")
            t.chunk_size = min(mtu, 244)  # the band's mtu follows the ATT MTU (protocol.md section 2)

            step = "Security Negotiation"
            req = TLV().put_u8(1, 4).put_u8(2, 1).put(5, cfg["android_id"]).put_u8(3, 1).put_u8(4, 0)
            p = (await request(0x01, 0x33, req.put(6).put(7, "BandBridge"))).tlv
            mode = p.get_int(1, 0)
            auth = 0x186A0 if mode == 1 else -1
            if 2 in p:
                auth = p.get_int(2) ^ (4 if mode == 4 else 0)
            if 0x7F in p:
                auth = p.get(0x7F)[0]  # Gadgetbridge reads the first byte only
            hichain3 = auth == 0x186A0 or auth in (5, 6)  # isHiChain3: authType ^ 1 == 4 or ^ 2 == 4
            print(f"{step}: tag1={mode} tag2={p.get_int(2, -1)} authType={auth} "
                  f"hichain3={hichain3} mac={p.get_str(9)}")

            step = "PIN"
            p = (await request(0x01, 0x2C, TLV().put(1))).tlv
            pin = crypto.decrypt_pin(p.get(1), p.get(2))
            text = pin.decode() if pin.isascii() and pin.decode().isprintable() else None
            print(f"{step}: {len(pin)} bytes hex={pin.hex().upper()} text={text!r}")
            return 0
    except (TimeoutError, ValueError, KeyError, IndexError) as e:
        print(f"{step} failed: {e!r}")
        return 1
    finally:
        print(f"log {t.log_path}")


sys.exit(asyncio.run(main()))
