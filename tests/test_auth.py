import asyncio
import hashlib
import json
import unittest
from unittest import mock

from band import auth, crypto
from band.auth import AuthError, parse_response, pin_key, run_hichain, step_json, step_tlv
from band.packet import TLV, Reassembler, build_frames
from tests.fake_band import FakeBand
from tests.gb6012_hichain import RX, TX

# Debug lines of the same log: op 1 sessionKey (line 354), op 2 sessionKey (686), final transport key (728).
OP1_SESSION_KEY = bytes.fromhex("9F4E4F9969A798C3E51DB82E09BA322FE4732DC2A4026984C3544A9A60161934")
FINAL_KEY = bytes.fromhex("4846CCF256F5525E1CA9A7AC7E3306259E70F90E592EC040E91D1DA05416D7B0")
LABELS = ["op 1 step 1", "op 1 step 2", "op 1 step 3", "op 1 step 4", "op 2 step 1", "op 2 step 2", "op 2 final"]


run = asyncio.run


def parse(entry) -> TLV:
    return Reassembler().feed(bytes.fromhex(entry[1])).tlv


def payload(entry) -> dict:
    return json.loads(parse(entry).get_str(1))["payload"]


ANDROID_ID = bytes.fromhex(payload(TX[0])["peerAuthId"]).decode()


def log_auth_token() -> bytes:
    """Op 1 step 3 of the log: decrypt the challenge with the logged session key, then the token with it."""
    sent, got = payload(TX[2]), payload(RX[2])
    challenge = crypto.aes_gcm_decrypt(
        OP1_SESSION_KEY, bytes.fromhex(sent["nonce"]), bytes.fromhex(sent["encData"]), b"hichain_iso_exchange")
    return crypto.aes_gcm_decrypt(
        OP1_SESSION_KEY, bytes.fromhex(got["nonce"]), bytes.fromhex(got["encAuthToken"]), challenge)


class LogFramesTest(unittest.TestCase):
    def test_every_logged_tx_frame_rebuilds_byte_for_byte(self):
        for label, entry in zip(LABELS, TX):
            with self.subTest(label):
                frame = bytes.fromhex(entry[1])
                tlv = parse(entry)
                env = json.loads(tlv.get_str(1))
                op, request_id = tlv.get_int(2), tlv.get_int(3)
                fields = {k: v for k, v in env["payload"].items() if k != "version"}
                text = step_json(op, env["message"], fields, ANDROID_ID, request_id, "isDeviceLevel" in env)
                self.assertEqual(text, tlv.get_str(1))
                self.assertEqual(build_frames(1, 0x28, step_tlv(text, op, request_id).serialize()), [frame])

    def test_op1_session_key_decrypts_steps_3_and_4(self):
        self.assertEqual(len(log_auth_token()), 32)
        sent = payload(TX[3])
        result = crypto.aes_gcm_decrypt(
            OP1_SESSION_KEY, bytes.fromhex(sent["nonce"]), bytes.fromhex(sent["encResult"]), b"hichain_iso_result")
        self.assertEqual(result, bytes(4))

    def test_op2_replayed_against_the_log(self):
        sent = [parse(e) for e in TX[4:]]
        replies = iter([parse(e) for e in RX[4:]])
        expected = iter(sent)
        first = payload(TX[4])
        randoms = iter([bytes.fromhex(first["seed"]), bytes.fromhex(first["isoSalt"]),
                        bytes.fromhex(payload(TX[6])["nonce"])])

        async def request(tlv, timeout):
            self.assertEqual(tlv.serialize(), next(expected).serialize())
            return next(replies)

        def rand(n):
            value = next(randoms)
            self.assertEqual(len(value), n)
            return value

        request_id = sent[0].get_int(3)
        with mock.patch.object(auth.time, "time_ns", return_value=request_id * 1_000_000):
            key = run(run_hichain(request, 2, log_auth_token(), ANDROID_ID, rand=rand))
        self.assertEqual(key, FINAL_KEY)

    def test_parse_response_routes_the_logged_replies(self):
        kinds = [parse_response(parse(e))[0] for e in RX]
        self.assertEqual(kinds, [1, 2, 3, "done", 1, 2, "done"])


class FlowTest(unittest.TestCase):
    PIN = bytes(range(16))
    TOKEN = bytes(range(100, 132))

    def test_pair_then_bind(self):
        band = FakeBand(self.PIN, self.TOKEN)
        token = run(run_hichain(band, 1, pin_key(self.PIN), ANDROID_ID))
        self.assertEqual(token, self.TOKEN)
        secret = run(run_hichain(band, 2, token, ANDROID_ID))
        self.assertEqual(secret, band.secret_key)
        self.assertEqual(len(secret), 32)
        self.assertEqual(band.messages, [(1, 1), (1, 2), (1, 3), (1, 4), (2, 0x11), (2, 0x12), (2, 0x13)])

    def test_wrong_pin_fails_at_the_step_1_token_check(self):
        band = FakeBand(self.PIN, self.TOKEN)
        with self.assertRaisesRegex(AuthError, "op 1 step 1.*wrong PIN"):
            run(run_hichain(band, 1, pin_key(bytes(16)), ANDROID_ID))
        self.assertEqual(len(band.messages), 1)

    def test_wrong_stored_token_fails_at_step_1(self):
        band = FakeBand(self.PIN, self.TOKEN)
        with self.assertRaisesRegex(AuthError, "op 2 step 1.*stored key"):
            run(run_hichain(band, 2, bytes(32), ANDROID_ID))

    def test_error_code_in_payload(self):
        body = {"authForm": 0, "payload": {"errorCode": 7, "version": {}}, "message": 0x8001}
        reply = TLV().put(1, json.dumps(body)).put_u8(2, 1).put_u8(4, 0)

        async def request(tlv, timeout):
            return reply

        with self.assertRaisesRegex(AuthError, "op 1 step 1.*errorCode 7"):
            run(run_hichain(request, 1, pin_key(self.PIN), ANDROID_ID))

    def test_band_ending_early_is_an_error(self):
        async def request(tlv, timeout):
            return TLV().put(1, "com.huawei.health").put_u8(4, 1)

        with self.assertRaisesRegex(AuthError, "op 2 step 1.*'done'"):
            run(run_hichain(request, 2, self.TOKEN, ANDROID_ID))

    def test_timeout_names_the_step(self):
        async def request(tlv, timeout):
            raise TimeoutError("no reply to 01/28")

        with self.assertRaisesRegex(TimeoutError, "op 1 step 1: no reply in 30 s"):
            run(run_hichain(request, 1, pin_key(self.PIN), ANDROID_ID, step1_timeout=30))

    def test_pin_key_is_sha256_of_uppercase_hex(self):
        self.assertEqual(pin_key(bytes.fromhex("0a0B")), hashlib.sha256(b"0A0B").digest())


if __name__ == "__main__":
    unittest.main()
