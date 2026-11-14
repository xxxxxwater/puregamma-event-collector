import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import market_report as report
import market_store as store


class ForecastCacheAgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(self.tmp.name + "/test.sqlite")
        self.service = report.MarketService(self.con, None, fetch=lambda *_a, **_k: self.fail("network called"))
        self.at = 1793160000
        self.service.collector.cache.update(outlooks={"BTC": {
            "price": 100, "score": 1, "direction": "up", "low": 90, "high": 110,
            "band_pct": 10, "parts": []}}, outlooks_at=self.at)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def build(self, now):
        # Only rendering is stubbed; production cache validation, digest,
        # fallback and SQLite snapshots remain active.
        with patch("market_render.forecast", return_value="future outlook"):
            return self.service.build_snapshot(now)

    def test_expiry_and_refresh_do_not_renew_original_generation(self):
        for age in (0, 3599, 3600):
            snap = self.build(self.at + age)
            self.assertEqual(snap["status"]["forecast"], "ok")
            self.assertEqual(snap["metrics"]["forecast"]["digest"]["at"], self.at)
            self.assertIn("研判生成：10-28 12:00", snap["sections"]["forecast"])
        for age in (3601, 3700, 25 * 3600):
            snap = self.build(self.at + age)
            self.assertNotEqual(snap["status"]["forecast"], "ok")
            self.assertNotIn("future outlook", snap["sections"]["forecast"])
            self.assertIn("暂不可用", snap["sections"]["forecast"])
        self.service.collector.cache["outlooks_at"] = self.at + 25 * 3600
        self.assertEqual(self.build(self.at + 25 * 3600)["status"]["forecast"], "ok")

    def test_fallback_uses_generation_not_recent_snapshot_timestamp(self):
        self.build(self.at + 3590)
        self.service.collector.cache["outlooks"] = {}
        reused = self.build(self.at + 3595)
        self.assertTrue(reused["status"]["forecast"].startswith("reused"))
        self.assertEqual(reused["metrics"]["forecast"]["reused_from"], self.at)
        self.assertIn("暂不可用", self.build(self.at + 3601)["sections"]["forecast"])

    def test_missing_future_and_invalid_timestamps_fail_closed(self):
        for stamp in (None, 0, -1, self.at + 1, float("nan"), float("inf"), "bad", True):
            with self.subTest(stamp=stamp):
                self.service.collector.cache["outlooks_at"] = stamp
                snap = self.build(self.at)
                self.assertNotEqual(snap["status"]["forecast"], "ok")
                self.assertNotIn("future outlook", snap["sections"]["forecast"])


if __name__ == "__main__":
    unittest.main()
