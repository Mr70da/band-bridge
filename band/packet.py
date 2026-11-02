# Ported from Gadgetbridge (AGPL-3.0): HuaweiPacket.java, HuaweiTLV.java
import binascii
from dataclasses import dataclass

from band import crypto

MAGIC = 0x5A
NON_TLV = {(0x0A, 0x05), (0x28, 0x06), (0x2C, 0x05), (0x1C, 0x05)}  # raw bytes, not TLV
_MISSING = object()


def crc16(data: bytes) -> int:
    return binascii.crc_hqx(data, 0)  # CRC-16/XMODEM: poly 0x1021, init 0, no reflection


def varint_encode(n: int) -> bytes:
    out = bytearray([n & 0x7F])
    n >>= 7
    while n:
        out.insert(0, (n & 0x7F) | 0x80)
        n >>= 7
    return bytes(out)


def varint_decode(buf: bytes, pos: int) -> tuple[int, int]:
    n = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        b = buf[pos]
        pos += 1
        n = (n << 7) | (b & 0x7F)
        if not b & 0x80:
            return n, pos


class TLV:
    """Ordered (tag, bytes) list; duplicate tags are kept."""

    def __init__(self):
        self.items: list[tuple[int, bytes]] = []

    def put(self, tag: int, value=None):
        if value is None:
            raw = b""
        elif isinstance(value, (bytes, bytearray)):
            raw = bytes(value)
        elif isinstance(value, bool):  # before int: bool is an int subclass
            raw = b"\x01" if value else b"\x00"
        elif isinstance(value, str):
            raw = value.encode("utf-8")
        elif isinstance(value, TLV):
            raw = value.serialize()
        else:
            raise TypeError(f"tag {tag:02X}: {type(value).__name__} has no wire width, use put_u8/u16/u32/u64")
        self.items.append((tag, raw))
        return self

    def put_u8(self, tag, n):
        return self.put(tag, n.to_bytes(1, "big"))

    def put_u16(self, tag, n):
        return self.put(tag, n.to_bytes(2, "big"))

    def put_u32(self, tag, n):
        return self.put(tag, n.to_bytes(4, "big"))

    def put_u64(self, tag, n):
        return self.put(tag, n.to_bytes(8, "big"))

    def serialize(self) -> bytes:
        return b"".join(bytes([t]) + varint_encode(len(v)) + v for t, v in self.items)

    @classmethod
    def parse(cls, data: bytes) -> "TLV":
        tlv, pos, end = cls(), 0, len(data)
        while pos < end:
            tag = data[pos]
            pos += 1
            if pos == end and tag == 0:  # Gadgetbridge: a lone 00 at the very end is padding left by decryption
                break
            size, pos = varint_decode(data, pos)
            if size > end - pos:
                raise ValueError(f"tag {tag:02X}: length {size} overruns the {end - pos} bytes left")
            tlv.items.append((tag, bytes(data[pos:pos + size])))
            pos += size
        return tlv

    def get(self, tag: int, default=_MISSING) -> bytes:
        for t, v in self.items:
            if t == tag:
                return v
        if default is _MISSING:
            raise KeyError(f"missing tag {tag:02X}")
        return default

    def get_int(self, tag: int, default=_MISSING) -> int:
        if tag not in self and default is not _MISSING:
            return default
        return int.from_bytes(self.get(tag), "big")

    def get_str(self, tag: int) -> str:
        return self.get(tag).decode("utf-8").rstrip("\x00")

    def get_tlv(self, tag: int) -> "TLV":
        return TLV.parse(self.get(tag))

    def get_all(self, tag: int) -> list[bytes]:
        return [v for t, v in self.items if t == tag]

    def __contains__(self, tag: int) -> bool:
        return any(t == tag for t, _ in self.items)

    def __repr__(self) -> str:
        return " - ".join(f"{{tag: {t:X} - Value: {v.hex().upper()}}}" for t, v in self.items) or "Empty"


# Transport encryption (protocol.md section 7): the whole TLV body becomes 7C=01, 7D=iv, 7E=ciphertext.
# shortcut: CBC needs the caller's IV; the counter IV scheme of ParamsProvider.getIv is not ported
# because Band 11 (deviceSupportType 4) always uses GCM with a random 16-byte IV.
def encrypt_body(plain: bytes, key: bytes, gcm: bool = True, iv: bytes | None = None) -> bytes:
    if iv is None:
        if not gcm:
            raise ValueError("CBC needs an explicit iv")
        iv = crypto.random_bytes(16)
    encrypt = crypto.aes_gcm_encrypt if gcm else crypto.aes_cbc_encrypt
    return TLV().put_u8(0x7C, 1).put(0x7D, iv).put(0x7E, encrypt(key, iv, plain)).serialize()


def decrypt_body(tlv: TLV, key: bytes, gcm: bool = True) -> TLV:
    decrypt = crypto.aes_gcm_decrypt if gcm else crypto.aes_cbc_decrypt
    return TLV.parse(decrypt(key, tlv.get(0x7D), tlv.get(0x7E)))


def _seal(content: bytes) -> bytes:
    """5A | LEN | content | CRC16. LEN counts the content only."""
    raw = bytes([MAGIC]) + len(content).to_bytes(2, "big") + content
    return raw + crc16(raw).to_bytes(2, "big")


def build_frames(service: int, command: int, body: bytes, slice_size: int = 0x3FE) -> list[bytes]:
    """body is the serialized TLV. Unsliced if it fits in slice_size, else SLICE 01, 02..., 03."""
    if slice_size < 10:
        raise ValueError(f"slice_size {slice_size} is too small")
    room = slice_size - 7  # per slice: 5A + LEN(2) + SLICE + FLAG + CRC(2)
    count = -(-(len(body) + 2) // room)
    if count <= 1:
        return [_seal(bytes([0, service, command]) + body)]
    frames, pos = [], 0
    for i in range(count):
        take = min(slice_size, len(body) - pos + 7) - 7  # Gadgetbridge sizing, so the vectors match
        head = bytes([3 if i == count - 1 else 1 if i == 0 else 2, i & 0xFF])
        if i == 0:
            head += bytes([service, command])
            take -= 2
        frames.append(_seal(head + body[pos:pos + take]))
        pos += take
    return frames


@dataclass
class Message:
    service: int
    command: int
    body: bytes  # raw bytes after SVC CMD
    tlv: TLV | None  # None for the NON_TLV pairs


class Reassembler:
    def __init__(self):
        self._sliced = bytearray()  # SVC CMD + data of the slices seen so far

    def feed(self, frame: bytes) -> Message | None:
        """frame is one complete raw frame (see transport.FrameAssembler). None until a sliced message ends."""
        if frame[0] != MAGIC:
            raise ValueError(f"bad magic {frame[0]:02X}")
        if len(frame) != 5 + int.from_bytes(frame[1:3], "big"):
            raise ValueError(f"LEN {int.from_bytes(frame[1:3], 'big')} does not match a {len(frame)}-byte frame")
        want, got = crc16(frame[:-2]), int.from_bytes(frame[-2:], "big")
        if want != got:
            raise ValueError(f"CRC mismatch: computed {want:04X}, frame has {got:04X}")
        kind = frame[3]
        if kind in (1, 2, 3):
            if kind == 1:
                self._sliced.clear()
            self._sliced += frame[5:-2]  # skip the FLAG byte
            if kind != 3:
                return None
            data, self._sliced = bytes(self._sliced), bytearray()
        else:
            data = frame[4:-2]
        if len(data) < 2:
            raise ValueError("frame has no SVC CMD")
        service, command = data[0], data[1]
        tlv = None if (service, command) in NON_TLV else TLV.parse(data[2:])
        return Message(service, command, data[2:], tlv)
