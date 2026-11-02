import time
import unittest
from datetime import date, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

import widget
from band.fitness_store import FitnessStore
from bridge import dashboard


def client(store, status=lambda: {}):
    app = FastAPI()
    app.include_router(dashboard.router(store, status))
    return TestClient(app)


class DashboardTest(unittest.TestCase):
    def test_empty_store(self):
        d = client(FitnessStore(":memory:")).get("/fitness/summary").json()
        self.assertEqual(d["today"]["steps"], 0)
        self.assertEqual((len(d["hourly"]), len(d["days"]), d["sleep"], d["heart_rates"]), (24, 7, [], []))
        self.assertEqual(d["sleep_totals"], dict(deep=0, light=0, rem=0, awake=0))
        self.assertEqual((d["spo2"], d["stress"], d["spo2_latest"], d["stress_level"]), ([], [], None, None))
        self.assertIsNone(d["last_sync"])
        self.assertIsNone(d["battery"])

    def test_filled_store(self):
        st = FitnessStore(":memory:")
        noon = int(datetime.combine(date.today(), datetime.min.time()).timestamp()) + 12 * 3600
        st.add_samples([(noon, 500, 20, 400, 80), (noon + 900, 100, 4, 80, None)])
        st.add_spo2([(noon, 94), (noon + 60, 98)])
        st.add_stress([(noon, 20), (noon + 60, 65)])
        st.set_meta("last_sync", 1234)
        d = client(st, lambda: {"state": "ready", "battery": 55}).get("/fitness/summary?days=3").json()
        self.assertEqual((d["today"]["steps"], d["hourly"][12], len(d["days"])), (600, 600, 3))
        self.assertEqual((d["heart_rates"], d["last_sync"], d["battery"], d["state"]), ([[noon, 80]], 1234, 55, "ready"))
        self.assertEqual((d["spo2_latest"], d["spo2_min"], d["spo2_avg"], d["stress_latest"], d["stress_level"]),
                         (98, 94, 96, 65, "Medium"))
        self.assertEqual(d["goal"], dashboard.goal())

    def test_sleep_totals(self):
        t0 = int(time.time())
        segs = [dict(start=t0, end=t0 + 90 * 60, stage="light"), dict(start=t0 + 5400, end=t0 + 7200, stage="deep"),
                dict(start=t0 + 7200, end=t0 + 7500, stage="awake")]
        self.assertEqual(dashboard.sleep_totals(segs), dict(deep=30, light=90, rem=0, awake=5))

    def test_demo_has_everything(self):
        d = dashboard.summary(dashboard.demo_store())
        self.assertTrue(d["today"]["steps"] and d["sleep"] and d["heart_rates"] and d["spo2"] and d["stress"] and d["last_sync"])
        self.assertTrue(all(x["steps"] for x in d["days"][:-1]))

    def test_stress_level(self):
        self.assertEqual([dashboard.stress_level(v) for v in (1, 29, 30, 59, 60, 79, 80, 99, None)],
                         ["Relaxed", "Relaxed", "Normal", "Normal", "Medium", "Medium", "High", "High", None])

    def test_page(self):
        r = client(FitnessStore(":memory:")).get("/dashboard")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])
        self.assertIn("/fitness/summary", r.text)
        self.assertNotIn("http://", r.text.replace("http://www.w3.org", ""))  # offline: no external resources


class WidgetTest(unittest.TestCase):
    def test_ring_extent(self):
        self.assertEqual(widget.ring_extent(0, 8000), 0)
        self.assertAlmostEqual(widget.ring_extent(4000, 8000), 179.95)
        self.assertAlmostEqual(widget.ring_extent(20000, 8000), 359.9)
        self.assertEqual(widget.ring_extent(5, 0) > 0, True)

    def test_duration(self):
        self.assertEqual(widget.duration(425), "7h 05m")

    def test_stage_widths(self):
        segs = [dict(start=0, end=60, stage="deep"), dict(start=60, end=180, stage="rem")]
        self.assertEqual(widget.stage_widths(segs, 300), [("deep", 100), ("rem", 200)])
        self.assertEqual(widget.stage_widths([], 300), [])


if __name__ == "__main__":
    unittest.main()
