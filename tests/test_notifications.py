import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from band.packet import TLV, Message, Reassembler, build_frames
from band.services import notifications as n
from band.services.notifications import Notifier
from band.session import Session

# gb6012.log line 1757: the band's 02/02 reply, the value of tag 81 (content 500, title 120, then 60, 30, 30, 30)
CONSTRAINTS = bytes.fromhex(
    "824F03017F84009048910A120101130103140201F4910A12010213010314020078910A1201031301031402003C"
    "910A1201051301031402001E910A1201071301031402001E910A1201061301031402001E")
KEY = "0|org.telegram.messenger|1234||0"


def msg(svc, cmd, tlv) -> Message:
    return Message(svc, cmd, tlv.serialize(), tlv)


def reply_event(text: bytes, key=KEY, type=2, encoding=3) -> Message:
    return msg(2, 0x10, TLV().put_u8(1, type).put_u8(2, encoding).put_u8(3, 0).put(4, key).put(5, "").put(6, text))


def band_packet(inner: bytes, cmd_id=2, seq=5, dst=n.MODULE) -> Message:
    """What the band sends for the quick-reply module (gb6012.log line 174)."""
    return msg(0x34, 1, TLV().put_u8(1, cmd_id).put_u16(2, seq).put(3, n.PEER).put(4, dst).put(5, n.PEER_FP)
               .put(6, n.LOCAL_FP).put(7, inner))


class NotificationTlvTest(unittest.TestCase):
    def test_gadgetbridge_notification_action_request(self):
        # TestNotifications.testNotificationActionRequest, same inputs, no additional params: byte for byte
        tlv = n.notification_tlv(1, 0x02, "Title", "Sender", "Body", "SourceApp")
        self.assertEqual(tlv.serialize(),
                         b"\x01\x02\x00\x01" b"\x02\x01\x02" b"\x03\x01\x01"
                         b"\x84\x2f" b"\x8c\x2d"
                         b"\x8d\x0d\x0e\x01\x03\x0f\x01\x02\x10\x05Title"
                         b"\x8d\x0e\x0e\x01\x02\x0f\x01\x02\x10\x06Sender"
                         b"\x8d\x0c\x0e\x01\x01\x0f\x01\x02\x10\x04Body"
                         b"\x11\x09SourceApp")

    def test_gadgetbridge_set_notification_and_wear_message_tlvs(self):
        # testSetNotificationRequest (true/false) and the ignored testSetWearMessagePushRequest, TLV level only: the
        # test's encrypted bytes use AES-CBC with a zero IV, we use GCM
        enable = lambda on: TLV().put(0x81, TLV().put(2, on).put(3, on)).serialize()
        self.assertEqual(enable(True), bytes.fromhex("8106020101030101"))
        self.assertEqual(enable(False), bytes.fromhex("8106020100030100"))
        self.assertEqual(TLV().put(1, True).serialize(), bytes.fromhex("010101"))

    def test_full_notification_for_fixed_inputs(self):
        tlv = n.notification_tlv(7, 0x7F, "Q", "Claude Code", "Hi", "org.telegram.messenger", "msg", KEY, 1700000000,
                                 KEY, 1234)
        self.assertEqual(tlv.serialize(),
                         b"\x01\x02\x00\x07" b"\x02\x01\x7f" b"\x03\x01\x01"
                         b"\x84\x2e" b"\x8c\x2c"
                         b"\x8d\x09\x0e\x01\x03\x0f\x01\x02\x10\x01Q"
                         b"\x8d\x13\x0e\x01\x02\x0f\x01\x02\x10\x0bClaude Code"
                         b"\x8d\x0a\x0e\x01\x01\x0f\x01\x02\x10\x02Hi"
                         b"\x11\x16org.telegram.messenger" b"\x12\x03msg"
                         b"\x18\x20" + KEY.encode() + b"\x15\x04\x65\x53\xf1\x00"
                         b"\x19\x20" + KEY.encode() + b"\x1a\x04\x00\x00\x04\xd2" b"\x1b\x00")

    def test_empty_values_and_a_long_source_app_are_handled_like_gadgetbridge(self):
        tlv = n.notification_tlv(1, 0x7F, "", "", "Hi", "x" * 200)
        texts = tlv.get_tlv(0x84).get_tlv(0x8C).get_all(0x8D)
        self.assertEqual([0x10 in TLV.parse(t) for t in texts], [False, False, True])  # tag 10 omitted when empty
        self.assertEqual(tlv.get(0x11), b"x" * 127)
        self.assertEqual([tag for tag, _ in tlv.items], [1, 2, 3, 0x84, 0x11])

    def test_body_is_cut_to_the_limit_minus_13_plus_dots(self):
        self.assertEqual(n.clip("a" * 500, 500), "a" * 500)
        self.assertEqual(n.clip("a" * 501, 500), "a" * 487 + "...")
        self.assertEqual(len(n.clip("a" * 501, 500)), 490)

    def test_content_limit_from_the_real_constraints_reply(self):
        self.assertEqual(n.content_limit(TLV().put(0x81, CONSTRAINTS)), 500)
        limit200 = CONSTRAINTS.replace(bytes.fromhex("140201F4"), bytes.fromhex("140200C8"))
        self.assertEqual(n.content_limit(TLV().put(0x81, limit200)), 200)

    def test_content_limit_falls_back_when_the_reply_has_another_shape(self):
        self.assertEqual(n.content_limit(TLV()), 500)
        self.assertEqual(n.content_limit(TLV().put_u32(0x7F, 0x186A4)), 500)
        self.assertEqual(n.content_limit(TLV().put(0x81, b"\x82\xff")), 500)  # overruns


class QuickReplyTlvTest(unittest.TestCase):
    def test_query_is_byte_exact_with_the_real_band_capture(self):  # gb6012.log line 2009, seq 000F
        tlv = n.p2p_packet(0x0F, TLV().put_u32(1, n.QUERY))
        self.assertEqual(tlv.items, [
            (1, bytes.fromhex("02")),
            (2, bytes.fromhex("000F")),
            (3, bytes.fromhex("68772E756E6974656465766963652E736D73717569636B")),  # hw.unitedevice.smsquick
            (4, bytes.fromhex("636F6D2E6875617765692E77617463682E686F6D65")),  # com.huawei.watch.home
            (5, bytes.fromhex("556E6974654465766963654D616E6167656D656E74")),  # UniteDeviceManagement
            (6, bytes.fromhex("36303341433641353745323032334530304339433933424235333943413635334446333030334542"
                              "413445393245413139303442413441414135443933384630")),  # the 64-char fingerprint
            (7, bytes.fromhex("010400001B59")),
        ])

    def test_update_data(self):
        self.assertEqual(n.replies_update(["1", "yes"]).serialize(), bytes.fromhex(
            "0104" "00001B5A" "820C" "8303040131" "83050403796573"))
        self.assertEqual(n.replies_update(n.QUICK_REPLIES).serialize(), bytes.fromhex(
            "0104" "00001B5A" "8230" "8303040131" "8303040132" "8303040133" "83050403796573" "830404026E6F"
            "830A0408" "636F6E74696E7565" "83060404" "73746F70"))

    def test_update_packet_carries_the_update_as_tag_7(self):
        tlv = n.p2p_packet(1, n.replies_update(["1"]))
        self.assertEqual(tlv.get_int(1), 2)
        self.assertEqual(TLV.parse(tlv.get(7)).get_int(1), 7002)

    def test_empty_list_is_refused(self):
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            asyncio.run(n.set_quick_replies(mock.Mock(), []))

    def test_parse_the_empty_list_from_the_real_band(self):  # gb6012.log line 174: 01 04 00001B59 82 00
        data = n.band_data(band_packet(bytes.fromhex("010400001B598200")))
        self.assertEqual(data.get_int(1), 7001)
        self.assertEqual(n.parse_replies(data), [])

    def test_parse_a_list(self):
        inner = bytes.fromhex("010400001B59" "820C" "8303040131" "83050403796573")
        self.assertEqual(n.parse_replies(n.band_data(band_packet(inner))), ["1", "yes"])

    def test_parse_skips_empty_slots_and_a_missing_list(self):
        padded = bytes.fromhex("010400001B59" "820E" "8303040131" "83020400" "8303050141")  # empty text, no text tag
        self.assertEqual(n.parse_replies(n.band_data(band_packet(padded))), ["1"])
        self.assertEqual(n.parse_replies(TLV().put_u32(1, 7001)), [])

    def test_band_data_ignores_other_packets(self):
        inner = bytes.fromhex("010400001B598200")
        self.assertIsNone(n.band_data(band_packet(inner, cmd_id=3)))  # an ack
        self.assertIsNone(n.band_data(band_packet(inner, dst="hw.unitedevice.calendarapp")))
        self.assertIsNone(n.band_data(band_packet(b"\x01\x09")))  # truncated inner TLV
        self.assertIsNone(n.band_data(msg(2, 0x10, TLV().put_u8(1, 2))))


class ReplyEventTest(unittest.TestCase):
    def setUp(self):
        self.got = []
        self.notifier = Notifier(on_reply=lambda *args: self.got.append(args))
        self.notifier.sent[KEY] = 1234

    def test_utf16_without_bom_is_big_endian(self):
        self.assertEqual(self.notifier.handle(reply_event("yes".encode("utf-16-be"))), (1234, "yes"))
        self.assertEqual(self.got, [(1234, "yes", KEY)])

    def test_utf16_with_bom(self):
        self.assertEqual(self.notifier.handle(reply_event("continue".encode("utf-16"))), (1234, "continue"))  # LE BOM
        self.assertEqual(self.notifier.handle(reply_event(b"\xfe\xff" + "no".encode("utf-16-be"))), (1234, "no"))

    def test_other_encodings_are_utf8(self):
        self.assertEqual(self.notifier.handle(reply_event("stop ✓".encode(), encoding=1)), (1234, "stop ✓"))
        self.assertEqual(self.notifier.handle(reply_event(b"2", encoding=0)), (1234, "2"))

    def test_unknown_key_gives_no_id(self):
        self.assertEqual(self.notifier.handle(reply_event(b"\x001", key="0|other|9||0")), (None, "1"))
        self.assertEqual(self.got, [(None, "1", "0|other|9||0")])

    def test_other_messages_are_not_replies(self):
        self.assertIsNone(self.notifier.handle(msg(1, 8, TLV().put_u8(1, 99))))
        self.assertIsNone(self.notifier.handle(msg(2, 0x10, TLV().put_u32(0x7F, 0x186A0))))  # e.g. an ack of our ack
        self.assertEqual(self.got, [])

    def test_only_an_sms_reply_gets_an_ack(self):
        self.assertEqual(n.reply_ack(reply_event(b"1", type=1).tlv).items, [(7, b"\xff")])
        self.assertIsNone(n.reply_ack(reply_event(b"1", type=2).tlv))


class FakeBand:
    """The band's side of service 2 and of the quick-reply P2P module (plaintext: the test session has no key)."""

    def __init__(self, constraints=CONSTRAINTS, stored=()):
        self.constraints, self.stored = constraints, list(stored)
        self.received = []  # (svc, cmd, tlv) of everything the session sent
        self.mute = set()  # (svc, cmd) the band never answers
        self.reject = set()  # (svc, cmd) answered with an error code

    def answer(self, msg) -> list[tuple[int, int, TLV]]:
        key, t = (msg.service, msg.command), msg.tlv
        self.received.append((*key, t))
        if key in self.mute:
            return []
        ok = TLV().put_u32(0x7F, 0x186A4 if key in self.reject else 0x186A0)
        if key == (2, 5):
            return [(2, 5, TLV().put_u8(1, 1))]
        if key == (2, 2):
            return [(2, 2, TLV().put(0x81, self.constraints))]
        if key in ((2, 4), (2, 8), (2, 1), (2, 6)):
            return [(*key, ok)]
        if key == (0x34, 1) and t.get_int(1) == 2:
            seq, inner = t.get_int(2), TLV.parse(t.get(7))
            ack = (0x34, 1, TLV().put_u8(1, 3).put_u16(2, seq).put(3, n.PEER).put(4, n.MODULE).put_u8(8, 0xCF))
            if inner.get_int(1) == n.UPDATE:
                self.stored = n.parse_replies(inner)
                return [ack]
            items = TLV()
            for reply in self.stored:
                items.put(0x83, TLV().put(4, reply))
            data = TLV().put_u32(1, n.QUERY).put(0x82, items)
            return [(0x34, 1, band_packet(data.serialize()).tlv), ack]  # the log shows the data before the ack
        return []

    def of(self, svc, cmd) -> list[TLV]:
        return [t for s, c, t in self.received if (s, c) == (svc, cmd)]


class BandTransport:
    def __init__(self, band):
        self.band, self.frames, self.logs, self._asm = band, asyncio.Queue(), [], Reassembler()
        self.chunk_size = 20

    def log(self, kind, text=""):
        self.logs.append(f"{kind} {text}")

    async def send(self, frame):
        m = self._asm.feed(frame)
        if m is None:
            return
        for svc, cmd, tlv in self.band.answer(m):  # answered before send() returns: the fastest a band could be
            for out in build_frames(svc, cmd, tlv.serialize()):
                self.frames.put_nowait(out)

    def push(self, svc, cmd, tlv):
        for out in build_frames(svc, cmd, tlv.serialize()):
            self.frames.put_nowait(out)


class NotifierFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "band.json"
        self.path.write_text(json.dumps({"address": "AA:BB:CC:DD:EE:FF", "android_id": "AB" * 16}))
        patch = mock.patch.object(n, "TIMEOUT", 0.05)
        patch.start()
        self.addCleanup(patch.stop)
        self.band = FakeBand()
        self.replies = []
        self.notifier = Notifier(on_reply=lambda *args: self.replies.append(args))
        self.s = Session(self.path, tmp.name, on_message=self.notifier.on_message)
        self.s.transport = BandTransport(self.band)
        reader = asyncio.create_task(self.s._read_loop())

        async def stop():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

        self.addAsyncCleanup(stop)

    async def until(self, cond):
        async with asyncio.timeout(2):
            while not cond():
                await asyncio.sleep(0)

    async def test_setup(self):
        await self.notifier.setup(self.s)
        self.assertEqual([k for k in dict.fromkeys((s, c) for s, c, _ in self.band.received)],
                         [(2, 5), (2, 2), (2, 4), (2, 8), (0x34, 1)])
        self.assertEqual(self.band.of(2, 5)[0].items, [(1, b"")])
        self.assertEqual(self.band.of(2, 2)[0].items, [(1, b"")])
        self.assertEqual(self.band.of(2, 4)[0].serialize(), bytes.fromhex("8106020101030101"))
        self.assertEqual(self.band.of(2, 8)[0].serialize(), bytes.fromhex("010101"))
        update, query, ack = self.band.of(0x34, 1)
        self.assertEqual((update.get_int(1), update.get_int(2), TLV.parse(update.get(7)).get_int(1)), (2, 1, 7002))
        self.assertEqual((query.get_int(1), query.get_int(2), query.get(7)), (2, 2, bytes.fromhex("010400001B59")))
        self.assertEqual(self.band.stored, n.QUICK_REPLIES)
        self.assertEqual(self.notifier.quick_replies, n.QUICK_REPLIES)
        self.assertEqual((self.notifier.capabilities, self.notifier.content_limit), (1, 500))
        # the band's data packet (its seq 5) was not handed to on_message, yet Session acked it (protocol.md 8.4)
        self.assertEqual(ack.items, [(1, b"\x03"), (2, b"\x00\x05"), (3, n.MODULE.encode()), (4, n.PEER.encode()),
                                     (8, bytes.fromhex("000000CF"))])
        self.assertIn("EVT notify setup: capabilities 1, content limit 500, update acked CF, band has "
                      f"{n.QUICK_REPLIES}", self.s.transport.logs)

    async def test_config_overrides_the_quick_replies(self):
        self.s.config["quick_replies"] = ["ok", "later"]
        await self.notifier.setup(self.s)
        self.assertEqual(self.notifier.quick_replies, ["ok", "later"])

    async def test_set_and_get_quick_replies(self):
        self.assertEqual(await n.set_quick_replies(self.s, ["a", "b"]), 0xCF)
        self.assertEqual(await n.get_quick_replies(self.s), ["a", "b"])
        self.assertEqual(await n.get_quick_replies(self.s), ["a", "b"])  # the second query has its own sequence
        self.assertEqual([t.get_int(2) for t in self.band.of(0x34, 1) if t.get_int(1) == 2], [1, 2, 3])

    async def test_set_replies_defaults_to_the_config_list(self):
        self.assertEqual(await self.notifier.set_replies(self.s, ["red", "yes"]), 0xCF)
        self.assertEqual(self.band.stored, ["red", "yes"])
        await self.notifier.set_replies(self.s)
        self.assertEqual(self.band.stored, n.QUICK_REPLIES)
        self.s.config["quick_replies"] = ["ok"]
        await self.notifier.set_replies(self.s)
        self.assertEqual(self.band.stored, ["ok"])

    async def test_a_silent_band_does_not_fail_setup(self):
        self.band.mute = {(2, 5), (2, 4), (0x34, 1)}
        await self.notifier.setup(self.s)
        logs = self.s.transport.logs
        self.assertIn("EVT notify no reply to 02/05", logs)
        self.assertIn("EVT notify no reply to 02/04", logs)
        self.assertTrue(any("quick replies failed: no ack of the quick-reply update" in line for line in logs))
        self.assertEqual((self.notifier.capabilities, self.notifier.content_limit), (None, 500))
        self.assertEqual(self.s._waiters, [])

    async def test_an_error_reply_is_logged(self):
        self.band.reject = {(2, 8)}
        await self.notifier.setup(self.s)
        self.assertIn("EVT notify 02/08 rejected, 7F=186A4", self.s.transport.logs)

    async def test_send(self):
        self.band.constraints = CONSTRAINTS.replace(bytes.fromhex("140201F4"), bytes.fromhex("140200C8"))
        await self.notifier.setup(self.s)
        with mock.patch("band.services.notifications.time.time", return_value=1700000000.9):
            nid = await self.notifier.send(self.s, "Band Bridge test", "x" * 250)
            nid2 = await self.notifier.send(self.s, "Second", "hello", reply=False, source_app="com.whatsapp",
                                            category="")
        self.assertEqual(nid2, nid + 1)
        self.assertTrue(1 < nid < 10100)
        key = f"0|org.telegram.messenger|{nid}||0"
        self.assertEqual(self.notifier.sent, {key: nid, f"0|com.whatsapp|{nid2}||0": nid2})

        first, second = self.band.of(2, 1)
        body = first.get_tlv(0x84).get_tlv(0x8C).get_all(0x8D)[2]
        self.assertEqual(TLV.parse(body).get_str(0x10), "x" * 187 + "...")  # limit 200 - 13
        self.assertEqual(first.get_int(1) + 1, second.get_int(1))
        self.assertEqual(first.get_str(0x18), key)
        self.assertEqual((first.get_str(0x19), first.get_int(0x1A), first.get_int(0x15)), (key, nid, 1700000000))
        self.assertEqual((second.get_str(0x18), second.get_str(0x11), 0x12 in second), ("", "com.whatsapp", False))

    async def test_send_and_remove_survive_a_missing_reply(self):
        self.band.mute = {(2, 1), (2, 6)}
        nid = await self.notifier.send(self.s, "t", "b")
        self.assertTrue(await self.notifier.remove(self.s, nid))
        self.assertIn("EVT notify no reply to 02/01", self.s.transport.logs)

    async def test_sent_is_capped_at_the_oldest(self):
        ids = [await self.notifier.send(self.s, "t", "b") for _ in range(n.KEEP + 3)]
        self.assertEqual(list(self.notifier.sent.values()), ids[3:])

    async def test_remove(self):
        nid = await self.notifier.send(self.s, "t", "b")
        self.assertTrue(await self.notifier.remove(self.s, nid))
        key = f"0|org.telegram.messenger|{nid}||0"
        self.assertEqual(self.band.of(2, 6)[0].items, [
            (1, b"\x7f"), (2, b"org.telegram.messenger"), (3, key.encode()), (4, nid.to_bytes(4, "big")), (5, b""),
            (6, b"msg")])
        self.assertFalse(await self.notifier.remove(self.s, 99999999))
        self.assertEqual(len(self.band.of(2, 6)), 1)

    async def test_reply_event_reaches_on_reply_through_the_session(self):
        nid = await self.notifier.send(self.s, "t", "b")
        key = f"0|org.telegram.messenger|{nid}||0"
        self.s.transport.push(2, 0x10, reply_event("yes".encode("utf-16-be"), key=key).tlv)
        await self.until(lambda: self.replies)
        self.assertEqual(self.replies, [(nid, "yes", key)])
        self.assertEqual(self.band.of(2, 0x10), [])  # a notification reply is not acked

    async def test_sms_reply_is_acked(self):
        await self.notifier.setup(self.s)
        self.s.transport.push(2, 0x10, reply_event("ok".encode("utf-16-be"), key="+1555", type=1).tlv)
        await self.until(lambda: self.band.of(2, 0x10))
        self.assertEqual(self.band.of(2, 0x10)[0].items, [(7, b"\xff")])
        self.assertEqual(self.replies, [(None, "ok", "+1555")])


if __name__ == "__main__":
    unittest.main()
