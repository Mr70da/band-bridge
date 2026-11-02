import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # config/, logs/ and data/ are relative to the repo root

import argparse
import asyncio

from bleak import BleakScanner


async def main():
    ap = argparse.ArgumentParser(description="Scan for BLE devices, highlight Huawei bands.")
    ap.add_argument("--timeout", type=float, default=10.0, help="scan seconds (default 10)")
    args = ap.parse_args()

    found = await BleakScanner.discover(timeout=args.timeout, return_adv=True)
    first_band = None
    for dev, adv in sorted(found.values(), key=lambda p: p[1].rssi, reverse=True):
        name = dev.name or adv.local_name or "(no name)"
        hit = any(k in name.lower() for k in ("band", "huawei"))
        print(f"{'>>' if hit else '  '} {dev.address}  {adv.rssi:>4} dBm  {name}")
        if hit:
            print(f"     services: {adv.service_uuids}")
            print(f"     manufacturer: { {k: v.hex() for k, v in adv.manufacturer_data.items()} }")
            if first_band is None and "band" in name.lower():
                first_band = (name, dev.address)

    if first_band:
        print(f"Band found: {first_band[0]} {first_band[1]}")
    else:
        print("No Huawei Band found. Is it on, factory-reset and not connected to a phone?")


asyncio.run(main())
