# Ported from Gadgetbridge (AGPL-3.0): HuaweiSupportProvider.java (fetchActivityData, downloadTruSleepData,
# downloadDictTrueSleepData, downloadStressData), FitnessData.java, GetFitnessTotalsRequest.java,
# Get{Step,Sleep}Data{Count,}Request.java, HuaweiFileDownloadManager.java, FileDownloadService2C.java,
# HuaweiSequenceDataFileParser.java, HuaweiSequenceDataParserBase.java, HuaweiTrueSleepSequenceDataParser.java,
# HuaweiStressParser.java
import asyncio
import logging
import struct
import time
from datetime import datetime

from band import crypto
from band.packet import TLV

FITNESS, FILES = 0x07, 0x2C
TOTALS, STEP_COUNT, STEP_DATA, SLEEP_COUNT, SLEEP_DATA = 0x03, 0x0A, 0x0B, 0x0C, 0x0D
INIT, INFO, BLOCK_REQ, BLOCK, COMPLETE = 1, 3, 4, 5, 6  # service 0x2C commands (02 = hash, unused: we never verify)
SLEEP_FILE, RRI_FILE, SEQUENCE_FILE = 0x0F, 0x10, 0x16  # 2C file types; 0x0E sleep_state is the old TruSleep pair
SLEEP_DETAILS = 700013  # dict id of the sequence file that holds sleep
BED_TIME_CAP = 199  # expand capability: sleep file starts at bed time instead of fall-asleep time
FIRST_DAYS, INTERVAL, DELAY, OVERLAP = 7, 900, 5, 3600  # first-run days, seconds between syncs, after connect, re-fetch
TIMEOUT = 10  # seconds to wait for the band's answer to one request
LEGACY_STAGES = {6: "light", 7: "deep"}  # 07/0D type byte (RawTypes LIGHT_SLEEP, DEEP_SLEEP)
FILE_STAGES = {1: "light", 2: "rem", 3: "deep", 4: "awake", 5: "light"}  # sequence file details; 5 is a nap

log = logging.getLogger("band.fitness")


def _short(b: bytes) -> int:
    return int.from_bytes(b, "big", signed=True)


# --- parsers: 07/0A..0D and 07/03 ---------------------------------------------------------------------------------

def parse_count(tlv: TLV) -> int:
    """07/0A, 07/0C reply -> number of chunks the band holds. 81 { 02 = count }"""
    return tlv.get_tlv(0x81).get_int(2)


def count_request(start: int, end: int) -> TLV:
    return TLV().put(0x81).put_u32(3, start).put_u32(4, end)


def data_request(index: int) -> TLV:
    return TLV().put(0x81, TLV().put_u16(2, index))


def parse_totals(tlv: TLV) -> tuple[int, int, int]:
    """07/03 reply -> (steps, calories, distance m), summed over the 83 containers. 05 steps u32, 06 calories u16,
    07 distance u32."""
    steps = cal = dist = 0
    for item in tlv.get_tlv(0x81).get_all(0x83):
        c = TLV.parse(item)
        steps += c.get_int(5, 0)
        cal += c.get_int(6, 0)
        dist += c.get_int(7, 0)
    return steps, cal, dist


def parse_step_record(data: bytes):
    """One 84/06 blob -> (steps, calories, distance, heart_rate, spo2), each None when absent or -1. Two feature
    bitmaps: the first (bit 7 = a second bitmap follows) selects 16-bit values 01 ?, 02 steps, 04 calories,
    08 distance, 10 ?, 40 and 20 are single bytes (20 ?, 40 heart rate); the second selects bytes (01 SpO2,
    02 resting heart rate, others ?)."""
    if not data:
        raise ValueError("no feature bitmap")
    pos, bm1, bm2 = 1, data[0], 0
    if bm1 & 0x80:
        if len(data) < 2:
            raise ValueError("no second feature bitmap")
        bm2, pos = data[1], 2
    got = {}
    for bit in (1, 2, 4, 8, 0x10, 0x20, 0x40):
        if not bm1 & bit:
            continue
        width = 1 if bit in (0x20, 0x40) else 2
        if pos + width > len(data):
            raise ValueError("data too short for the selected features")
        got[bit] = _short(data[pos:pos + width])
        pos += width
    for bit in (1, 2, 4, 8, 0x10, 0x20, 0x40, 0x80):
        if not bm2 & bit:
            continue
        if pos >= len(data):
            raise ValueError("data too short for the selected features")
        got[("b2", bit)] = _short(data[pos:pos + 1])
        pos += 1

    def val(k):
        v = got.get(k)
        return None if v is None or v == -1 else v
    hr, spo = val(0x40), val(("b2", 1))
    return (val(2), val(4), val(8), hr & 0xFF if hr is not None else None, spo & 0xFF if spo is not None else None)


def parse_steps(tlv: TLV) -> list[tuple]:
    """07/0B reply -> [(ts, steps, calories, distance, heart_rate|None, spo2|None)]. 81 { 03 = base ts u32,
    84 { 05 = minute offset, 06 = record } ... }. A record that does not parse is skipped."""
    box = tlv.get_tlv(0x81)
    base, rows = box.get_int(3), []
    for item in box.get_all(0x84):
        c = TLV.parse(item)
        try:
            steps, cal, dist, hr, spo = parse_step_record(c.get(6))
        except ValueError as e:
            log.debug("step record skipped: %s", e)
            continue
        rows.append((base + 60 * int.from_bytes(c.get(5), "big", signed=True), steps or 0, cal or 0, dist or 0,
                     hr if hr else None, spo))
    return rows


def parse_legacy_sleep(tlv: TLV) -> list[tuple[int, int, str]]:
    """07/0D reply -> [(start, end, stage)]. 81 { 83 { 04 = type, 05 = u32 start + u16 minutes } ... }"""
    rows = []
    for item in tlv.get_tlv(0x81).get_all(0x83):
        c = TLV.parse(item)
        stamp, stage = c.get(5), LEGACY_STAGES.get(c.get_int(4))
        start = int.from_bytes(stamp[:4], "big")
        end = start + int.from_bytes(stamp[4:6], "big") * 60
        if stage and end > start:
            rows.append((start, end, stage))
    return rows


# --- file download, service 0x2C ----------------------------------------------------------------------------------

def init_request(name: str, ftype: int, start: int, end: int, dict_id: int = 0) -> TLV:
    tlv = TLV().put(1, name).put_u8(2, ftype).put_u32(5, start).put_u32(6, end)
    return tlv.put_u32(0x0C, dict_id) if dict_id > 0 and ftype == SEQUENCE_FILE else tlv


def info_request(file_id: int) -> TLV:
    return TLV().put_u8(1, file_id).put(2).put(3).put(4).put(5)


def block_request(file_id: int, offset: int, size: int, dict_id: int = 0) -> TLV:
    tlv = TLV().put_u8(1, file_id).put_u32(2, offset).put_u32(3, size)
    # Gadgetbridge compares the band's file id (not the type) with 0x16 here
    return tlv.put_u32(4, dict_id) if dict_id > 0 and file_id == SEQUENCE_FILE else tlv


def parse_block(body: bytes, key: bytes | None) -> tuple[int, int, bytes]:
    """2C/05 body (raw, not TLV) -> (file id, offset, data). Encrypted when it starts 7C 01 01: then 7D = iv and
    7E = AES-GCM ciphertext of file id | offset u32 | 1 unknown byte | data."""
    if body[:3] == b"\x7c\x01\x01":
        if not key:
            raise ValueError("encrypted block but no session key")
        t = TLV.parse(body)
        body = crypto.aes_gcm_decrypt(key, t.get(0x7D), t.get(0x7E))
    if len(body) < 6:
        raise ValueError("block too short")
    return body[0], int.from_bytes(body[1:5], "big"), body[6:]


# --- file parsers -------------------------------------------------------------------------------------------------

def _read_value(kind: int, v: bytes, default=-1) -> int:
    """HuaweiSequenceDataParserBase.getValueAsLong: kinds 1, 2, 4 = int/byte/short, 3 = long, 6 = double."""
    if not v or len(v) > 8 or v == b"\xbf\x80\x00\x00":  # BF800000 is the float -1 the band stores as an int
        return default
    n = int.from_bytes(v, "big")
    if kind in (1, 2, 4):
        return n if n < 1 << 31 else n - (1 << 32) if len(v) == 4 else n
    if kind == 3:
        return n
    if kind == 6 and len(v) == 8:
        return int(struct.unpack(">d", v)[0])
    return default


def parse_sequence_file(data: bytes) -> list[dict]:
    """A sequence_data file -> [{start, end, version, summary_type, summary, details}]. Header: size u32, dict id
    u32, type u16, version u16, 20 spare bytes. Record: start u32, end u32, version u8, summary type u8,
    summary length u16, details length u32, 16 spare, summary, details."""
    if len(data) < 32:
        raise ValueError(f"sequence file too short ({len(data)} bytes)")
    pos, out = 32, []
    while len(data) - pos >= 32:
        start, end, ver, stype, slen, dlen = struct.unpack_from(">IIBBHI", data, pos)
        pos += 32
        summary = data[pos:pos + slen] if ver == 1 else b""
        pos += slen if ver == 1 else 0
        details = b""
        if 0 < dlen <= 20 * 1025 * 1024:
            details = data[pos:pos + dlen]
            pos += dlen
        out.append(dict(start=start, end=end, version=ver, summary_type=stype, summary=summary, details=details))
    return out


def parse_summary(summary: bytes) -> dict[int, int]:
    """TLV summary (type 2): containers inside containers, each leaf 03 = dict id, 04 = value kind, 05 = value."""
    out = {}
    for top in TLV.parse(summary).items:
        if top[0] < 0x80:
            continue
        for mid in TLV.parse(top[1]).items:
            if mid[0] < 0x80:
                continue
            leaf = TLV.parse(mid[1])
            try:
                out[int.from_bytes(leaf.get(3), "big")] = _read_value(int.from_bytes(leaf.get(4), "big"), leaf.get(5))
            except KeyError:
                pass
    return out


def parse_sleep_details(details: bytes, t0: int) -> list[tuple[int, int, str]]:
    """Little-endian records of duration seconds i32, stage u8, 3 spare bytes, laid end to end from t0 ->
    [(start, end, stage)], neighbours of one stage merged. Invalid records (as Gadgetbridge) are skipped."""
    if len(details) % 4:
        raise ValueError("sleep details length is not a multiple of 4")
    rows, pos = [], 0
    while len(details) - pos >= 8:
        dur, stage = struct.unpack_from("<iB", details, pos)
        pos += 8
        if dur == 0 or stage == 0 or dur % 60 or dur // 60 > 1440:
            continue
        name = FILE_STAGES.get(stage)
        if name:
            if rows and rows[-1][2] == name and rows[-1][1] == t0:
                rows[-1] = (rows[-1][0], t0 + dur, name)
            else:
                rows.append((t0, t0 + dur, name))
        t0 += dur
    return rows


def parse_sleep_file(data: bytes, bed_time: bool) -> list[tuple[int, int, str]]:
    """sleep sequence file -> stage rows of every night in it. A night starts at its bed time when the band
    supports that and marked the record valid, else at the fall-asleep time (HuaweiTrueSleepSequenceDataParser)."""
    rows = []
    for rec in parse_sequence_file(data):
        if rec["version"] != 1 or rec["summary_type"] != 2 or not rec["details"]:
            continue
        s = parse_summary(rec["summary"])
        asleep, bed, valid = s.get(700013686, -1), s.get(700013298, -1), s.get(700013786, -1)
        t0 = bed if bed_time and valid != -1 else asleep
        if t0 > 0:
            rows += parse_sleep_details(rec["details"], t0 // 60 * 60)
    return rows


def parse_stress_file(data: bytes) -> list[tuple[int, int]]:
    """rrisqi_data.bin -> [(end ts, score)]. Header: size u64, bitmap u16, version u16, padding to 48; version 3
    has 66-byte entries: start u32, end u32, algorithm u32, score, level, 4 flag bytes, 12 floats."""
    if len(data) < 48:
        raise ValueError("stress file too short")
    version = struct.unpack_from(">H", data, 10)[0]
    if version != 3:
        raise ValueError(f"unknown stress file version {version}")
    rows, pos = [], 48
    while len(data) - pos >= 66:
        start, end, _algo, score = struct.unpack_from(">IIIb", data, pos)
        pos += 66
        if start and end and 1 <= score <= 99:
            rows.append((end, score))
    return rows


# --- the service --------------------------------------------------------------------------------------------------

class FitnessService:
    """Pulls the band's activity into a FitnessStore: samples (+ SpO2) from 07/0A-0B, today's totals from 07/03, sleep
    from the 0x2C sequence file (and 07/0C-0D when the band answers), stress from the 0x2C RRI file. Every step
    stands alone: a failure is logged and the next step still runs."""

    def __init__(self, store, config=None):
        self.store, self.config = store, config or {}
        self._session = None
        self._blocks = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._tasks = set()
        self._legacy = True
        self.report: dict[str, str] = {}

    def _log(self, text):
        log.info(text)
        if self._session is not None and getattr(self._session, "transport", None):
            self._session.transport.log("EVT", f"fitness: {text}")

    def _live(self):
        return self._session is not None and not self._session.disconnected.is_set()

    def attach(self, session):
        self._session = session
        self._blocks = asyncio.Queue()
        self._legacy = True  # 07/0C-0D unanswered once: not asked again on this connection

    async def setup(self, session):
        """Per connection: start a sync shortly after, in the background, so the connect is never delayed."""
        self.attach(session)
        if self.config.get("enabled", True):
            self._spawn(self._delayed(session))

    def _spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _delayed(self, session):
        await asyncio.sleep(self.config.get("delay", DELAY))
        if self._session is session and self._live():
            await self.sync(session)

    async def run(self):
        while True:
            try:
                await asyncio.sleep(self.config.get("interval", INTERVAL))
                if self.config.get("enabled", True) and self._live():
                    await self.sync(self._session)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log(f"sync failed: {e!r}")

    def on_message(self, msg):
        if (msg.service, msg.command) == (FILES, BLOCK):
            self._blocks.put_nowait(msg)

    # --- sync ---

    async def sync(self, session, since=None):
        """One pass. since: unix seconds to fetch from (default: the last sync minus an hour, else FIRST_DAYS days).
        Returns {step: "ok ..." | "failed ..."}; last_sync moves forward only when the samples step worked."""
        async with self._lock:
            self._session = session
            now = int(time.time())
            if since is None:
                last = self.store.meta("last_sync")
                since = int(last) - OVERLAP if last else now - self.config.get("days", FIRST_DAYS) * 86400
            self.report = {}
            for name, step in (("sleep (07/0C-0D)", self._legacy_sleep), ("samples", self._samples),
                               ("totals", self._totals), ("sleep (file)", self._sleep_file),
                               ("stress", self._stress)):
                try:
                    self.report[name] = "ok " + str(await step(session, since, now))
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.report[name] = f"failed {e!r}"
                    self._log(f"{name} failed: {e!r}")
            if self.report["samples"].startswith("ok"):
                self.store.set_meta("last_sync", now)
            return self.report

    async def _ask(self, session, cmd, tlv, svc=FITNESS):
        t = (await session.request(svc, cmd, tlv, timeout=TIMEOUT)).tlv
        if 0x7F in t and t.get_int(0x7F) != 0x186A0 and 0x81 not in t and 1 not in t:
            raise RuntimeError(f"{svc:02X}/{cmd:02X} refused, 7F={t.get_int(0x7F):X}")
        return t

    async def _chunks(self, session, count_cmd, data_cmd, since, now):
        """Yield every data reply of one count/data pair (Gadgetbridge indexes the chunks 0..count-1)."""
        count = parse_count(await self._ask(session, count_cmd, count_request(since, now)))
        for i in range(count):
            yield await self._ask(session, data_cmd, data_request(i))

    async def _samples(self, session, since, now):
        rows = []
        async for t in self._chunks(session, STEP_COUNT, STEP_DATA, since, now):
            rows += parse_steps(t)
        self.store.add_samples([r[:5] for r in rows])
        spo = [(r[0], r[5]) for r in rows if r[5]]
        if spo:
            self.store.add_spo2(spo)
        return f"{len(rows)} samples, {len(spo)} SpO2"

    async def _totals(self, session, since, now):
        steps, cal, dist = parse_totals(await self._ask(session, TOTALS, TLV().put(1)))
        self.store.set_totals(datetime.fromtimestamp(now).date().isoformat(), steps, cal, dist, now)
        return f"{steps} steps, {cal} kcal, {dist} m"

    async def _legacy_sleep(self, session, since, now):
        if not self._legacy:
            return "skipped, the band did not answer before"
        rows = []
        try:
            async for t in self._chunks(session, SLEEP_COUNT, SLEEP_DATA, since, now):
                rows += parse_legacy_sleep(t)
        except Exception:
            self._legacy = False
            raise
        if rows:
            self.store.add_sleep(rows)
        return f"{len(rows)} segments"

    async def _sleep_file(self, session, since, now):
        data = await self.download(session, "sequence_data", SEQUENCE_FILE, since - 86400, now, SLEEP_DETAILS)
        caps = bytes.fromhex(session.config.get("expand_caps", ""))
        bed = BED_TIME_CAP // 8 < len(caps) and bool(caps[BED_TIME_CAP // 8] >> (BED_TIME_CAP % 8) & 1)
        rows = parse_sleep_file(data, bed) if data else []
        if rows:
            self.store.add_sleep(rows)
        return f"{len(data)} bytes, {len(rows)} segments"

    async def _stress(self, session, since, now):
        data = await self.download(session, "rrisqi_data.bin", RRI_FILE, since, now)
        rows = parse_stress_file(data) if data else []
        if rows:
            self.store.add_stress(rows)
        return f"{len(data)} bytes, {len(rows)} readings"

    async def download(self, session, name, ftype, start, end, dict_id=0) -> bytes:
        """The 2C file download: init -> info -> block requests (each answered by 05 messages) -> complete."""
        while not self._blocks.empty():
            self._blocks.get_nowait()
        t = await self._ask(session, INIT, init_request(name, ftype, start, end, dict_id), FILES)
        if t.get_str(1) != name:
            raise RuntimeError(f"init answered for file {t.get_str(1)!r}, wanted {name!r}")
        fid, size = t.get_int(3), t.get_int(4)
        if size == 0:
            await session.send(FILES, COMPLETE, TLV().put_u8(1, fid).put_u8(2, 1))
            return b""
        if size >= 1 << 31:
            raise RuntimeError(f"band reports file size {size:#x} (it has no data)")
        info = await self._ask(session, INFO, info_request(fid), FILES)
        wait, max_block, plain = max(info.get_int(2, 0), 5), info.get_int(4), bool(info.get_int(5, 0))
        if not max_block:
            raise RuntimeError("band gave no block size")
        buf = bytearray()
        while len(buf) < size:
            want = min(size - len(buf), max_block)
            await session.send(FILES, BLOCK_REQ, block_request(fid, len(buf), want, dict_id), encrypt=not plain)
            got = 0
            while got < want:
                msg = await asyncio.wait_for(self._blocks.get(), wait)
                bid, _off, data = parse_block(msg.body, session.secret_key)
                if bid != fid or not data:
                    raise RuntimeError(f"unexpected block for file {bid}, {len(data)} bytes")
                buf += data
                got += len(data)
        await session.send(FILES, COMPLETE, TLV().put_u8(1, fid).put_u8(2, 1))
        return bytes(buf[:size])
