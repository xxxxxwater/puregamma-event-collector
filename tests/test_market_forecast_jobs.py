import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import market_jobs as jobs
import market_store as store


class ForecastEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.con = store.connect(str(Path(self.tmp.name) / "market.sqlite"))
        self.addCleanup(self.con.close)
        self.fetch = Mock(side_effect=AssertionError("No network requests allowed"))
        self.collector = jobs.Collector(self.con, fetch=self.fetch)
        self.created = 1_793_160_000
        self.target = self.created + 24 * jobs.HOUR
        store.add_forecast(self.con, {
            "created_at": self.created, "target": "BTC", "horizon_h": 24,
            "price": 100.0, "score": 30.0, "direction": 1,
            "sigma": 0.01, "low": 98.0, "high": 102.0, "factors": {},
        })

    def bar(self, start, close):
        store.upsert_candles(self.con, "BTC-USD", jobs.HOUR, [{
            "start": start, "open": close, "high": close, "low": close,
            "close": close, "volume": 1,
        }])

    def row(self):
        return dict(self.con.execute("SELECT * FROM forecasts").fetchone())

    def test_waits_for_target_bar_then_scores_once(self):
        self.bar(self.created, 101.0)
        # A later bar must not substitute for the missing target either.
        self.bar(self.target, 102.0)
        self.collector.forecast(self.target + jobs.HOUR)
        self.assertIsNone(self.row()["evaluated_at"])
        self.assertEqual(store.forecast_record(self.con, "BTC", 0)["evaluated"], 0)
        self.bar(self.target - jobs.HOUR, 99.0)
        self.collector.forecast(self.target + 2 * jobs.HOUR)
        result = self.row()
        self.assertAlmostEqual(result["realized"], -1.0)
        self.assertEqual(result["hit"], 0)
        self.assertEqual(result["in_range"], 1)
        self.assertEqual(result["evaluated_at"], self.target + 2 * jobs.HOUR)
        self.bar(self.target - jobs.HOUR, 101.0)
        self.collector.forecast(self.target + 3 * jobs.HOUR)
        self.assertEqual(self.row(), result)
        self.fetch.assert_not_called()

    def test_missing_target_expires_unknown_after_grace_period(self):
        self.bar(self.created, 101.0)
        self.collector.forecast(self.target + jobs.DAY)
        self.assertIsNone(self.row()["evaluated_at"])
        self.collector.forecast(self.target + jobs.DAY + 1)
        result = self.row()
        self.assertEqual(result["evaluated_at"], self.target + jobs.DAY + 1)
        self.assertEqual((result["realized"], result["hit"], result["in_range"]), (None, None, None))
        self.assertEqual(store.forecast_record(self.con, "BTC", 0)["directional"], 0)
        self.bar(self.target - jobs.HOUR, 99.0)
        self.collector.forecast(self.target + jobs.DAY + jobs.HOUR)
        self.assertEqual(self.row(), result)

    def test_target_bar_scores_at_maturity_not_before(self):
        self.bar(self.target - jobs.HOUR, 101.0)
        self.collector.forecast(self.target - 1)
        self.assertIsNone(self.row()["evaluated_at"])
        self.collector.forecast(self.target)
        self.assertEqual(self.row()["hit"], 1)
        self.assertAlmostEqual(self.row()["realized"], 1.0)

    def test_empty_history_expires_unknown(self):
        self.collector.forecast(self.target)
        self.assertIsNone(self.row()["evaluated_at"])
        self.collector.forecast(self.target + jobs.DAY + 1)
        self.assertIsNone(self.row()["realized"])
        self.assertIsNotNone(self.row()["evaluated_at"])


if __name__ == "__main__":
    unittest.main()
