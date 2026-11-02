import json
import unittest

from cryptography.exceptions import InvalidTag

from band import crypto
from band.packet import (
    TLV, Reassembler, build_frames, crc16, decrypt_body, encrypt_body, varint_decode, varint_encode,
)

ZERO_KEY = bytes(16)
ZERO_IV = bytes(16)

LINK_PARAMS_REQUEST = "5A000B0001010100020003000400F13B"
PIN_REQUEST = "5A000500012C0100AF3B"

# Real Band 11 frames from the Gadgetbridge issue #6012 log (reference/issue-logs/gb6012.log).
LINK_PARAMS_REPLY = (  # RX, line 72
    "5A0035000101010102020203FE030200140402000005120001F4458C8644C8F429E7EF6CFF8BC44EA3070104"
    "0901000D01000F01011001001A41"
)
SECURITY_REQUEST = (  # TX, line 81
    "5A00390001330101040201010520414233453438383634353835433036314634463439424531383036333246"
    "363203010104010006000704413036339119"
)
SECURITY_REPLY = (  # RX, line 90
    "5A00840001330101040201020301010401000540353442384136353445423638313039343234323035354444"
    "4537394432453641444246354242333842434638334641353334394436363136413037434541423606120000"
    "000000000000000000000000000000000700080100091246383A33423A37453A30363A32323A4246000A0400"
    "000000CAA5"
)
PIN_REPLY = (  # RX, line 107
    "5A006700012C01503AA913CB83468E182B9982B02EE2EE84419D37282A0D17BCD863AD79984A990A278E4822"
    "03019961C5D6D974619A5050A0B6A2F6A8C94B91FCEAAFA14455374411CC3E14AB2ADFA2BACFAFED54387E58"
    "02101AFCB929A224CF68B465419805B655C8241F"
)
HICHAIN_STEP1 = (  # TX, line 121
    "5A02B200012801851F7B2261757468466F726D223A302C227061796C6F6164223A7B2276657273696F6E223A"
    "7B226D696E56657273696F6E223A22312E302E30222C2263757272656E7456657273696F6E223A22322E302E"
    "3136227D2C2269736F53616C74223A2232414238324236413636454230423942333043413534313245373644"
    "46383832222C2270656572417574684964223A22343134323333343533343338333833363334333533383335"
    "34333330333633313436333434363334333934323435333133383330333633333332343633363332222C226F"
    "7065726174696F6E436F6465223A312C2273656564223A223631314339314632303245444441374542344337"
    "4343443531414445354637444537413343333533453036413433304236393231333942373945343632424543"
    "222C22706565725573657254797065223A307D2C2267726F7570416E644D6F64756C6556657273696F6E223A"
    "22322E302E31222C226D657373616765223A312C22726571756573744964223A223137373633333733363730"
    "3934222C2267726F75704964223A223742304243304342434534373446364332333844393636314336333430"
    "3042373937423136364541373834394233413337304643373341394132333645393839222C2267726F75704E"
    "616D65223A226865616C74685F67726F75705F6E616D65222C2267726F75704F70223A322C2267726F757054"
    "797065223A3235362C22706565724465766963654964223A2241423345343838363435383543303631463446"
    "34394245313830363332463632222C22636F6E6E4465766963654964223A2241423345343838363435383543"
    "30363146344634394245313830363332463632222C226170704964223A22636F6D2E6875617765692E686561"
    "6C7468222C226F776E65724E616D65223A22227D02010103080000019D95F53036D6DE"
)
BATTERY_REQUEST = (  # TX, line 55
    "5A002C0001087C01017D10894A861464D49A9D2029645DD7E0E3137E12CE70641AD3A0DB464A75EB62252CC9"
    "91B8E4A5B6"
)
# Our own Windows capture (logs/ble-20261102-180348.log): the whole reply in one notification, mtu 0x0202.
WINDOWS_LINK_PARAMS_REPLY = (
    "5A0035000101010102020203FE03020202040200000512000171C2B83875843AE313853F973B6275900701040901"
    "000D01000F0101100100DEA6"
)

# Frames Gadgetbridge's own tests build or parse (TestHuaweiPacket), unsliced and unencrypted.
JAVA_FRAMES = [
    "5A000700 7F7F 01020304 40B6",
    "5A000900 0102 03 04 05060708 A4F0",
    "5A000900 2BB4 010102020101 03C0",  # adaptive volume on
    "5A000900 2BB4 010102020100 13E1",
    "5A000900 2BB4 010102030100 24D1",
    "5A000900 2BB4 010102030101 34F0",
    "5A000900 2BB4 010102030102 0493",
    "5A000C00 2BB4 010102020101 030101 758A",
    "5A000C00 2BB4 010102020100 030101 033E",
    "5A000600 2B87 010101 3EE4",  # extra media volume
    "5A000600 2B87 010100 2EC5",
    "5A000500 2B88 0100 9182",
    "5A000600 2B88 010101 EA0A",
    "5A000700 2B5D 010200 00 6F63",  # find headphones
    "5A000700 2B5D 010200 01 7F42",
    "5A000700 2B5D 010201 00 5C52",
    "5A000700 2B5D 010201 01 4C73",
    "5A000700 2B5D 020200 00 F4BF",
    "5A000700 2B5E 020201 01 397D",
    "5A000600 2B6C 010101 A411",  # low latency
    "5A000600 2B6C 010100 B430",
    "5A000900 2B6C 7F04000186A0 A497",
]


# TestHuaweiPacket.testUnencryptedSlicedSerialize: sliceSize 0x10, svc 01 cmd 02, tags 1-4 = u32 0.
SLICED_FRAMES = [
    "5A000B0100010201040000000002CC98",
    "5A000B0201040000000003040000FAD3",
    "5A000A0302000004040000000037CA",
]
SLICED_BODY = TLV().put_u32(1, 0).put_u32(2, 0).put_u32(3, 0).put_u32(4, 0).serialize()


class Crc16Test(unittest.TestCase):
    def test_real_frames(self):
        for h in (LINK_PARAMS_REQUEST, PIN_REQUEST):
            frame = bytes.fromhex(h)
            self.assertEqual(crc16(frame[:-2]), int.from_bytes(frame[-2:], "big"))

    def test_xmodem_check_value(self):
        self.assertEqual(crc16(b"123456789"), 0x31C3)


class VarIntTest(unittest.TestCase):
    CASES = [
        (0, "00"), (5, "05"), (17, "11"), (127, "7F"), (128, "8100"),  # TestVarInt: zero, single, 0x80
        (460, "834C"), (500, "8374"), (16383, "FF7F"), (16384, "818000"),
    ]

    def test_encode(self):
        for n, h in self.CASES:
            self.assertEqual(varint_encode(n), bytes.fromhex(h), n)

    def test_decode(self):
        for n, h in self.CASES:
            self.assertEqual(varint_decode(bytes.fromhex(h), 0), (n, len(h) // 2), n)

    def test_decode_at_offset(self):  # TestVarInt.testOffset
        self.assertEqual(varint_decode(bytes([0x00, 0x83, 0x4C]), 1), (460, 3))

    def test_truncated(self):
        with self.assertRaises(ValueError):
            varint_decode(bytes([0x83]), 0)


class TlvTest(unittest.TestCase):
    def test_serialize_empty(self):
        self.assertEqual(TLV().serialize(), b"")
        self.assertEqual(repr(TLV()), "Empty")

    def test_serialize_keeps_duplicates_in_order(self):
        self.assertEqual(TLV().put(1).put(1, b"\x42").serialize(), bytes([1, 0, 1, 1, 0x42]))

    def test_put_empty_and_none(self):
        self.assertEqual(TLV().put(1).items, [(1, b"")])
        self.assertEqual(TLV().put(1, None).items, [(1, b"")])

    def test_put_bytes(self):
        self.assertEqual(TLV().put(1, b"\x01\x02\x03").items, [(1, b"\x01\x02\x03")])
        self.assertEqual(TLV().put(1, bytearray(b"\x01")).items, [(1, b"\x01")])

    def test_put_bool(self):
        self.assertEqual(TLV().put(1, True).put(2, False).items, [(1, b"\x01"), (2, b"\x00")])

    def test_put_fixed_widths(self):
        self.assertEqual(TLV().put_u8(1, 0x13).items, [(1, b"\x13")])
        self.assertEqual(TLV().put_u16(1, 0xCAFE).items, [(1, b"\xCA\xFE")])
        self.assertEqual(TLV().put_u32(1, 0xDEADBEEF).items, [(1, b"\xDE\xAD\xBE\xEF")])
        self.assertEqual(TLV().put_u64(1, 0x0102030405060708).items, [(1, bytes(range(1, 9)))])

    def test_put_out_of_range(self):
        with self.assertRaises(OverflowError):
            TLV().put_u8(1, 256)
        with self.assertRaises(OverflowError):
            TLV().put_u16(1, -1)

    def test_put_str(self):
        self.assertEqual(TLV().put(1, "Hello world!").items, [(1, b"Hello world!")])
        self.assertEqual(TLV().put(1, "é").items, [(1, b"\xC3\xA9")])

    def test_put_tlv(self):
        inner = TLV().put_u16(1, 0x1337)
        self.assertEqual(TLV().put(1, inner).items, [(1, bytes([0x01, 0x02, 0x13, 0x37]))])

    def test_put_rejects_plain_int(self):
        with self.assertRaises(TypeError):
            TLV().put(1, 5)

    def test_put_chains(self):
        t = TLV()
        self.assertIs(t.put(1), t)
        self.assertIs(t.put_u8(2, 0), t)

    def test_parse(self):
        self.assertEqual(TLV.parse(b"").items, [])
        self.assertEqual(TLV.parse(bytes([1, 1, 1])).items, [(1, b"\x01")])
        self.assertEqual(TLV.parse(bytes.fromhex("0104DEADBEEF")).items, [(1, b"\xDE\xAD\xBE\xEF")])
        self.assertEqual(TLV.parse(bytes([1, 0, 1, 0])).items, [(1, b""), (1, b"")])

    def test_parse_two_byte_length(self):
        value = bytes(range(200))
        self.assertEqual(TLV.parse(bytes([1, 0x81, 0x48]) + value).items, [(1, value)])

    def test_parse_malformed(self):
        for bad in (bytes([1, 1]), bytes([1, 5, 0, 0]), bytes([1]), bytes([1, 0x81])):
            with self.assertRaises(ValueError, msg=bad.hex()):
                TLV.parse(bad)

    def test_parse_ignores_single_trailing_zero(self):
        self.assertEqual(TLV.parse(bytes([1, 1, 5, 0])).items, [(1, b"\x05")])
        self.assertEqual(TLV.parse(b"\x00").items, [])
        self.assertEqual(TLV.parse(bytes([0, 1, 5])).items, [(0, b"\x05")])  # a 00 tag elsewhere is a tag

    def test_get(self):
        t = TLV().put(1, b"\x01\x02\x03").put(1, b"x").put(2)
        self.assertEqual(t.get(1), b"\x01\x02\x03")  # first match
        self.assertEqual(t.get(2), b"")
        self.assertEqual(t.get(3, None), None)
        with self.assertRaises(KeyError):
            t.get(3)

    def test_get_int(self):
        t = TLV().put(1, b"\x04").put_u16(2, 0xCAFE).put_u32(3, 0xDEADBEEF).put_u64(4, 2 ** 40).put(5)
        self.assertEqual([t.get_int(i) for i in (1, 2, 3, 4)], [4, 0xCAFE, 0xDEADBEEF, 2 ** 40])
        self.assertEqual(t.get_int(5), 0)
        self.assertEqual(t.get_int(9, 7), 7)
        with self.assertRaises(KeyError):
            t.get_int(9)

    def test_get_str(self):
        self.assertEqual(TLV().put(1, b"Hello world!").get_str(1), "Hello world!")
        self.assertEqual(TLV().put(1, b"F8:3B\x00").get_str(1), "F8:3B")

    def test_get_tlv(self):
        t = TLV().put(1, TLV().put(1))
        self.assertEqual(t.get_tlv(1).items, [(1, b"")])

    def test_get_all(self):
        t = TLV().put(1, b"a").put(2, b"b").put(1, b"c")
        self.assertEqual(t.get_all(1), [b"a", b"c"])
        self.assertEqual(t.get_all(9), [])

    def test_contains(self):
        t = TLV().put(1)
        self.assertIn(1, t)
        self.assertNotIn(2, t)

    def test_repr(self):
        t = TLV().put(1, b"\x01\x02").put(2, b"\x03\x04")
        self.assertEqual(repr(t), "{tag: 1 - Value: 0102} - {tag: 2 - Value: 0304}")  # TestHuaweiTLV.testToString
        self.assertEqual(repr(TLV().put(0x7C, b"\xAB")), "{tag: 7C - Value: AB}")

    def test_parse_serialize_round_trip(self):
        body = bytes.fromhex(LINK_PARAMS_REPLY)[6:-2]
        self.assertEqual(TLV.parse(body).serialize(), body)


class EncryptBodyTest(unittest.TestCase):
    PLAIN = TLV().put_u16(1, 0xCAFE).serialize()
    CIPHER = bytes.fromhex("0EA001BB1EDACB098320407D971BF6D0")  # TestHuaweiTLV.testEncrypt, CBC, zero key and IV

    def test_cbc_encrypt_vector(self):
        sealed = TLV.parse(encrypt_body(self.PLAIN, ZERO_KEY, gcm=False, iv=ZERO_IV))
        self.assertEqual(sealed.items, [(0x7C, b"\x01"), (0x7D, ZERO_IV), (0x7E, self.CIPHER)])

    def test_cbc_decrypt_vector(self):  # TestHuaweiTLV.testDecrypt
        sealed = TLV().put_u8(0x7C, 1).put(0x7D, ZERO_IV).put(0x7E, self.CIPHER)
        self.assertEqual(decrypt_body(sealed, ZERO_KEY, gcm=False).items, [(1, b"\xCA\xFE")])

    def test_cbc_needs_iv(self):
        with self.assertRaises(ValueError):
            encrypt_body(self.PLAIN, ZERO_KEY, gcm=False)

    def test_gcm_round_trip_with_random_iv(self):
        key = crypto.random_bytes(32)
        sealed = TLV.parse(encrypt_body(self.PLAIN, key))
        self.assertEqual([t for t, _ in sealed.items], [0x7C, 0x7D, 0x7E])
        self.assertEqual(sealed.get_int(0x7C), 1)
        self.assertEqual(len(sealed.get(0x7D)), 16)
        self.assertEqual(len(sealed.get(0x7E)), len(self.PLAIN) + 16)
        self.assertEqual(decrypt_body(sealed, key).items, [(1, b"\xCA\xFE")])
        self.assertNotEqual(sealed.get(0x7D), TLV.parse(encrypt_body(self.PLAIN, key)).get(0x7D))

    def test_gcm_explicit_iv_and_wrong_key(self):
        key, iv = crypto.random_bytes(32), crypto.random_bytes(16)
        sealed = TLV.parse(encrypt_body(self.PLAIN, key, iv=iv))
        self.assertEqual(sealed.get(0x7D), iv)
        with self.assertRaises(InvalidTag):
            decrypt_body(sealed, crypto.random_bytes(32))

    def test_battery_request_shape(self):  # plaintext "01 00" gives the 18-byte 7E of the real request
        sealed = TLV.parse(encrypt_body(TLV().put(1).serialize(), crypto.random_bytes(32)))
        self.assertEqual(len(sealed.get(0x7E)), 18)

    def test_decrypted_trailing_zero_is_ignored(self):
        key, iv = crypto.random_bytes(32), crypto.random_bytes(16)
        sealed = TLV.parse(encrypt_body(bytes([1, 1, 5, 0]), key, iv=iv))
        self.assertEqual(decrypt_body(sealed, key).items, [(1, b"\x05")])


class BuildFramesTest(unittest.TestCase):
    def test_link_params_request(self):
        body = TLV().put(1).put(2).put(3).put(4).serialize()
        self.assertEqual(build_frames(0x01, 0x01, body), [bytes.fromhex(LINK_PARAMS_REQUEST)])

    def test_pin_request(self):
        self.assertEqual(build_frames(0x01, 0x2C, TLV().put(1).serialize()), [bytes.fromhex(PIN_REQUEST)])

    def test_security_negotiation_request_matches_real_one(self):
        androidid = "AB3E48864585C061F4F49BE180632F62"
        tlv = TLV().put_u8(1, 4).put_u8(2, 1).put(5, androidid).put_u8(3, 1).put_u8(4, 0).put(6).put(7, "A063")
        self.assertEqual(build_frames(0x01, 0x33, tlv.serialize()), [bytes.fromhex(SECURITY_REQUEST)])

    def test_gadgetbridge_unsliced_frames(self):  # TestHuaweiPacket serialize vectors
        for spaced in JAVA_FRAMES:
            frame = bytes.fromhex(spaced.replace(" ", ""))
            self.assertEqual(build_frames(frame[4], frame[5], frame[6:-2]), [frame], spaced)

    def test_gadgetbridge_encrypted_unsliced(self):  # testEncryptedUnslicedSerialize, CBC with zero key and IV
        body = encrypt_body(TLV().put_u32(3, 0x05060708).serialize(), ZERO_KEY, gcm=False, iv=ZERO_IV)
        expected = bytes.fromhex(
            "5A002A00" "0102" "7C0101" "7D10" + "00" * 16 + "7E10" "3B89FC79D9055EED5235FE16A08A4D53" "93C7"
        )
        self.assertEqual(build_frames(0x01, 0x02, body), [expected])

    def test_gadgetbridge_sliced(self):
        frames = build_frames(0x01, 0x02, SLICED_BODY, slice_size=0x10)
        self.assertEqual(frames, [bytes.fromhex(h) for h in SLICED_FRAMES])

    def test_largest_unsliced_frame(self):
        # Gadgetbridge slices as soon as SVC CMD + body exceed sliceSize - 7, so an unsliced frame tops out at sliceSize - 1.
        value = bytes(0x3FE - 7 - 5)  # tag(1) + two-byte length(2) + value + SVC CMD(2) fills the room exactly
        frames = build_frames(1, 2, TLV().put(1, value).serialize())
        self.assertEqual([len(f) for f in frames], [0x3FD])
        self.assertEqual(len(build_frames(1, 2, TLV().put(1, value + b"x").serialize())), 2)

    def test_slice_size_too_small(self):
        with self.assertRaises(ValueError):
            build_frames(1, 2, b"", slice_size=9)


class ReassemblerTest(unittest.TestCase):
    def feed(self, h):
        return Reassembler().feed(bytes.fromhex(h))

    def test_link_params_reply(self):
        msg = self.feed(LINK_PARAMS_REPLY)
        self.assertEqual((msg.service, msg.command), (0x01, 0x01))
        t = msg.tlv
        self.assertEqual(t.get(1), b"\x02")
        self.assertEqual(t.get(2), bytes.fromhex("03FE"))
        self.assertEqual(t.get(3), bytes.fromhex("0014"))
        self.assertEqual(t.get(7), b"\x04")
        self.assertEqual(t.get_int(4), 0)
        self.assertEqual(t.get_int(0x0C, 0), 0)  # encryptMethod is absent on Band 11
        self.assertEqual(t.get(5)[1], 0x01)  # authVersion
        self.assertEqual(t.get(5)[2:], bytes.fromhex("F4458C8644C8F429E7EF6CFF8BC44EA3"))  # 16-byte nonce
        self.assertEqual(msg.body, bytes.fromhex(LINK_PARAMS_REPLY)[6:-2])

    def test_link_params_reply_from_windows(self):
        t = self.feed(WINDOWS_LINK_PARAMS_REPLY).tlv
        self.assertEqual(t.get_int(3), 0x0202)
        self.assertEqual(t.get(5)[1], 0x01)
        self.assertEqual(len(t.get(5)[2:]), 16)

    def test_security_negotiation_request(self):
        msg = self.feed(SECURITY_REQUEST)
        self.assertEqual((msg.service, msg.command), (0x01, 0x33))
        t = msg.tlv
        self.assertEqual(t.get(1), b"\x04")
        self.assertEqual(len(t.get(5)), 32)
        self.assertTrue(t.get(5).isascii() and t.get_str(5).isalnum())
        self.assertEqual(t.get_str(7), "A063")
        self.assertIn(6, t)

    def test_security_negotiation_reply(self):
        msg = self.feed(SECURITY_REPLY)
        self.assertEqual((msg.service, msg.command), (0x01, 0x33))
        t = msg.tlv
        self.assertEqual(t.get(1), b"\x04")
        self.assertEqual(t.get(2), b"\x02")
        self.assertEqual(t.get_str(9), "F8:3B:7E:06:22:BF")
        self.assertEqual(len(t.get(5)), 64)
        self.assertEqual(t.get_int(0x7F, -1), -1)

    def test_pin_reply_decrypts(self):
        msg = self.feed(PIN_REPLY)
        self.assertEqual((msg.service, msg.command), (0x01, 0x2C))
        self.assertEqual(len(msg.tlv.get(1)), 80)
        self.assertEqual(len(msg.tlv.get(2)), 16)
        pin = crypto.decrypt_pin(msg.tlv.get(1), msg.tlv.get(2))  # valid PKCS7 only with the right key
        self.assertEqual(
            pin.hex().upper(),
            "11521141111048011089552288742565310751215341010613942066659034072315318434254551"
            "910211245415474858711270510021521336469021318336",
        )

    def test_hichain_step1_request(self):
        msg = self.feed(HICHAIN_STEP1)
        self.assertEqual((msg.service, msg.command), (0x01, 0x28))
        t = msg.tlv
        outer = json.loads(t.get(1))
        self.assertEqual(outer["message"], 1)
        self.assertEqual(outer["payload"]["operationCode"], 1)
        self.assertEqual(t.get(2), b"\x01")
        self.assertEqual(len(t.get(3)), 8)
        # the JSON peerAuthId is the hex of the android id we sent in the Security Negotiation request
        androidid = self.feed(SECURITY_REQUEST).tlv.get(5)
        self.assertEqual(bytes.fromhex(outer["payload"]["peerAuthId"]), androidid)

    def test_encrypted_battery_request(self):
        msg = self.feed(BATTERY_REQUEST)
        self.assertEqual((msg.service, msg.command), (0x01, 0x08))
        t = msg.tlv
        self.assertEqual(t.get(0x7C), b"\x01")
        self.assertEqual(len(t.get(0x7D)), 16)
        self.assertEqual(len(t.get(0x7E)), 18)

    def test_gadgetbridge_unencrypted_unknown_packet(self):  # testUnknownUnencryptedPacketParse
        msg = self.feed("5A000700" "7F7F" "01020304" "40B6")
        self.assertEqual((msg.service, msg.command), (0x7F, 0x7F))
        self.assertEqual(msg.tlv.items, [(1, b"\x03\x04")])

    def test_gadgetbridge_encrypted_packets(self):  # testUnknownEncryptedPacketParse / testKnownEncryptedPacketParse
        for frame, svc, cmd, expected in (
            ("5A002A00" "7F7F" "7C0101" "7D10" + "00" * 16 + "7E10" "9E40E1EA15F650808C4519D52ABB29B8" "D524",
             0x7F, 0x7F, [(1, b"\x03\x04")]),
            ("5A002A00" "0B01" "7C0101" "7D10" + "00" * 16 + "7E10" "2800996F2ACB623AE6542854F8AB5483" "F4F4",
             0x0B, 0x01, [(1, b"\x00")]),
        ):
            msg = self.feed(frame)
            self.assertEqual((msg.service, msg.command), (svc, cmd))
            self.assertEqual(decrypt_body(msg.tlv, ZERO_KEY, gcm=False).items, expected)

    def test_non_tlv_pair_keeps_raw_body(self):
        (frame,) = build_frames(0x0A, 0x05, b"\x01\x02\x03")
        msg = Reassembler().feed(frame)
        self.assertEqual((msg.service, msg.command, msg.body, msg.tlv), (0x0A, 0x05, b"\x01\x02\x03", None))

    def test_sliced_message(self):
        asm = Reassembler()
        slices = [bytes.fromhex(h) for h in SLICED_FRAMES]
        self.assertIsNone(asm.feed(slices[0]))
        self.assertIsNone(asm.feed(slices[1]))
        msg = asm.feed(slices[2])
        self.assertEqual((msg.service, msg.command), (0x01, 0x02))
        self.assertEqual(msg.body, SLICED_BODY)
        self.assertEqual([t for t, _ in msg.tlv.items], [1, 2, 3, 4])
        self.assertEqual(asm.feed(slices[0]), None)  # ready for the next message

    def test_build_then_reassemble_around_slice_boundaries(self):
        for slice_size in (0x3FE, 0xF4):
            for n in list(range(slice_size - 20, slice_size + 30)) + list(range(2 * slice_size - 20, 2 * slice_size + 30)):
                body = TLV().put(1, bytes(i % 251 for i in range(n))).serialize()
                frames = build_frames(0x05, 0x06, body, slice_size)
                self.assertTrue(all(len(f) <= slice_size for f in frames), (slice_size, n))
                asm = Reassembler()
                results = [asm.feed(f) for f in frames]
                self.assertEqual(results[:-1], [None] * (len(frames) - 1))
                self.assertEqual((results[-1].service, results[-1].command, results[-1].body), (5, 6, body))

    def test_new_first_slice_discards_an_unfinished_message(self):
        asm = Reassembler()
        first = build_frames(1, 2, bytes(100), slice_size=0x20)[0]
        asm.feed(first)
        frames = build_frames(3, 4, TLV().put(1, bytes(50)).serialize(), slice_size=0x20)
        results = [asm.feed(f) for f in frames]
        self.assertEqual(results[-1].body, TLV().put(1, bytes(50)).serialize())

    def test_java_frames_parse(self):
        for spaced in JAVA_FRAMES:
            frame = bytes.fromhex(spaced.replace(" ", ""))
            msg = Reassembler().feed(frame)
            self.assertEqual((msg.service, msg.command, msg.body), (frame[4], frame[5], frame[6:-2]), spaced)
            self.assertEqual(msg.tlv.serialize(), msg.body)

    def test_crc_mismatch(self):
        frame = bytearray.fromhex(LINK_PARAMS_REPLY)
        frame[10] ^= 0x01
        with self.assertRaises(ValueError) as ctx:
            Reassembler().feed(bytes(frame))
        self.assertIn("1A41", str(ctx.exception))  # the CRC the frame carries
        self.assertIn("CRC", str(ctx.exception))

    def test_bad_magic_and_length(self):
        good = bytes.fromhex(PIN_REQUEST)
        with self.assertRaises(ValueError):
            Reassembler().feed(b"\x5B" + good[1:])
        with self.assertRaises(ValueError):
            Reassembler().feed(good + b"\x00")

    def test_rebuilding_real_frames_is_byte_exact(self):
        for h in (LINK_PARAMS_REPLY, SECURITY_REQUEST, SECURITY_REPLY, PIN_REPLY, HICHAIN_STEP1, BATTERY_REQUEST):
            frame = bytes.fromhex(h)
            msg = Reassembler().feed(frame)
            self.assertEqual(build_frames(msg.service, msg.command, msg.body), [frame])


if __name__ == "__main__":
    unittest.main()
