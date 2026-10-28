import math
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import market_forecast as f


class Forecast(unittest.TestCase):
    def test_score_renormalises_missing_factors(self):
        value, parts = f.score({"trend": 1.0, "momentum": None})
        self.assertAlmostEqual(value, 100)
        self.assertEqual(parts[0][0], "trend")
        self.assertEqual(f.score({})[0], 0.0)
        self.assertEqual((f.direction(25), f.direction(-20), f.direction(19)), (1, -1, 0))

    def test_live_factor_mapping(self):
        live = f.live_factors(taker_share=60, book_ratio=2.0, funding_ann=40, oi_chg24=10, ret_24h=-1, breadth=75,
                              macro_ret=-3)
        self.assertEqual(live, {"taker": 1.0, "book": 1.0, "funding": -1.0, "oi": -1.0, "breadth": 1.0, "macro": -1.0})
        self.assertEqual(f.live_factors(), {})

    def test_outlook_range_and_trend_sign(self):
        up = [100 * math.exp(0.001 * i + 0.002 * math.sin(i)) for i in range(400)]
        o = f.outlook(up)
        self.assertEqual(o["direction"], 1)
        self.assertLess(o["low"], o["price"])
        self.assertGreater(o["high"], o["price"])
        self.assertAlmostEqual(o["high"] / o["price"], math.exp(o["sigma"] * math.sqrt(24)))
        self.assertIsNone(f.outlook(up[:100]))

    def test_backtest_detects_trend_and_reports_significance(self):
        trend = [100 * math.exp(0.001 * i + 0.003 * math.sin(i)) for i in range(1400)]
        bt = f.backtest(trend)
        self.assertGreater(bt["hit_rate"], 90)
        self.assertTrue(bt["significant"])
        rng = random.Random(7)
        walk = [100.0]
        for _ in range(1399):
            walk.append(walk[-1] * math.exp(rng.gauss(0, 0.01)))
        noise = f.backtest(walk)
        self.assertGreater(noise["coverage"], 50)
        self.assertEqual(noise["effective"], round(noise["signals"] * 6 / 24, 1))

    def test_evaluate(self):
        forecast = {"price": 100.0, "direction": 1, "low": 98.0, "high": 102.0}
        self.assertEqual(f.evaluate(forecast, 101.0), (1.0000000000000009, 1, 1))
        self.assertEqual(f.evaluate(dict(forecast, direction=0), 90.0)[1:], (None, 0))


if __name__ == "__main__":
    unittest.main()
