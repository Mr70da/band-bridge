"""Local store for the band's activity and sleep (data/fitness.db). Times are unix seconds; days are local dates."""
import sqlite3
import threading
from datetime import date, datetime, timedelta

DB = "data/fitness.db"
STAGES = ("deep", "light", "rem", "awake")
SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (ts INTEGER PRIMARY KEY, steps INTEGER NOT NULL DEFAULT 0,
    calories INTEGER NOT NULL DEFAULT 0, distance INTEGER NOT NULL DEFAULT 0, heart_rate INTEGER);
CREATE TABLE IF NOT EXISTS sleep (start INTEGER PRIMARY KEY, end INTEGER NOT NULL, stage TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS totals (day TEXT PRIMARY KEY, steps INTEGER, calories INTEGER, distance INTEGER,
    updated INTEGER);
CREATE TABLE IF NOT EXISTS spo2 (ts INTEGER PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS stress (ts INTEGER PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _day_start(day: str) -> int:
    return int(datetime.combine(date.fromisoformat(day), datetime.min.time()).timestamp())


class FitnessStore:
    """samples: one row per band interval (steps, calories in kcal, distance in m, heart rate or None).
    totals: the band's own day totals, which win over summed samples. All methods are thread-safe."""

    def __init__(self, path=DB):
        if path != ":memory:":
            import os
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def _write(self, sql, rows):
        with self._lock, self._db:
            self._db.executemany(sql, rows)

    def _read(self, sql, args=()):
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # --- writing (the band sync) ---

    def add_samples(self, rows):
        """rows: (ts, steps, calories, distance, heart_rate or None); a repeated ts replaces the old row."""
        self._write("INSERT OR REPLACE INTO samples VALUES (?, ?, ?, ?, ?)", rows)

    def add_sleep(self, rows):
        """rows: (start, end, stage) with stage in STAGES."""
        bad = [r for r in rows if r[2] not in STAGES]
        if bad:
            raise ValueError(f"unknown sleep stage in {bad[0]}")
        self._write("INSERT OR REPLACE INTO sleep VALUES (?, ?, ?)", rows)

    def set_totals(self, day: str, steps, calories, distance, updated: int):
        self._write("INSERT OR REPLACE INTO totals VALUES (?, ?, ?, ?, ?)", [(day, steps, calories, distance, updated)])

    def add_spo2(self, rows):
        """rows: (ts, percent)."""
        self._write("INSERT OR REPLACE INTO spo2 VALUES (?, ?)", rows)

    def add_stress(self, rows):
        """rows: (ts, score 1..99 as the band shows it)."""
        self._write("INSERT OR REPLACE INTO stress VALUES (?, ?)", rows)

    def set_meta(self, key, value):
        self._write("INSERT OR REPLACE INTO meta VALUES (?, ?)", [(key, str(value))])

    def meta(self, key, default=None):
        rows = self._read("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0][0] if rows else default

    # --- reading (dashboard, widget, tray) ---

    def day(self, day: str) -> dict:
        """{day, steps, calories, distance, source}: the band's totals if we have them, else summed samples."""
        t = self._read("SELECT steps, calories, distance FROM totals WHERE day = ?", (day,))
        if t:
            return dict(day=day, steps=t[0][0], calories=t[0][1], distance=t[0][2], source="band")
        start = _day_start(day)
        s = self._read("SELECT COALESCE(SUM(steps), 0), COALESCE(SUM(calories), 0), COALESCE(SUM(distance), 0) "
                       "FROM samples WHERE ts >= ? AND ts < ?", (start, start + 86400))[0]
        return dict(day=day, steps=s[0], calories=s[1], distance=s[2], source="samples")

    def days(self, n: int, today: str | None = None) -> list[dict]:
        """The last n days, oldest first, ending today."""
        end = date.fromisoformat(today) if today else date.today()
        return [self.day((end - timedelta(days=i)).isoformat()) for i in range(n - 1, -1, -1)]

    def hourly(self, day: str) -> list[int]:
        """Steps per local hour, 24 values."""
        start = _day_start(day)
        hours = [0] * 24
        for ts, steps in self._read("SELECT ts, steps FROM samples WHERE ts >= ? AND ts < ?", (start, start + 86400)):
            hours[datetime.fromtimestamp(ts).hour] += steps
        return hours

    def heart_rates(self, day: str) -> list[tuple[int, int]]:
        start = _day_start(day)
        return self._read("SELECT ts, heart_rate FROM samples WHERE ts >= ? AND ts < ? AND heart_rate > 0 "
                          "ORDER BY ts", (start, start + 86400))

    def spo2(self, day: str) -> list[tuple[int, int]]:
        start = _day_start(day)
        return self._read("SELECT ts, value FROM spo2 WHERE ts >= ? AND ts < ? ORDER BY ts", (start, start + 86400))

    def stress(self, day: str) -> list[tuple[int, int]]:
        start = _day_start(day)
        return self._read("SELECT ts, value FROM stress WHERE ts >= ? AND ts < ? ORDER BY ts", (start, start + 86400))

    def sleep(self, day: str) -> list[dict]:
        """The night that ends on the morning of `day`: segments starting from 18:00 the evening before to 18:00."""
        start = _day_start(day) - 6 * 3600
        return [dict(start=s, end=e, stage=st) for s, e, st in self._read(
            "SELECT start, end, stage FROM sleep WHERE start >= ? AND start < ? ORDER BY start",
            (start, start + 86400))]
