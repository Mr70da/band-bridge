# Ported from Gadgetbridge (AGPL-3.0): GetHiChainRequest.java, DeviceConfig.java (HiChain)
import hmac
import json
import time

from cryptography.exceptions import InvalidTag

from band import crypto
from band.packet import TLV

GROUP_ID = "7B0BC0CBCE474F6C238D9661C63400B797B166EA7849B3A370FC73A9A236E989"
STEP_TIMEOUT = 10


class AuthError(Exception):
    pass


def pin_key(pin: bytes) -> bytes:
    return crypto.sha256(pin.hex().upper().encode())


def _hex(data: bytes) -> str:
    return data.hex().upper()


def step_json(op: int, message: int, payload: dict, android_id: str, request_id: int, device_level: bool) -> str:
    """Key order is the wire order (protocol.md 6.3). device_level adds "isDeviceLevel":false (op 2, steps 1-2)."""
    env = {
        "authForm": 0,
        "payload": {"version": {"minVersion": "1.0.0", "currentVersion": "2.0.16"}, **payload},
        "groupAndModuleVersion": "2.0.1",
        "message": message,
    }
    if op == 1:
        env |= {
            "requestId": str(request_id), "groupId": GROUP_ID, "groupName": "health_group_name", "groupOp": 2,
            "groupType": 256, "peerDeviceId": android_id, "connDeviceId": android_id,
            "appId": "com.huawei.health", "ownerName": "",
        }
    if device_level:
        env["isDeviceLevel"] = False
    return json.dumps(env, separators=(",", ":"))


def step_tlv(json_str: str, op: int, request_id: int) -> TLV:
    return TLV().put(1, json_str).put_u8(2, op).put_u64(3, request_id)


def parse_response(tlv: TLV) -> tuple[int | str, dict]:
    """(1|2|3, payload) for the JSON steps, ("done", {}) for the last reply. Raises AuthError on errorCode."""
    if tlv.get_int(4, 0) != 0:
        return "done", {}
    try:
        payload = json.loads(tlv.get_str(1))["payload"]
    except (KeyError, ValueError) as e:
        raise AuthError(f"unreadable reply ({e!r})") from None
    if payload.get("errorCode", 0) != 0:
        raise AuthError(f"band returned errorCode {payload['errorCode']}")
    for name, kind in (("isoSalt", 1), ("returnCodeMac", 2), ("encAuthToken", 3)):
        if name in payload:
            return kind, payload
    raise AuthError(f"unknown reply, payload keys {sorted(payload)}")


async def run_hichain(request, op, key, android_id, step1_timeout=STEP_TIMEOUT, rand=crypto.random_bytes) -> bytes:
    """request(tlv, timeout) -> reply TLV. key: op 1 pin_key(pin), op 2 the stored auth_token.
    Returns the auth_token (op 1) or the transport secret_key (op 2)."""
    request_id = time.time_ns() // 1_000_000
    id_self = android_id.encode()
    last = 4 if op == 1 else 3  # op 2 has no step 3, so its final message is 0x13

    async def step(n, payload, want, fields=(), timeout=STEP_TIMEOUT):
        where = f"HiChain op {op} step {n}"
        j = step_json(op, n | (0x10 if op == 2 else 0), payload, android_id, request_id, op == 2 and n <= 2)
        try:
            reply = await request(step_tlv(j, op, request_id), timeout)
        except TimeoutError:
            raise TimeoutError(f"{where}: no reply in {timeout} s") from None
        try:
            kind, got = parse_response(reply)
        except AuthError as e:
            raise AuthError(f"{where}: {e}") from None
        if kind != want:
            raise AuthError(f"{where}: got reply {kind!r}, wanted {want!r}")
        try:
            return {f: bytes.fromhex(got[f]) for f in fields}
        except (KeyError, ValueError, TypeError):
            raise AuthError(f"{where}: reply lacks valid {', '.join(fields)}") from None

    def check(where, got, want):
        if not hmac.compare_digest(got, want):
            raise AuthError(f"HiChain op {op} {where}")

    seed, rand_self = rand(32), rand(16)
    first = {"isoSalt": _hex(rand_self), "peerAuthId": _hex(id_self), "operationCode": op, "seed": _hex(seed),
             "peerUserType": 0}
    if op == 2:
        first |= {"pkgName": "com.huawei.devicegroupmanage", "serviceType": GROUP_ID, "keyLength": 32}
    r = await step(1, first, 1, ("isoSalt", "peerAuthId", "token"), step1_timeout)
    rand_peer, id_peer = r["isoSalt"], r["peerAuthId"]
    psk = crypto.hmac_sha256(key, seed)
    check("step 1: band token does not match, " + ("wrong PIN?" if op == 1 else "stored key not accepted"),
          r["token"], crypto.hmac_sha256(psk, rand_peer + rand_self + id_self + id_peer))

    token = crypto.hmac_sha256(psk, rand_self + rand_peer + id_peer + id_self)
    r = await step(2, {"peerAuthId": _hex(id_self), "token": _hex(token)}, 2, ("returnCodeMac",))
    check("step 2: returnCodeMac does not match", r["returnCodeMac"], crypto.hmac_sha256(psk, bytes(4)))

    salt = rand_self + rand_peer
    session_key = crypto.hkdf_sha256(psk, salt, b"hichain_iso_session_key")
    auth_token = b""
    if op == 1:
        nonce, challenge = rand(12), rand(16)
        enc = crypto.aes_gcm_encrypt(session_key, nonce, challenge, b"hichain_iso_exchange")
        r = await step(3, {"nonce": _hex(nonce), "encData": _hex(enc)}, 3, ("nonce", "encAuthToken"))
        try:
            auth_token = crypto.aes_gcm_decrypt(session_key, r["nonce"], r["encAuthToken"], challenge)
        except InvalidTag:
            raise AuthError("HiChain op 1 step 3: cannot decrypt the auth token") from None

    nonce = rand(12)
    enc = crypto.aes_gcm_encrypt(session_key, nonce, bytes(4), b"hichain_iso_result")
    await step(last, {"nonce": _hex(nonce), "encResult": _hex(enc), "operationCode": op}, "done")
    return auth_token if op == 1 else crypto.hkdf_sha256(session_key, salt, b"hichain_return_key")
