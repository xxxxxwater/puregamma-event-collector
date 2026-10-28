import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import market_crypto as c


def trade(ts, price, side, size=1.0, tid=None):
    return {"ts": ts, "price": str(price), "size": str(size), "side": side, "trade_id": str(tid or ts)}


class Crypto(unittest.TestCase):
    def test_side_is_maker_side(self):
        self.assertEqual(c.taker_side({"side": "SELL"}), "buy")
        self.assertEqual(c.taker_side({"side": "BUY"}), "sell")
        consistent = [trade(1, 100, "BUY"), trade(2, 101, "SELL"), trade(3, 100, "BUY"), trade(4, 100, "SELL")]
        ratio, n = c.maker_side_consistency(consistent)
        self.assertEqual((ratio, n), (1.0, 2))

    def test_bucketize_amounts_and_coverage(self):
        trades = [trade(905, 10, "SELL", 2), trade(910, 10, "BUY", 1), trade(1810, 20, "SELL", 1)]
        b = c.bucketize(trades, covered_from=850, covered_to=1900)
        self.assertEqual(b[900]["buy"], 20)
        self.assertEqual(b[900]["sell"], 10)
        self.assertEqual(b[1800]["buy"], 20)
        self.assertEqual((b[0]["covered"], b[900]["covered"], b[1800]["covered"]), (50, 900, 100))

    def test_taker_summary(self):
        s = c.taker_summary({"BTC": {"buy": 60.0, "sell": 40.0, "trades": 5, "covered": 7200.0},
                             "ETH": {"buy": 10.0, "sell": 30.0, "trades": 3, "covered": 14400.0}}, 14400)
        self.assertEqual((s["buy"], s["sell"], s["net"]), (70.0, 70.0, 0.0))
        self.assertEqual(s["coverage"], 0.5)
        self.assertEqual(s["rows"][0]["symbol"], "BTC")

    def test_book_depth(self):
        book = {"bids": [{"price": "100", "size": "2"}, {"price": "98", "size": "100"}, {"price": "x", "size": "1"}],
                "asks": [{"price": "100.2", "size": "1"}, {"price": "100.9", "size": "1"}]}
        depth = c.book_depth(book)
        self.assertAlmostEqual(depth["bid_usd"], 200)
        self.assertAlmostEqual(depth["ask_usd"], 100.2 + 100.9)
        self.assertIsNone(c.book_depth({"bids": [], "asks": []}))

    def test_candle_metrics_and_anomalies(self):
        candles = [{"start": i * 3600, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10}
                   for i in range(180)]
        candles[-1] = {"start": 179 * 3600, "open": 100, "high": 106, "low": 100, "close": 105, "volume": 100}
        m = c.candle_metrics(candles)
        self.assertAlmostEqual(m["ret_1h"], 5.0)
        self.assertAlmostEqual(m["rvol_1h"], (100 * (106 + 100 + 105) / 3) / (10 * 100))
        self.assertEqual(m["high_7d"], 101)
        found = {(a["symbol"], a["type"]): a for a in c.crypto_anomalies({"BTC": m, "XYZ": m}, 179 * 3600)}
        self.assertTrue(found[("BTC", "move1h")]["major"])
        self.assertIn(("XYZ", "move1h"), found)
        self.assertFalse(found[("XYZ", "move1h")]["major"])     # small cap: needs >= 8% and >= $2M
        self.assertIn(("BTC", "high7d"), found)
        self.assertIsNone(c.candle_metrics(candles[:10]))
        self.assertIsNotNone(c.ewma_sigma([100, 101, 100, 102]))


if __name__ == "__main__":
    unittest.main()
