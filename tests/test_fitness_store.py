import unittest
from datetime import datetime

from band.fitness_store import FitnessStore


def ts(text):
    return int(datetime.fromisoformat(text).timestamp())


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.s = FitnessStore(":memory:")

    def test_day_sums_samples_until_the_band_gives_totals(self):
        self.s.add_samples([(ts("2026-11-02 08:00"), 100, 5, 70, 80), (ts("2026-11-02 08:01"), 50, 2, 35, None),
                            (ts("2026-11-03 00:00"), 999, 0, 0, None)])
        self.assertEqual(self.s.day("2026-11-02"),
                         dict(day="2026-11-02", steps=150, calories=7, distance=105, source="samples"))
        self.s.set_totals("2026-11-02", 4000, 200, 3000, 0)
        self.assertEqual(self.s.day("2026-11-02")["steps"], 4000)

    def test_repeated_sample_replaces(self):
        self.s.add_samples([(ts("2026-11-02 08:00"), 100, 0, 0, None)])
        self.s.add_samples([(ts("2026-11-02 08:00"), 120, 0, 0, None)])
        self.assertEqual(self.s.day("2026-11-02")["steps"], 120)

    def test_days_hourly_and_heart_rates(self):
        self.s.add_samples([(ts("2026-11-02 08:10"), 10, 0, 0, 70), (ts("2026-11-02 08:50"), 5, 0, 0, 0),
                            (ts("2026-11-02 23:30"), 7, 0, 0, None)])
        self.assertEqual([d["day"] for d in self.s.days(3, "2026-11-02")], ["2026-10-31", "2026-11-01", "2026-11-02"])
        hours = self.s.hourly("2026-11-02")
        self.assertEqual((hours[8], hours[23], sum(hours)), (15, 7, 22))
        self.assertEqual(self.s.heart_rates("2026-11-02"), [(ts("2026-11-02 08:10"), 70)])

    def test_sleep_night_runs_from_the_evening_before(self):
        self.s.add_sleep([(ts("2026-11-01 23:00"), ts("2026-11-02 01:00"), "light"),
                          (ts("2026-11-02 01:00"), ts("2026-11-02 06:00"), "deep"),
                          (ts("2026-11-02 20:00"), ts("2026-11-02 21:00"), "light")])  # the next night
        self.assertEqual([x["stage"] for x in self.s.sleep("2026-11-02")], ["light", "deep"])
        with self.assertRaises(ValueError):
            self.s.add_sleep([(0, 1, "dreaming")])

    def test_spo2_and_stress_per_day(self):
        self.s.add_spo2([(ts("2026-11-02 03:00"), 96), (ts("2026-11-03 03:00"), 97)])
        self.s.add_stress([(ts("2026-11-02 10:00"), 32)])
        self.assertEqual(self.s.spo2("2026-11-02"), [(ts("2026-11-02 03:00"), 96)])
        self.assertEqual(self.s.stress("2026-11-02"), [(ts("2026-11-02 10:00"), 32)])

    def test_meta(self):
        self.assertIsNone(self.s.meta("last_sync"))
        self.s.set_meta("last_sync", 5)
        self.assertEqual(self.s.meta("last_sync"), "5")


if __name__ == "__main__":
    unittest.main()
