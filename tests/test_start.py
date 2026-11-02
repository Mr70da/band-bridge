import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from band import crypto
from band.auth import AuthError
from band.packet import TLV, Reassembler, build_frames, decrypt_body, encrypt_body
from band.session import SERVICES, Session
from tests.fake_band import FakeBand

PIN = bytes(range(16))
TOKEN = bytes(range(100, 132))
CAPS = bytes.fromhex("FF7F014226C37442A0A6BC7222698A4B87AAA10D6EFDCC8A8F511901594DA48A0A42100000")


class FakeLinkTransport:
    """Stands in for Transport: frames written by the session are answered by a band, like over BLE."""

    band = None  # set per test

    def __init__(self, address, log_dir="logs", on_disconnect=None, log_path=None):
        self.frames = asyncio.Queue()
        self.chunk_size = 20
        self.log_path = "fake.log"
        self.logs = []
        self._asm = Reassembler()

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    def log(self, kind, text=""):
        self.logs.append(f"{kind} {text}")

    async def send(self, frame):
        msg = self._asm.feed(frame)
        if msg is None:
            return
        key = self.band.secret_key
        if 0x7C in msg.tlv:
            msg.tlv = decrypt_body(msg.tlv, key)
            self.band.encrypted.add((msg.service, msg.command))
        answer = await self.band.answer(msg)
        if answer is None:  # the band does not reply to every message
            return
        reply, encrypted = answer
        body = reply.serialize()
        for out in build_frames(msg.service, msg.command, encrypt_body(body, key) if encrypted else body):
            self.frames.put_nowait(out)


class LinkBand(FakeBand):
    """FakeBand's HiChain plus the plain requests the session sends around it."""

    def __init__(self, tag2=1):
        super().__init__(PIN, TOKEN)
        self.tag2, self.seen, self.encrypted = tag2, {}, set()
        self.rejected = set()  # (svc, cmd) pairs answered with 7F = 0x186A4

    async def answer(self, msg):
        t = msg.tlv
        self.seen[(msg.service, msg.command)] = t
        if (msg.service, msg.command) in self.rejected:
            return TLV().put_u32(0x7F, 0x186A4), True
        match (msg.service, msg.command):
            case (1, 0x01):
                return (TLV().put_u8(1, 2).put_u16(2, 0x3FE).put_u16(3, 0x202).put_u16(4, 0).put_u8(7, 4)), False
            case (1, 0x33):
                return TLV().put_u8(1, 4).put_u8(2, self.tag2).put_u8(3, 1).put_u8(4, 0), False
            case (1, 0x2C):
                iv = bytes(range(16))
                return TLV().put(1, crypto.aes_cbc_encrypt(crypto.DIGEST_SECRET_V1, iv, PIN)).put(2, iv), False
            case (1, 0x28):
                return await self(t, 5), False
            case (1, 0x07):
                return TLV().put(7, "6.0.0.113(SP9C00M00)").put(0x0A, "Candy-B19FB"), True
            case (1, 0x05):
                return TLV().put_u32(0x7F, 0x186A0).put(1, t.get(1)).put(2, t.get(2)), False
            case (1, 0x08):
                return TLV().put_u8(1, 99), False
            case (1, 0x02):
                return TLV().put(2, bytes([1, 0] * 21 + [1])), True
            case (1, 0x37):
                return TLV().put(1, CAPS), True
            case (0x1A, 0x05):
                return TLV().put_u8(2, 0), True
            case (1, 0x31):
                return TLV().put_u8(1, 0x0F).put_u8(2, 0x0E).put_u8(3, 3), True
            case (1, 0x30 | 0x3F | 0x35):
                return TLV().put_u32(0x7F, 0x186A0), True
            case (1, 0x3D):
                return TLV().put_u8(1, 2), True
            # (1, 0x3E) gets no reply: falls through and returns None


class StartTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "band.json"
        self.band = FakeLinkTransport.band = LinkBand()
        patch = mock.patch("band.session.Transport", FakeLinkTransport)
        patch.start()
        self.addCleanup(patch.stop)
        self.prompts = []

    def session(self, **config):
        self.path.write_text(json.dumps({"address": "AA:BB", "android_id": "AB" * 16, **config}))
        return Session(self.path, "unused", on_prompt=self.prompts.append)

    async def test_first_pairing_then_reconnect(self):
        s = self.session()
        await s.start()
        saved = json.loads(self.path.read_text())
        self.assertEqual(bytes.fromhex(saved["auth_token"]), TOKEN)
        self.assertEqual(saved["firmware"], "6.0.0.113(SP9C00M00)")
        self.assertEqual(bytes.fromhex(saved["expand_caps"]), CAPS)
        self.assertEqual((s.model, s.battery), ("Candy-B19FB", 99))
        self.assertEqual(s.secret_key, self.band.secret_key)
        self.assertEqual(s.transport.chunk_size, 244)
        self.assertEqual(self.prompts, [
            "Tap ✓ on the band now to approve pairing (30 s)", "Pairing approved, key saved"])
        self.assertEqual(list(s.services.values()), [bool(b) for b in bytes([1, 0] * 21 + [1])])
        self.assertEqual(list(s.services), list(SERVICES))
        self.assertEqual(self.band.seen[(1, 0x02)].get(1), SERVICES)
        clock = self.band.seen[(1, 0x05)]
        self.assertLess(abs(clock.get_int(1) - time.time()), 5)
        self.assertEqual(len(clock.get(2)), 2)
        await s.close()

        self.prompts.clear()
        self.band = FakeLinkTransport.band = LinkBand(tag2=2)  # a known peer: authType 6
        s = self.session(auth_token=TOKEN.hex())
        await s.start()
        self.assertEqual(self.prompts, [])
        self.assertNotIn((1, 0x2C), self.band.seen)
        self.assertEqual(s.secret_key, self.band.secret_key)
        await s.close()

    async def test_setup_messages_follow_init_in_order(self):  # protocol.md section 5, "Required post-init setup"
        s = self.session(name="HUAWEI Band 11-572")
        await s.start()
        order = list(self.band.seen)
        setup = order[order.index((1, 0x37)) + 1:]
        self.assertEqual(setup, [(0x1A, 5), (1, 0x31), (1, 0x30), (1, 0x3F), (1, 0x3E), (1, 0x3D), (1, 0x35)])
        self.assertTrue(set(setup) <= self.band.encrypted)
        seen = self.band.seen
        self.assertEqual(seen[(0x1A, 5)].items, [(1, b"\x00"), (3, b"\x01")])
        self.assertEqual(seen[(1, 0x31)].items, [(tag, b"") for tag in range(1, 7)])
        agreements = [TLV.parse(a) for a in seen[(1, 0x30)].get_tlv(0x81).get_all(0x82)]
        self.assertEqual([a.get_str(3) for a in agreements], [
            "software_update_service_statement", "device_information_management", "user_license_agreement"])
        self.assertEqual(agreements[0].items[1:3], [(4, b"\x01"), (5, b"20230508-20230508-0-0")])
        self.assertLess(abs(int(agreements[0].get_str(6)) / 1000 - time.time()), 5)
        self.assertEqual(seen[(1, 0x3F)].items, [(1, bytes.fromhex("FDF7737A"))])
        self.assertEqual(seen[(1, 0x3E)].items, [(1, b"\x01"), (2, b"HUAWEI Band 11-572"), (3, b"\x00")])
        self.assertEqual(seen[(1, 0x3D)].items, [(1, b"")])
        self.assertEqual(seen[(1, 0x35)].items, [(1, b"\x01")])
        await s.close()

    async def test_rejected_setup_reply_is_logged_not_raised(self):
        self.band.rejected.add((1, 0x35))
        s = self.session()
        await s.start()
        self.assertIn("EVT setup 01/35 rejected, 7F=186A4", s.transport.logs)
        await s.close()

    async def test_rejected_stored_key_is_not_replaced(self):
        s = self.session(auth_token=bytes(32).hex())
        with self.assertRaisesRegex(AuthError, "remove auth_token from config/band.json to re-pair"):
            await s.start()
        self.assertEqual(json.loads(self.path.read_text())["auth_token"], bytes(32).hex())
        self.assertEqual(self.prompts, [])

    async def test_unsupported_auth_type(self):
        self.band.tag2 = 9
        s = self.session()
        with self.assertRaisesRegex(AuthError, "unsupported auth type 13"):
            await s.start()


if __name__ == "__main__":
    unittest.main()
