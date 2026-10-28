import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import market_analysis as a


def spot(pid, vol, chg, price="1", alias="", vchg="0", status="online"):
    base, quote = pid.split("-")
    return {"product_id": pid, "product_type": "SPOT", "base_currency_id": base, "quote_currency_id": quote,
            "base_name": base, "approximate_quote_24h_volume": str(vol), "price_percentage_change_24h": str(chg),
            "volume_percentage_change_24h": vchg, "price": price, "alias": alias, "status": status}


def candle(start, o, c, vol, h=None, l=None):
    return {"start": str(start), "open": str(o), "close": str(c), "high": str(h or max(o, c)),
            "low": str(l or min(o, c)), "volume": str(vol)}


class CryptoAggregation(unittest.TestCase):
    def test_alias_once_usdt_added_fiat_ignored(self):
        assets = a.crypto_assets([spot("BTC-USD", 100, 1, "80000"), spot("BTC-USDC", 100, 1, alias="BTC-USD"),
                                  spot("BTC-USDT", 5, 2, "80010"), spot("BTC-EUR", 50, 1)])
        self.assertEqual(assets["BTC"]["volume"], 105)
        self.assertEqual((assets["BTC"]["price"], assets["BTC"]["product_id"]), (80000, "BTC-USD"))

    def test_offline_disabled_and_blank_base_skipped(self):
        bad = spot("X-USD", 9, 1, status="offline")
        disabled = dict(spot("Y-USD", 9, 1), trading_disabled=True)
        self.assertEqual(a.crypto_assets([bad, disabled]), {})

    def test_previous_volume_from_percentage(self):
        assets = a.crypto_assets([spot("ETH-USD", 150, 1, vchg="50")])
        self.assertAlmostEqual(assets["ETH"]["prev_volume"], 100)

    def test_overview_excludes_stables_from_ranking(self):
        ov = a.crypto_overview([spot("BTC-USD", 300, 1), spot("USDT-USD", 500, 0), spot("ETH-USD", 100, -1),
                                spot("SOL-USD", 100, 2)])
        self.assertEqual([x["symbol"] for x in ov["top"]], ["BTC", "ETH", "SOL"])
        self.assertEqual([s["symbol"] for s in ov["stables"]], ["USDT"])
        self.assertEqual(ov["flow"]["total"], 500)
        self.assertEqual(ov["flow"]["net"], 300)
        self.assertAlmostEqual(ov["major_share"], 80)

    def test_flow_summary_counts_flat_as_neither(self):
        f = a.flow_summary([{"volume": 80, "change": 2}, {"volume": 20, "change": -1}, {"volume": 5, "change": 0},
                            {"volume": 1, "change": None}])
        self.assertEqual((f["up"], f["down"], f["net"], f["advancers"], f["decliners"]), (80, 20, 60, 1, 1))


class CryptoWindow(unittest.TestCase):
    END = 1_793_160_000  # aligned to the hour

    def test_turnover_uses_typical_price(self):
        self.assertAlmostEqual(a.candle_turnover(candle(0, 10, 12, 3, h=13, l=8)), 3 * (13 + 8 + 12) / 3)

    def test_window_split_direction_and_ranking(self):
        h = 3600
        btc = [candle(self.END - 8 * h + i * h, 100, 100, 1) for i in range(4)]            # prior window, flat
        btc += [candle(self.END - 4 * h, 100, 110, 1, h=110, l=100),                       # up hour
                candle(self.END - 3 * h, 110, 105, 2, h=110, l=105)]                       # down hour
        eth = [candle(self.END - 2 * h, 10, 9, 10, h=10, l=9)]                             # down hour only
        w = a.crypto_window_flow({"BTC": btc, "ETH": eth}, self.END, hours=4)
        btc_up = 1 * (110 + 100 + 110) / 3
        btc_down = 2 * (110 + 105 + 105) / 3
        eth_down = 10 * (10 + 9 + 9) / 3
        self.assertAlmostEqual(w["inflow"], btc_up)
        self.assertAlmostEqual(w["outflow"], btc_down + eth_down)
        self.assertAlmostEqual(w["total"], btc_up + btc_down + eth_down)
        self.assertAlmostEqual(w["vs_prior"], (w["total"] / 400 - 1) * 100)
        self.assertEqual([x["symbol"] for x in w["top_out"]], ["BTC", "ETH"])  # most negative first
        self.assertEqual(w["top_in"], [])
        btc_row = [x for x in w["top_out"] if x["symbol"] == "BTC"][0]
        self.assertAlmostEqual(btc_row["change"], 5.0)

    def test_candle_at_window_end_is_excluded(self):
        w = a.crypto_window_flow({"BTC": [candle(self.END, 1, 2, 100)]}, self.END)
        self.assertEqual(w["total"], 0)
        self.assertIsNone(w["vs_prior"])


class Parsing(unittest.TestCase):
    def test_money_and_flow_value(self):
        self.assertEqual(a.money("$1,234.50"), 1234.5)
        self.assertEqual(a.money("-2.1%"), -2.1)
        self.assertIsNone(a.money("N/A"))
        self.assertIsNone(a.fnum("nan"))
        self.assertEqual((a.flow_value(100, 3), a.flow_value(100, -1), a.flow_value(100, None)), (100, -50, 0))

    def test_flow_summary_includes_scaled_flow(self):
        f = a.flow_summary([{"volume": 100, "change": 4}, {"volume": 100, "change": -1}])
        self.assertEqual((f["net"], f["flow"]), (0, 50))


class UsSession(unittest.TestCase):
    INFO = {"openRaw": "2026-10-28T09:30:00", "closeRaw": "2026-10-28T16:00:00", "previousTradeDate": "Oct 27, 2026"}

    @staticmethod
    def at(text):
        return int(dt.datetime.fromisoformat(text).replace(tzinfo=a.ET).timestamp())

    def test_before_open_uses_previous_trade_date(self):
        s = a.us_session(self.INFO, self.at("2026-10-28T04:30:00"))
        self.assertEqual((s["state"], s["trade_date"]), ("previous", "2026-10-27"))

    def test_intraday(self):
        s = a.us_session(self.INFO, self.at("2026-10-28T12:00:00"))
        self.assertEqual((s["state"], s["trade_date"], s["as_of"]), ("intraday", "2026-10-28", "12:00"))

    def test_after_close_and_weekend_report_that_session(self):
        self.assertEqual(a.us_session(self.INFO, self.at("2026-10-28T16:00:00"))["state"], "closed")
        friday = {"openRaw": "2026-10-30T09:30:00", "closeRaw": "2026-10-30T16:00:00",
                  "previousTradeDate": "Oct 29, 2026"}
        s = a.us_session(friday, self.at("2026-11-01T10:00:00"))  # Sunday, after the DST switch
        self.assertEqual((s["state"], s["trade_date"]), ("closed", "2026-10-30"))

    def test_unknown_when_missing_or_malformed(self):
        self.assertEqual(a.us_session(None, 0)["state"], "unknown")
        self.assertEqual(a.us_session({"openRaw": "bad", "closeRaw": "x"}, 0)["state"], "unknown")
        s = a.us_session(dict(self.INFO, previousTradeDate="?"), self.at("2026-10-28T01:00:00"))
        self.assertEqual((s["state"], s["trade_date"]), ("previous", None))


if __name__ == "__main__":
    unittest.main()
