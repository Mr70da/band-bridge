import asyncio
import os
from datetime import datetime

from bleak import BleakClient

SERVICE = "0000fe86-0000-1000-8000-00805f9b34fb"
WRITE = "0000fe01-0000-1000-8000-00805f9b34fb"
NOTIFY = "0000fe02-0000-1000-8000-00805f9b34fb"


def chunks(frame: bytes, size: int) -> list[bytes]:
    return [frame[i:i + size] for i in range(0, len(frame), size)]


class FrameAssembler:
    """Rebuilds frames (5A | LEN u16 BE | LEN bytes | CRC 2) from notification chunks.

    Does not check the CRC. Garbage between frames is skipped and kept in `dropped`.
    """

    def __init__(self):
        self.buf = bytearray()
        self.dropped: list[bytes] = []

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        out = []
        while self.buf:
            if self.buf[0] != 0x5A:
                end = self.buf.find(0x5A)
                if end < 0:
                    end = len(self.buf)
                self.dropped.append(bytes(self.buf[:end]))
                del self.buf[:end]
                continue
            if len(self.buf) < 3:
                break
            total = 3 + int.from_bytes(self.buf[1:3], "big") + 2
            if len(self.buf) < total:
                break
            out.append(bytes(self.buf[:total]))
            del self.buf[:total]
        return out


class Transport:
    def __init__(self, address, log_dir="logs", chunk_size=20, on_frame=None):
        self.address = address
        self.chunk_size = chunk_size
        self.on_frame = on_frame
        self.frames: asyncio.Queue[bytes] = asyncio.Queue()
        self.client = None
        self._asm = FrameAssembler()
        self._lock = asyncio.Lock()
        os.makedirs(log_dir, exist_ok=True)
        self.log_path = os.path.join(log_dir, datetime.now().strftime("ble-%Y%m%d-%H%M%S.log"))
        self._log = open(self.log_path, "a", encoding="utf-8")

    def log(self, kind, text=""):
        if self._log.closed:  # the disconnected callback can fire after disconnect() returns
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self._log.write(f"{ts} {kind} {text}\n")
        self._log.flush()  # we debug from these logs, so never lose the tail

    async def connect(self):
        # No pair(): the band's own HiChain replaces OS-level security.
        # use_cached_services=False: the band was reset, so an old Windows GATT cache could be stale.
        self.client = BleakClient(
            self.address,
            disconnected_callback=lambda _: self.log("EVT", "disconnected"),
            winrt=dict(use_cached_services=False),
        )
        try:
            await self.client.connect()
            self.log("EVT", f"connected mtu={self.client.mtu_size}")
            for svc in self.client.services:
                self.log("EVT", f"service {svc.uuid}")
                for ch in svc.characteristics:
                    self.log("EVT", f" char {ch.uuid} {','.join(ch.properties)}")
            if self.client.services.get_service(SERVICE) is None:
                raise RuntimeError(f"{self.address} has no Huawei service {SERVICE}; wrong device?")
            await self.client.start_notify(NOTIFY, self._on_notify)
        except Exception as e:
            self.log("EVT", f"error {e!r}")
            await self.disconnect()
            raise

    async def disconnect(self):
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        self._log.close()

    async def send(self, frame: bytes):
        async with self._lock:  # never interleave chunks of two frames
            for chunk in chunks(frame, self.chunk_size):
                self.log("TX", chunk.hex().upper())
                await self.client.write_gatt_char(WRITE, chunk, response=False)

    def _on_notify(self, _char, data: bytearray):
        self.log("RX", data.hex().upper())
        frames = self._asm.feed(data)
        while self._asm.dropped:
            self.log("DROP", self._asm.dropped.pop(0).hex().upper())
        for frame in frames:
            self.log("FRAME", frame.hex().upper())
            self.frames.put_nowait(frame)
            if self.on_frame:
                self.on_frame(frame)

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()
