import json

from band import crypto
from band.auth import pin_key
from band.packet import TLV


def hx(data: bytes) -> str:
    return data.hex().upper()


class FakeBand:
    """The band's side of HiChain, written from its point of view (own id, own randomness)."""

    def __init__(self, pin: bytes, auth_token: bytes):
        self.id = hx(crypto.random_bytes(32)).encode()  # 64 ASCII bytes, like the real one
        self.keys = {1: pin_key(pin), 2: auth_token}
        self.auth_token = auth_token
        self.messages = []
        self.secret_key = None

    async def __call__(self, tlv, timeout):
        env = json.loads(tlv.get_str(1))
        op, step = tlv.get_int(2), env["message"] & 0xF
        self.messages.append((op, env["message"]))
        if step == 1:
            reply = self.step1(op, env["payload"])
        elif step == 2:
            reply = self.step2(op, env["payload"])
        elif step == 3 and op == 1:
            reply = self.step3(env["payload"])
        else:
            self.final(env["payload"])
            return TLV().put(1, "com.huawei.health").put_u8(2, op).put(3, tlv.get(3)).put_u8(4, 1).put_u8(5, 0)
        body = {"authForm": 0, "payload": reply, "message": 0x8000 | env["message"]}
        return TLV().put(1, json.dumps(body)).put_u8(2, op).put(3, tlv.get(3)).put_u8(4, 0).put_u8(5, 0)

    def step1(self, op, p):
        self.rand_c, self.id_c = bytes.fromhex(p["isoSalt"]), bytes.fromhex(p["peerAuthId"])
        self.rand_b = crypto.random_bytes(16)
        self.psk = crypto.hmac_sha256(self.keys[op], bytes.fromhex(p["seed"]))
        token = crypto.hmac_sha256(self.psk, self.rand_b + self.rand_c + self.id_c + self.id)
        return {"isoSalt": hx(self.rand_b), "token": hx(token), "peerAuthId": hx(self.id), "peerUserType": 0}

    def step2(self, op, p):
        want = crypto.hmac_sha256(self.psk, self.rand_c + self.rand_b + self.id + self.id_c)
        if bytes.fromhex(p["token"]) != want:
            raise AssertionError("client token is wrong")
        self.salt = self.rand_c + self.rand_b
        self.session_key = crypto.hkdf_sha256(self.psk, self.salt, b"hichain_iso_session_key")
        return {"peerAuthId": hx(self.id), "returnCodeMac": hx(crypto.hmac_sha256(self.psk, bytes(4)))}

    def step3(self, p):
        challenge = crypto.aes_gcm_decrypt(
            self.session_key, bytes.fromhex(p["nonce"]), bytes.fromhex(p["encData"]), b"hichain_iso_exchange")
        nonce = crypto.random_bytes(12)
        return {"nonce": hx(nonce),
                "encAuthToken": hx(crypto.aes_gcm_encrypt(self.session_key, nonce, self.auth_token, challenge))}

    def final(self, p):
        result = crypto.aes_gcm_decrypt(
            self.session_key, bytes.fromhex(p["nonce"]), bytes.fromhex(p["encResult"]), b"hichain_iso_result")
        if result != bytes(4):
            raise AssertionError("client sent a bad result")
        self.secret_key = crypto.hkdf_sha256(self.session_key, self.salt, b"hichain_return_key")
