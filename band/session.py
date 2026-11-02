# Ported from Gadgetbridge (AGPL-3.0): HuaweiSupportProvider.java, DeviceConfig.java, HuaweiUtil.java, P2P.java
import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path

from band import crypto
from band.auth import AuthError, pin_key, run_hichain
from band.packet import TLV, Message, Reassembler, build_frames, decrypt_body, encrypt_body
from band.transport import Transport

HICHAIN3 = (5, 6, 0x186A0)
PRODUCT_TAGS = [0x01, 0x02, 0x07, 0x09, 0x0A, 0x11, 0x12, 0x16, 0x1A, 0x1D, 0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23]
SERVICES = bytes.fromhex("02030405060708090A0B0C0D0E0F101112131415161718191A1B1D202223242526272A2B2D2E3032333435")


def auth_type(p: TLV) -> int:
    """Security Negotiation reply -> authType (DeviceConfig.SecurityNegotiation.Response)."""
    mode = p.get_int(1, 0)
    auth = 0x186A0 if mode == 1 else -1
    if 2 in p:
        auth = p.get_int(2) ^ (4 if mode == 4 else 0)
    if 0x7F in p:
        auth = p.get(0x7F)[0]  # Gadgetbridge reads the first byte only
    return auth


def zone_bytes(offset_seconds: int) -> bytes:
    # HuaweiUtil.getTimeAndZoneId. Java's `% 60` makes the minutes negative for -05:30 (byte 0xE2); we send 30.
    hours, minutes = divmod(abs(offset_seconds) // 60, 60)
    return bytes([(hours | 0x80) if offset_seconds < 0 else hours, minutes])


def phone_info_reply(tags: list[int]) -> TLV:
    """DeviceConfig.PhoneInfo.Request: fixed fake phone values, one per tag the band asked for."""
    out = TLV()
    for t in tags:
        if t in (0x00, 0x0F):
            continue
        elif t in (0x02, 0x04, 0x15):
            out.put(t)
        elif t == 0x08:
            out.put(t, "14")
        elif t == 0x11:
            out.put_u32(t, 1600103320)
        else:
            out.put_u8(t, 0)
    return out


def p2p_ack(tlv: TLV) -> TLV | None:
    """protocol.md 8.4: every band cmdId 1 or 2 gets cmdId 3, same sequence, src/dst swapped, code 0xCF."""
    if tlv.get_int(1, 0) not in (1, 2):
        return None
    return TLV().put_u8(1, 3).put(2, tlv.get(2)).put(3, tlv.get(4)).put(4, tlv.get(3)).put_u32(8, 0xCF)


def _tlv_of(tags) -> TLV:
    """Request TLV with every tag empty = "send me this field"."""
    tlv = TLV()
    for tag in tags:
        tlv.put(tag)
    return tlv


class Session:
    def __init__(self, config_path="config/band.json", log_dir="logs", on_prompt=print, on_message=None):
        self.config_path = Path(config_path)
        self.config = json.loads(self.config_path.read_text())
        if "android_id" not in self.config:
            self.config["android_id"] = crypto.random_bytes(16).hex().upper()
            self.save_config()
        self.log_dir = log_dir
        self.on_prompt = on_prompt
        self.on_message = on_message
        self.transport = None
        self.secret_key = None  # per connection, memory only
        self.slice_size = 0x3FE
        self.model = ""
        self.battery = None
        self.services = {}
        self.disconnected = asyncio.Event()
        self._loop = None
        self._reader = None
        self._pending: dict[tuple[int, int], asyncio.Future] = {}

    def save_config(self):
        tmp = self.config_path.with_name(self.config_path.name + ".tmp")
        tmp.write_text(json.dumps(self.config, indent=2) + "\n")
        os.replace(tmp, self.config_path)  # a half-written file would lose the auth_token

    # --- messages ---------------------------------------------------------------------------

    def _log_msg(self, way, svc, cmd, what):
        if (way, svc, cmd) == ("RX", 1, 0x2C):
            what = "<redacted: holds the PIN>"
        self.transport.log("MSG", f"{way} {svc:02X}/{cmd:02X} {what}")

    async def send(self, svc, cmd, tlv, encrypt=True):
        self._log_msg("TX", svc, cmd, tlv)
        body = tlv.serialize()
        if encrypt and self.secret_key:
            body = encrypt_body(body, self.secret_key)
        for frame in build_frames(svc, cmd, body, self.slice_size):
            await self.transport.send(frame)

    async def request(self, svc, cmd, tlv, encrypt=True, timeout=10) -> Message:
        if self.disconnected.is_set():
            raise ConnectionError("band disconnected")
        if (svc, cmd) in self._pending:
            raise RuntimeError(f"request {svc:02X}/{cmd:02X} already pending")
        fut = self._pending[(svc, cmd)] = asyncio.get_running_loop().create_future()
        try:
            await self.send(svc, cmd, tlv, encrypt)
            async with asyncio.timeout(timeout):
                return await fut
        except TimeoutError:
            raise TimeoutError(f"no reply to {svc:02X}/{cmd:02X}") from None
        finally:
            del self._pending[(svc, cmd)]

    async def _read_loop(self):
        asm = Reassembler()
        while True:
            frame = await self.transport.frames.get()
            try:
                msg = asm.feed(frame)
                if msg is not None:
                    await self._handle(msg)
            except Exception as e:  # one bad frame or callback must not stop the reader
                self.transport.log("EVT", f"reader error {e!r}")

    async def _handle(self, msg: Message):
        t = msg.tlv
        if t is not None and t.get(0x7C, b"\x00") != b"\x00":
            if not self.secret_key:
                self.transport.log("EVT", f"dropped encrypted {msg.service:02X}/{msg.command:02X}, no key yet")
                return
            t = msg.tlv = decrypt_body(t, self.secret_key)
        self._log_msg("RX", msg.service, msg.command, t if t is not None else msg.body.hex().upper())
        key = (msg.service, msg.command)
        if key in self._pending and not self._pending[key].done():
            self._pending[key].set_result(msg)
            return
        if self.on_message:
            self.on_message(msg)
        if t is None:
            return
        if key == (0x01, 0x10) and not (len(t.items) == 1 and t.get_int(0x7F, 0) == 0x186A0):  # not just an ack
            await self.send(0x01, 0x10, phone_info_reply([tag for tag, _ in t.items]))
        elif key == (0x34, 0x01) and (ack := p2p_ack(t)):
            await self.send(0x34, 0x01, ack)

    def _on_disconnect(self):
        # bleak 3.0.2 WinRT already calls this on the loop (it hops there from the WinRT thread itself);
        # call_soon_threadsafe keeps it correct if a backend ever calls from another thread.
        try:
            self._loop.call_soon_threadsafe(self._drop)
        except (AttributeError, RuntimeError):  # never started, or the loop is already closed
            pass

    def _drop(self):
        self.disconnected.set()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("band disconnected"))

    # --- connect -> auth -> ready -----------------------------------------------------------

    async def start(self):
        self._loop = asyncio.get_running_loop()
        self.transport = Transport(self.config["address"], self.log_dir, on_disconnect=self._on_disconnect)
        await self.transport.connect()
        self._reader = asyncio.create_task(self._read_loop())
        try:
            await self._authenticate()
            await self._init_device()
        except BaseException:
            await self.close()
            raise

    async def _authenticate(self):
        cfg = self.config
        p = (await self.request(1, 0x01, _tlv_of([1, 2, 3, 4]), encrypt=False)).tlv
        self.slice_size = p.get_int(2)
        self.transport.chunk_size = min(p.get_int(3), 244)  # the band's mtu follows the ATT MTU (protocol.md 2)

        req = TLV().put_u8(1, 4).put_u8(2, 1).put(5, cfg["android_id"]).put_u8(3, 1).put_u8(4, 0)
        p = (await self.request(1, 0x33, req.put(6).put(7, "BandBridge"), encrypt=False)).tlv
        auth = auth_type(p)
        if auth not in HICHAIN3:
            raise AuthError(f"unsupported auth type {auth}")

        async def hichain(tlv, timeout):
            return (await self.request(1, 0x28, tlv, encrypt=False, timeout=timeout)).tlv

        if not cfg.get("auth_token"):
            p = (await self.request(1, 0x2C, TLV().put(1), encrypt=False)).tlv
            pin = crypto.decrypt_pin(p.get(1), p.get(2))
            self.on_prompt("Tap ✓ on the band now to approve pairing (30 s)")
            token = await run_hichain(hichain, 1, pin_key(pin), cfg["android_id"], step1_timeout=30)
            cfg["auth_token"] = token.hex().upper()
            self.save_config()  # before anything else can fail: re-pairing needs the band's screen
            self.on_prompt("Pairing approved, key saved")
        try:
            self.secret_key = await run_hichain(hichain, 2, bytes.fromhex(cfg["auth_token"]), cfg["android_id"])
        except AuthError as e:
            self.transport.log("EVT", f"op 2 failed: {e}")
            raise AuthError("band rejected the stored key: remove auth_token from config/band.json to re-pair") from e

    async def _init_device(self):
        """Gadgetbridge initializeDeviceConfigure, trimmed to what the bridge needs."""
        cfg = self.config
        p = (await self.request(1, 0x07, _tlv_of(PRODUCT_TAGS))).tlv
        cfg["firmware"] = p.get_str(7) if 7 in p else ""
        self.model = p.get_str(0x0A) if 0x0A in p else ""
        offset = int(datetime.now().astimezone().utcoffset().total_seconds())  # includes DST
        await self.request(1, 0x05, TLV().put_u32(1, int(time.time())).put(2, zone_bytes(offset)))
        await self.read_battery()
        p = (await self.request(1, 0x02, TLV().put(1, SERVICES))).tlv
        self.services = {svc: bool(flag) for svc, flag in zip(SERVICES, p.get(2, b""))}
        cfg["expand_caps"] = (await self.request(1, 0x37, TLV().put(1))).tlv.get(1).hex().upper()
        self.save_config()

    async def read_battery(self) -> int | None:
        """Battery %, or None when the band answers with only an error code (7F), e.g. 0x186A4 seen ~60 s in."""
        t = (await self.request(1, 0x08, TLV().put(1))).tlv
        self.battery = t.get_int(1) if 1 in t else None
        self.last_error = None if 1 in t else t.get_int(0x7F, -1)
        return self.battery

    async def close(self):
        if self._reader:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        if self.transport:
            await self.transport.disconnect()

