import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from band.packet import TLV, Message, Reassembler, build_frames, decrypt_body, encrypt_body
from band.session import Session, auth_type, zone_bytes

KEY = bytes(range(32))
# the log's older Gadgetbridge build sent 5F5E306C here; the current source sends 1600103320
APP_VERSION = (1600103320).to_bytes(4, "big")


class FakeTransport:
    def __init__(self):
        self.sent, self.logs = [], []
        self.frames = asyncio.Queue()
        self.chunk_size = 20

    async def send(self, frame):
        self.sent.append(frame)

    def log(self, kind, text=""):
        self.logs.append(f"{kind} {text}")


def empty_tags(*tags) -> TLV:
    tlv = TLV()
    for t in tags:
        tlv.put(t)
    return tlv


def msg(svc, cmd, tlv) -> Message:
    return Message(svc, cmd, tlv.serialize(), tlv)


def sent_tlv(frame) -> TLV:
    return Reassembler().feed(frame).tlv


class SessionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "band.json"
        self.path.write_text(json.dumps({"address": "AA:BB:CC:DD:EE:FF", "android_id": "AB" * 16}))
        self.s = Session(self.path, tmp.name)
        self.s.transport = FakeTransport()
        self.sent = self.s.transport.sent

    async def test_phone_info_reply(self):
        await self.s._handle(msg(1, 0x10, empty_tags(0x02, 0x04, 0x08, 0x11, 0x15, 0x10)))
        out = Reassembler().feed(self.sent[0])
        self.assertEqual((out.service, out.command), (1, 0x10))
        self.assertEqual(out.tlv.items, [
            (0x02, b""), (0x04, b""), (0x08, b"14"), (0x11, APP_VERSION), (0x15, b""), (0x10, b"\x00"),
        ])

    async def test_phone_info_reply_to_the_real_band_request(self):  # gb6012.log: RX line 1113 -> TX line 1137
        await self.s._handle(msg(1, 0x10, empty_tags(0x02, 0x04, 0x08, 0x0F, 0x10, 0x11, 0x13, 0x14, 0x15, 0x16)))
        self.assertEqual(sent_tlv(self.sent[0]).items, [
            (0x02, b""), (0x04, b""), (0x08, b"14"), (0x10, b"\x00"), (0x11, APP_VERSION),
            (0x13, b"\x00"), (0x14, b"\x00"), (0x15, b""), (0x16, b"\x00"),
        ])

    async def test_phone_info_ack_is_not_answered(self):
        await self.s._handle(msg(1, 0x10, TLV().put_u32(0x7F, 0x186A0)))
        self.assertEqual(self.sent, [])

    async def test_p2p_ack_for_the_real_band_packet(self):  # protocol.md 8.4, gb6012.log RX line 2180 -> TX 2289
        fingerprint = "603AC6A57E2023E00C9C93BB539CA653DF3003EBA4E92EA1904BA4AAA5D938F0"
        data = (TLV().put_u8(1, 2).put_u16(2, 5).put(3, "com.huawei.watch.home").put(4, "hw.unitedevice.smsquick")
                .put(5, fingerprint).put(6, "UniteDeviceManagement").put(7, bytes.fromhex("010400001B598200")))
        await self.s._handle(msg(0x34, 1, data))
        out = Reassembler().feed(self.sent[0])
        self.assertEqual((out.service, out.command), (0x34, 1))
        self.assertEqual(out.tlv.items, [
            (1, b"\x03"), (2, b"\x00\x05"), (3, b"hw.unitedevice.smsquick"), (4, b"com.huawei.watch.home"),
            (8, bytes.fromhex("000000CF")),
        ])

    async def test_p2p_ack_from_the_band_is_not_answered(self):
        await self.s._handle(msg(0x34, 1, TLV().put_u8(1, 3).put_u16(2, 0xF).put(3, "a").put(4, "b").put_u8(8, 0xCF)))
        self.assertEqual(self.sent, [])

    async def test_unsolicited_goes_to_on_message_and_the_log(self):
        seen = []
        self.s.on_message = seen.append
        m = msg(0x25, 1, TLV().put_u8(1, 7))
        await self.s._handle(m)
        self.assertEqual(seen, [m])
        self.assertIn("MSG RX 25/01 {tag: 1 - Value: 07}", self.s.transport.logs)

    async def test_pin_reply_is_not_logged(self):
        await self.s._handle(msg(1, 0x2C, TLV().put(1, bytes.fromhex("AABBCCDD")).put(2, bytes(16))))
        self.assertEqual([x for x in self.s.transport.logs if "AABBCCDD" in x], [])
        self.assertIn("redacted", self.s.transport.logs[0])

    async def test_encrypted_request_and_reply(self):
        self.s.secret_key = KEY
        reader = asyncio.create_task(self.s._read_loop())
        call = asyncio.create_task(self.s.request(1, 8, TLV().put(1)))
        while not self.sent:
            await asyncio.sleep(0)
        wire = sent_tlv(self.sent[0])
        self.assertEqual(decrypt_body(wire, KEY).items, [(1, b"")])
        reply = encrypt_body(TLV().put_u8(1, 99).serialize(), KEY)
        for frame in build_frames(1, 8, reply):
            self.s.transport.frames.put_nowait(frame)
        self.assertEqual((await call).tlv.get_int(1), 99)
        reader.cancel()

    async def test_unencrypted_reply_is_accepted_with_a_key_set(self):  # the real band answers 01/08 in plain text
        self.s.secret_key = KEY
        reader = asyncio.create_task(self.s._read_loop())
        call = asyncio.create_task(self.s.request(1, 8, TLV().put(1)))
        while not self.sent:
            await asyncio.sleep(0)
        self.s.transport.frames.put_nowait(bytes.fromhex("5A000600010801016335CC"))
        self.assertEqual((await call).tlv.get_int(1), 0x63)
        reader.cancel()

    async def test_timeout_message(self):
        with self.assertRaisesRegex(TimeoutError, "no reply to 01/08"):
            await self.s.request(1, 8, TLV().put(1), timeout=0.01)
        self.assertEqual(self.s._pending, {})

    async def test_disconnect_from_another_thread_fails_pending_requests(self):
        self.s._loop = asyncio.get_running_loop()
        call = asyncio.create_task(self.s.request(1, 8, TLV().put(1)))
        while not self.sent:
            await asyncio.sleep(0)
        await asyncio.to_thread(self.s._on_disconnect)
        with self.assertRaisesRegex(ConnectionError, "disconnected"):
            await call
        self.assertTrue(self.s.disconnected.is_set())
        with self.assertRaises(ConnectionError):
            await self.s.request(1, 8, TLV().put(1))

    def p2p_data(self):  # the band's packet from test_p2p_ack_for_the_real_band_packet
        return msg(0x34, 1, TLV().put_u8(1, 2).put_u16(2, 5).put(3, "com.huawei.watch.home")
                   .put(4, "hw.unitedevice.smsquick").put(7, bytes.fromhex("010400001B598200")))

    async def waiting(self, predicate, timeout=1):
        task = asyncio.create_task(self.s.wait_for(predicate, timeout))
        await asyncio.sleep(0)  # let it register
        return task

    async def test_wait_for_takes_the_message_but_the_automatic_replies_still_go_out(self):
        seen = []
        self.s.on_message = seen.append
        p2p = await self.waiting(lambda m: (m.service, m.command) == (0x34, 1))
        phone = await self.waiting(lambda m: (m.service, m.command) == (1, 0x10))
        m1, m2 = self.p2p_data(), msg(1, 0x10, empty_tags(0x02, 0x04))
        await self.s._handle(m1)
        await self.s._handle(m2)
        self.assertIs(await p2p, m1)
        self.assertIs(await phone, m2)
        self.assertEqual(seen, [])  # consumed, not passed on
        sent = [Reassembler().feed(f) for f in self.sent]
        self.assertEqual([(m.service, m.command) for m in sent], [(0x34, 1), (1, 0x10)])
        self.assertEqual(sent[0].tlv.get_int(1), 3)  # the cmdId 3 ack
        self.assertEqual(sent[1].tlv.items, [(0x02, b""), (0x04, b"")])
        self.assertEqual(self.s._waiters, [])

    async def test_a_message_no_waiter_wants_goes_to_on_message(self):
        seen = []
        self.s.on_message = seen.append
        waiter = await self.waiting(lambda m: m.service == 9)
        m = msg(0x25, 1, TLV().put_u8(1, 7))
        await self.s._handle(m)
        self.assertEqual(seen, [m])
        self.assertFalse(waiter.done())
        waiter.cancel()

    async def test_first_matching_waiter_wins(self):
        first = await self.waiting(lambda m: True)
        second = await self.waiting(lambda m: True)
        a, b = msg(0x25, 1, TLV().put_u8(1, 1)), msg(0x25, 1, TLV().put_u8(1, 2))
        await self.s._handle(a)
        await self.s._handle(b)  # `first` is done but has not run its cleanup yet: it must be skipped
        self.assertIs(await first, a)
        self.assertIs(await second, b)

    async def test_pending_request_has_priority_over_a_waiter(self):
        waiter = await self.waiting(lambda m: True)
        call = asyncio.create_task(self.s.request(1, 8, TLV().put(1)))
        while not self.sent:
            await asyncio.sleep(0)
        reply = msg(1, 8, TLV().put_u8(1, 99))
        await self.s._handle(reply)
        self.assertIs(await call, reply)
        self.assertFalse(waiter.done())
        waiter.cancel()

    async def test_wait_for_timeout(self):
        with self.assertRaisesRegex(TimeoutError, "no matching message"):
            await self.s.wait_for(lambda m: False, 0.01)
        self.assertEqual(self.s._waiters, [])

    async def test_disconnect_fails_waiters_at_once(self):
        self.s._loop = asyncio.get_running_loop()
        waiter = await self.waiting(lambda m: True)
        await asyncio.to_thread(self.s._on_disconnect)
        with self.assertRaisesRegex(ConnectionError, "disconnected"):
            await waiter
        with self.assertRaises(ConnectionError):
            await self.s.wait_for(lambda m: True, 1)

    async def test_p2p_sequence_starts_at_1_and_wraps_at_u16(self):
        self.assertEqual([self.s.next_p2p_seq() for _ in range(3)], [1, 2, 3])
        self.s.p2p_seq = 0xFFFF
        self.assertEqual(self.s.next_p2p_seq(), 0)

    async def test_android_id_is_created_and_saved(self):
        self.path.write_text(json.dumps({"address": "AA:BB:CC:DD:EE:FF"}))
        s = Session(self.path, "unused")
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["android_id"], s.config["android_id"])
        self.assertRegex(saved["android_id"], "^[0-9A-F]{32}$")


class HelpersTest(unittest.TestCase):
    def test_zone_bytes(self):
        h = 3600
        self.assertEqual(zone_bytes(3 * h), bytes.fromhex("0300"))
        self.assertEqual(zone_bytes(-(5 * h + 1800)), bytes.fromhex("851E"))  # -05:30
        self.assertEqual(zone_bytes(0), bytes.fromhex("0000"))
        self.assertEqual(zone_bytes(5 * h + 1800), bytes.fromhex("051E"))
        self.assertEqual(zone_bytes(-12 * h), bytes.fromhex("8C00"))

    def test_auth_type(self):
        base = TLV().put_u8(1, 4)
        self.assertEqual(auth_type(TLV().put_u8(1, 4).put_u8(2, 1)), 5)  # factory-reset band, Phase 3
        self.assertEqual(auth_type(base.put_u8(2, 2)), 6)  # gb6012.log
        self.assertEqual(auth_type(TLV().put_u8(1, 1)), 0x186A0)
        self.assertEqual(auth_type(TLV().put_u8(1, 3)), -1)


if __name__ == "__main__":
    unittest.main()
