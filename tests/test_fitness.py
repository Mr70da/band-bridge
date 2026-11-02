import asyncio
import struct
import time
import unittest
from datetime import datetime
from unittest import mock

from band import crypto
from band.fitness_store import FitnessStore
from band.packet import TLV, Message
from band.services import fitness as f
from band.services.fitness import FitnessService

KEY = bytes(range(32))
# Band 11 expand capabilities from the real gb6012.log (01/37 reply): bits 143 (dict sleep) and 199 (bed time) set
BAND11_CAPS = "FF7F014226C37442A0A6BC7222698A4B87AAA10D6EFDCC8A8F511901594DA48A0A42100000"


def msg(svc, cmd, tlv) -> Message:
    return Message(svc, cmd, tlv.serialize(), tlv)


def step_chunk(base, records) -> TLV:
    box = TLV().put_u16(2, 1).put_u32(3, base)
    for off, rec in records:
        box.put(0x84, TLV().put_u8(5, off).put(6, rec))
    return TLV().put(0x81, box)


def seq_file(records, dict_id=700013) -> bytes:
    out = struct.pack(">IIHH", 0, dict_id, 1, 1) + bytes(20)
    for start, end, summary, details in records:
        out += struct.pack(">IIBBHI", start, end, 1, 2, len(summary), len(details)) + bytes(16) + summary + details
    return out


def leaf(dict_id, kind, value: bytes) -> TLV:
    return TLV().put(3, dict_id.to_bytes(4, "big")).put_u8(4, kind).put(5, value)


def sleep_summary(asleep, bed, valid=1) -> bytes:
    body = TLV()
    for d, v in ((700013686, asleep), (700013298, bed), (700013786, valid)):
        body.put(0x82, leaf(d, 3, v.to_bytes(8, "big")))
    return TLV().put(0x81, body).serialize()


def details(*parts) -> bytes:
    return b"".join(struct.pack("<iB", dur, stage) + bytes(3) for dur, stage in parts)


def stress_file(entries) -> bytes:
    out = struct.pack(">QHH", 0, 0, 3).ljust(48, b"\0")
    for start, end, score in entries:
        out += struct.pack(">IIIbbbbbb", start, end, 1, score, 1, 0, 0, 0, 0) + bytes(48)
    return out


class Parsers(unittest.TestCase):
    def test_step_record_bitmaps(self):
        # 4E = steps, calories, distance, heart rate
        self.assertEqual(f.parse_step_record(bytes.fromhex("4E000A00050007" + "48")), (10, 5, 7, 72, None))
        # 82 01 = steps, then second bitmap with SpO2
        self.assertEqual(f.parse_step_record(bytes.fromhex("8201000A61")), (10, None, None, None, 97))
        # 0xFFFF steps and a 0xFF heart rate mean "not measured"
        self.assertEqual(f.parse_step_record(bytes.fromhex("42FFFFFF")), (None, None, None, None, None))
        self.assertEqual(f.parse_step_record(bytes([0x01, 0, 9])), (None, None, None, None, None))  # unknown bit 01

    def test_step_record_errors(self):
        for bad in (b"", b"\x82", b"\x4E\x00\x01"):
            with self.assertRaises(ValueError):
                f.parse_step_record(bad)

    def test_steps_chunk(self):
        t = step_chunk(1_700_000_000, [(0, bytes.fromhex("4E000A00050007" + "48")), (2, bytes.fromhex("8201000A61")),
                                       (3, b"")])
        self.assertEqual(f.parse_steps(t), [(1_700_000_000, 10, 5, 7, 72, None),
                                            (1_700_000_120, 10, 0, 0, None, 97)])  # the empty record is skipped

    def test_count_and_requests(self):
        self.assertEqual(f.parse_count(TLV().put(0x81, TLV().put_u16(2, 7))), 7)
        self.assertEqual(f.count_request(5, 9).items, [(0x81, b""), (3, b"\0\0\0\x05"), (4, b"\0\0\0\x09")])
        self.assertEqual(TLV.parse(f.data_request(3).get(0x81)).get_int(2), 3)

    def test_totals_sum_containers(self):
        t = TLV().put(0x81, TLV()
                      .put(0x83, TLV().put_u32(5, 1000).put_u16(6, 50).put_u32(7, 700))
                      .put(0x83, TLV().put_u32(5, 234).put_u16(6, 6).put_u32(7, 120)))
        self.assertEqual(f.parse_totals(t), (1234, 56, 820))

    def test_legacy_sleep(self):
        item = lambda typ, start, minutes: TLV().put_u8(4, typ).put(5, start.to_bytes(4, "big") + minutes.to_bytes(2, "big"))
        t = TLV().put(0x81, TLV().put_u16(2, 0).put(0x83, item(7, 1_700_000_000, 90)).put(0x83, item(6, 1_700_005_400, 30))
                      .put(0x83, item(9, 1_700_007_200, 5)))
        self.assertEqual(f.parse_legacy_sleep(t), [(1_700_000_000, 1_700_005_400, "deep"),
                                                   (1_700_005_400, 1_700_007_200, "light")])

    def test_requests_match_gadgetbridge_layout(self):
        # FileDownloadInit: 01 name, 02 type, 05 start, 06 end, 0C dict id (sequence data only)
        t = f.init_request("sequence_data", 0x16, 100, 200, 700013)
        self.assertEqual([k for k, _ in t.items], [1, 2, 5, 6, 0x0C])
        self.assertEqual(t.get_int(0x0C), 700013)
        self.assertEqual([k for k, _ in f.init_request("rrisqi_data.bin", 0x10, 1, 2, 5).items], [1, 2, 5, 6])
        self.assertEqual([k for k, _ in f.info_request(0x16).items], [1, 2, 3, 4, 5])
        self.assertEqual([k for k, _ in f.block_request(0x16, 0, 10, 700013).items], [1, 2, 3, 4])
        self.assertEqual([k for k, _ in f.block_request(0x05, 0, 10, 700013).items], [1, 2, 3])

    def test_block_plain_and_encrypted(self):
        plain = bytes([0x16]) + (0x20).to_bytes(4, "big") + b"\x00" + b"hello"
        self.assertEqual(f.parse_block(plain, KEY), (0x16, 0x20, b"hello"))
        iv = bytes(16)
        sealed = TLV().put_u8(0x7C, 1).put(0x7D, iv).put(0x7E, crypto.aes_gcm_encrypt(KEY, iv, plain)).serialize()
        self.assertEqual(f.parse_block(sealed, KEY), (0x16, 0x20, b"hello"))
        with self.assertRaises(ValueError):
            f.parse_block(sealed, None)

    def test_sleep_details_merge_and_skip(self):
        d = details((600, 1), (600, 1), (1800, 3), (0, 1), (90, 2), (60, 9), (300, 4))
        rows = f.parse_sleep_details(d, 1000)
        # 90 s is not a whole minute (skipped, no time), stage 9 is unknown (time passes), then awake
        self.assertEqual(rows, [(1000, 2200, "light"), (2200, 4000, "deep"), (4060, 4360, "awake")])

    def test_sleep_file_uses_bed_time_when_supported(self):
        rec = (1000, 9000, sleep_summary(asleep=1_700_000_100, bed=1_700_000_000), details((600, 3), (600, 1)))
        data = seq_file([rec])
        bed = f.parse_sleep_file(data, True)
        asleep = f.parse_sleep_file(data, False)
        self.assertEqual(bed[0][0], 1_700_000_000 // 60 * 60)
        self.assertEqual(asleep[0][0], 1_700_000_100 // 60 * 60)
        self.assertEqual([r[2] for r in bed], ["deep", "light"])
        self.assertEqual(bed[1][1] - bed[0][0], 1200)

    def test_sleep_file_invalid_record_ignored(self):
        bad = (1, 2, sleep_summary(0, 0), b"")  # no details
        self.assertEqual(f.parse_sleep_file(seq_file([bad]), True), [])
        with self.assertRaises(ValueError):
            f.parse_sequence_file(b"short")

    def test_real_band11_caps(self):
        caps = bytes.fromhex(BAND11_CAPS)
        bit = lambda n: bool(caps[n // 8] >> (n % 8) & 1)
        self.assertTrue(bit(f.BED_TIME_CAP) and bit(143))

    def test_stress_file(self):
        data = stress_file([(100, 200, 42), (300, 400, 0), (0, 500, 30), (600, 700, 77)])
        self.assertEqual(f.parse_stress_file(data), [(200, 42), (700, 77)])
        with self.assertRaises(ValueError):
            f.parse_stress_file(bytes(48))  # version 0
        with self.assertRaises(ValueError):
            f.parse_stress_file(b"x")


class FakeSession:
    """The band's side: answers requests from `replies` {(svc, cmd): TLV | Exception | callable(tlv)}."""

    def __init__(self, service, files=None, caps=BAND11_CAPS, encrypt=False):
        self.service, self.files, self.encrypt = service, files or {}, encrypt
        self.config, self.secret_key = {"expand_caps": caps}, KEY
        self.disconnected = asyncio.Event()
        self.transport = mock.Mock()
        self.replies, self.requests, self.sent, self.fid = {}, [], [], {}

    async def request(self, svc, cmd, tlv, timeout=10):
        self.requests.append((svc, cmd, tlv))
        r = self.replies.get((svc, cmd))
        if callable(r):
            r = r(tlv)
        if r is None:
            raise TimeoutError(f"no reply to {svc:02X}/{cmd:02X}")
        if isinstance(r, Exception):
            raise r
        return msg(svc, cmd, r)

    async def send(self, svc, cmd, tlv, encrypt=True):
        self.sent.append((svc, cmd, tlv))
        if (svc, cmd) == (f.FILES, f.BLOCK_REQ):
            fid, off, size = tlv.get_int(1), tlv.get_int(2), tlv.get_int(3)
            data = self.files[fid][off:off + size]
            for i in range(0, len(data), 40):  # the band answers a block in bursts
                body = bytes([fid]) + (off + i).to_bytes(4, "big") + b"\x00" + data[i:i + 40]
                if self.encrypt:
                    iv = bytes(16)
                    body = TLV().put_u8(0x7C, 1).put(0x7D, iv).put(0x7E, crypto.aes_gcm_encrypt(KEY, iv, body)).serialize()
                self.service.on_message(Message(f.FILES, f.BLOCK, body, None))

    def file_reply(self, name, fid, data):
        self.files[fid] = data
        self.replies[(f.FILES, f.INIT)] = lambda t: (
            TLV().put(1, t.get_str(1)).put_u8(2, t.get_int(2)).put_u8(3, fid).put_u32(4, len(data)))
        self.replies[(f.FILES, f.INFO)] = TLV().put_u8(1, fid).put_u8(2, 5).put_u16(3, 40).put_u32(4, 100).put(5, False)


def run(coro):
    return asyncio.run(coro)


class Download(unittest.TestCase):
    def test_download_assembles_blocks(self):
        data = bytes(range(256)) * 2
        for enc in (False, True):
            svc = FitnessService(FitnessStore(":memory:"))
            s = FakeSession(svc, encrypt=enc)
            svc.attach(s)
            s.file_reply("sequence_data", 0x16, data)

            async def go():
                return await svc.download(s, "sequence_data", 0x16, 1, 2, 700013)
            self.assertEqual(run(go()), data)
            blocks = [t for sv, c, t in s.sent if c == f.BLOCK_REQ]
            self.assertEqual([(b.get_int(2), b.get_int(3)) for b in blocks], [(0, 100), (100, 100), (200, 100),
                                                                          (300, 100), (400, 100), (500, 12)])
            self.assertEqual(s.sent[-1][1], f.COMPLETE)

    def test_download_errors(self):
        svc = FitnessService(FitnessStore(":memory:"))
        s = FakeSession(svc)
        svc.attach(s)
        s.file_reply("x", 0x10, b"abc")
        s.replies[(f.FILES, f.INIT)] = lambda t: TLV().put(1, "other").put_u8(2, 0x10).put_u8(3, 1).put_u32(4, 3)
        with self.assertRaises(RuntimeError):
            run(svc.download(s, "rrisqi_data.bin", 0x10, 1, 2))
        s.replies[(f.FILES, f.INIT)] = lambda t: TLV().put(1, t.get_str(1)).put_u8(2, 0x10).put_u8(3, 1).put_u32(4, 0xFFFFFFFF)
        with self.assertRaises(RuntimeError):
            run(svc.download(s, "rrisqi_data.bin", 0x10, 1, 2))
        s.replies[(f.FILES, f.INIT)] = lambda t: TLV().put(1, t.get_str(1)).put_u8(2, 0x10).put_u8(3, 1).put_u32(4, 0)
        self.assertEqual(run(svc.download(s, "rrisqi_data.bin", 0x10, 1, 2)), b"")


NOW = int(time.time())
DAY = datetime.fromtimestamp(NOW).date().isoformat()


def full_band(svc) -> FakeSession:
    s = FakeSession(svc)
    s.replies[(f.FITNESS, f.STEP_COUNT)] = TLV().put(0x81, TLV().put_u16(2, 2))
    chunks = [step_chunk(NOW - 7200, [(0, bytes.fromhex("CE01000A00050007" + "48" + "61"))]),
              step_chunk(NOW - 3600, [(1, bytes.fromhex("42001400"))])]
    s.replies[(f.FITNESS, f.STEP_DATA)] = lambda t: chunks[TLV.parse(t.get(0x81)).get_int(2)]
    s.replies[(f.FITNESS, f.TOTALS)] = TLV().put(0x81, TLV().put(0x83, TLV().put_u32(5, 321).put_u16(6, 40).put_u32(7, 250)))
    s.replies[(f.FITNESS, f.SLEEP_COUNT)] = TimeoutError("unsupported")
    sleep = seq_file([(1, 2, sleep_summary(NOW - 30000, NOW - 30060), details((600, 3), (1200, 1)))])
    stress = stress_file([(NOW - 500, NOW - 400, 33)])
    files = {"sequence_data": (0x16, sleep), "rrisqi_data.bin": (0x10, stress)}

    def init(t):
        fid, data = files[t.get_str(1)]
        s.files[fid] = data
        return TLV().put(1, t.get_str(1)).put_u8(2, fid).put_u8(3, fid).put_u32(4, len(data))
    s.replies[(f.FILES, f.INIT)] = init
    s.replies[(f.FILES, f.INFO)] = lambda t: TLV().put_u8(1, t.get_int(1)).put_u8(2, 5).put_u16(3, 40).put_u32(4, 4096).put(5, False)
    return s


class Sync(unittest.TestCase):
    def sync(self, svc, s, **kw):
        svc.attach(s)
        return run(svc.sync(s, **kw))

    def test_full_sync_writes_everything(self):
        store = FitnessStore(":memory:")
        svc = FitnessService(store)
        s = full_band(svc)
        report = self.sync(svc, s)
        self.assertEqual({k: v.split()[0] for k, v in report.items()},
                         {"sleep (07/0C-0D)": "failed", "samples": "ok", "totals": "ok", "sleep (file)": "ok",
                          "stress": "ok"})
        self.assertEqual(store.day(DAY)["steps"], 321)
        self.assertEqual(store.day(DAY)["source"], "band")
        with store._lock:
            samples = store._db.execute("SELECT * FROM samples ORDER BY ts").fetchall()
        self.assertEqual(samples, [(NOW - 7200, 10, 5, 7, 72), (NOW - 3540, 20, 0, 0, None)])
        with store._lock:
            self.assertEqual(store._db.execute("SELECT ts, value FROM spo2").fetchall(), [(NOW - 7200, 97)])
            self.assertEqual(store._db.execute("SELECT ts, value FROM stress").fetchall(), [(NOW - 400, 33)])
            sleep = store._db.execute("SELECT start, end, stage FROM sleep ORDER BY start").fetchall()
        self.assertEqual([r[2] for r in sleep], ["deep", "light"])
        self.assertEqual(sleep[1][1] - sleep[0][0], 1800)
        self.assertGreaterEqual(int(store.meta("last_sync")), NOW)

    def test_first_run_asks_for_seven_days_then_incremental(self):
        store = FitnessStore(":memory:")
        svc = FitnessService(store)
        s = full_band(svc)
        self.sync(svc, s)
        first = [t for sv, c, t in s.requests if c == f.STEP_COUNT][0]
        self.assertAlmostEqual(first.get_int(3), time.time() - 7 * 86400, delta=30)
        store.set_meta("last_sync", 1_700_000_000)
        s2 = full_band(svc)
        self.sync(svc, s2)
        second = [t for sv, c, t in s2.requests if c == f.STEP_COUNT][0]
        self.assertEqual(second.get_int(3), 1_700_000_000 - f.OVERLAP)
        self.assertGreaterEqual(int(store.meta("last_sync")), NOW)

    def test_one_failing_step_does_not_stop_the_rest(self):
        store = FitnessStore(":memory:")
        svc = FitnessService(store)
        s = full_band(svc)
        s.replies[(f.FITNESS, f.TOTALS)] = TimeoutError("no reply")
        s.replies[(f.FILES, f.INIT)] = ConnectionError("gone")
        report = self.sync(svc, s)
        self.assertTrue(report["totals"].startswith("failed"))
        self.assertTrue(report["sleep (file)"].startswith("failed"))
        self.assertTrue(report["stress"].startswith("failed"))
        self.assertTrue(report["samples"].startswith("ok"))
        self.assertEqual(len(store.hourly(DAY)), 24)
        with store._lock:
            self.assertEqual(store._db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 2)

    def test_failed_samples_keep_last_sync(self):
        store = FitnessStore(":memory:")
        store.set_meta("last_sync", 123)
        svc = FitnessService(store)
        s = full_band(svc)
        s.replies[(f.FITNESS, f.STEP_COUNT)] = TimeoutError("no reply")
        report = self.sync(svc, s)
        self.assertTrue(report["samples"].startswith("failed"))
        self.assertEqual(store.meta("last_sync"), "123")
        self.assertTrue(report["totals"].startswith("ok"))

    def test_legacy_sleep_skipped_after_one_failure(self):
        svc = FitnessService(FitnessStore(":memory:"))
        s = full_band(svc)
        self.sync(svc, s)
        n = sum(1 for sv, c, t in s.requests if c == f.SLEEP_COUNT)
        run(svc.sync(s))
        self.assertEqual(sum(1 for sv, c, t in s.requests if c == f.SLEEP_COUNT), n)

    def test_refused_request_is_a_failure(self):
        svc = FitnessService(FitnessStore(":memory:"))
        s = full_band(svc)
        s.replies[(f.FITNESS, f.STEP_COUNT)] = TLV().put_u32(0x7F, 0x186A4)
        self.assertTrue(self.sync(svc, s)["samples"].startswith("failed"))

    def test_only_reads_are_sent(self):
        svc = FitnessService(FitnessStore(":memory:"))
        s = full_band(svc)
        self.sync(svc, s)
        self.assertEqual({(sv, c) for sv, c, t in s.requests} | {(sv, c) for sv, c, t in s.sent} - {(f.FILES, f.COMPLETE)},
                         {(7, 0x0A), (7, 0x0B), (7, 3), (7, 0x0C), (f.FILES, 1), (f.FILES, 3), (f.FILES, 4)})

    def test_setup_starts_background_sync(self):
        async def go():
            store = FitnessStore(":memory:")
            svc = FitnessService(store, {"delay": 0})
            s = full_band(svc)
            await svc.setup(s)  # returns at once
            self.assertIsNone(store.meta("last_sync"))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if store.meta("last_sync"):
                    break
            return store.meta("last_sync")
        self.assertIsNotNone(run(go()))

    def test_disabled_does_not_sync(self):
        async def go():
            svc = FitnessService(FitnessStore(":memory:"), {"enabled": False, "delay": 0})
            s = full_band(svc)
            await svc.setup(s)
            await asyncio.sleep(0.05)
            return s.requests
        self.assertEqual(run(go()), [])


if __name__ == "__main__":
    unittest.main()
