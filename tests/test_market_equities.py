import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import market_equities as e

# Verbatim Nasdaq watchlist row captured in pre-market on 2026-10-28 (DESIGN C2).
NVDA_PRE = {"symbol": "NVDA", "companyName": "NVIDIA Corporation Common Stock", "lastSalePrice": "$219.50",
            "netChange": "+1.56", "percentageChange": "+0.72%", "deltaIndicator": "up",
            "lastTradeTimestamp": "Oct 28, 2026 4:28 AM ET",
            "lastTradeTimestampDateTime": "2026-10-28T04:28:49.4863758-04:00", "volume": "84,802,023",
            "assetClass": "STOCKS", "previousClosePrice": 216.0, "openingPrice": 218.35, "marketStatus": "Pre Market"}
INFO = {"openRaw": "2026-10-28T09:30:00", "closeRaw": "2026-10-28T16:00:00", "previousTradeDate": "Oct 27, 2026",
        "pmOpenRaw": "2026-10-28T04:00:00", "ahCloseRaw": "2026-10-28T20:00:00"}


def at(text):
    return int(dt.datetime.fromisoformat(text).replace(tzinfo=e.ET).timestamp())


def tick(ticker="AAA", status="open", last=10.0, chg_abs=0.5, chg_pct=5.0, volume=1e7, prev_close=9.5):
    return {"ticker": ticker, "status": status, "last": last, "chg_abs": chg_abs, "chg_pct": chg_pct,
            "volume": volume, "prev_close": prev_close, "open": 9.6, "trade_ts": None}


class TickSemantics(unittest.TestCase):
    def test_parse_real_pre_market_row(self):
        t = e.parse_tick(NVDA_PRE)
        self.assertEqual((t["ticker"], t["status"], t["last"], t["chg_abs"], t["volume"], t["prev_close"]),
                         ("NVDA", "pre", 219.5, 1.56, 84802023.0, 216.0))
        self.assertIsNotNone(t["trade_ts"])
        self.assertIsNone(e.parse_tick({"symbol": "X", "lastSalePrice": "N/A"}))

    def test_pre_market_reconstructs_last_session(self):
        view = e.session_view(e.parse_tick(NVDA_PRE))
        self.assertAlmostEqual(view["price"], 217.94)                 # 10/27 close
        self.assertAlmostEqual(view["change"], (217.94 / 216 - 1) * 100)  # +0.90%, matches Nasdaq
        self.assertEqual(view["volume"], 84802023.0)
        self.assertAlmostEqual(view["ext"], 0.72)
        self.assertFalse(view["live"])
        self.assertFalse(view["unverified"])

    def test_open_session_is_live(self):
        view = e.session_view(tick())
        self.assertEqual((view["price"], view["change"], view["turnover"], view["live"]), (10.0, 5.0, 1e8, True))

    def test_after_hours_gated_by_self_check(self):
        after = tick(status="after", last=10.2, chg_abs=0.2, chg_pct=2.0)
        self.assertTrue(e.session_view(after)["unverified"])
        self.assertFalse(e.session_view(after, "validated")["unverified"])
        fallback = e.session_view(after, "failed", last_open=tick(last=9.9, chg_pct=4.2))
        self.assertEqual((fallback["price"], fallback["change"], fallback["live"]), (9.9, 4.2, False))
        self.assertIsNone(e.session_view(after, "failed"))

    def test_after_hours_hypothesis_test(self):
        live = {f"T{i}": tick(f"T{i}", last=100.0, chg_pct=3.0) for i in range(25)}
        pre_like = {k: tick(k, status="after", last=101.0, chg_abs=1.0) for k in live}       # 101 - 1 = today close
        other = {k: tick(k, status="after", last=101.0, chg_abs=101.0 - 100 / 1.03) for k in live}  # vs yesterday
        self.assertTrue(e.after_hours_check(pre_like, live)["ok"])
        self.assertFalse(e.after_hours_check(other, live)["ok"])
        self.assertIsNone(e.after_hours_check(dict(list(pre_like.items())[:5]), live))


class Classification(unittest.TestCase):
    def test_kind_of(self):
        self.assertEqual(e.kind_of("EQUITY_PRODUCT_SUBTYPE_ADR"), "stock")
        self.assertEqual(e.kind_of("EQUITY_PRODUCT_SUBTYPE_ETF"), "etf")
        self.assertIsNone(e.kind_of("EQUITY_PRODUCT_SUBTYPE_WARRANT"))

    def test_etf_categories(self):
        cases = {
            ("SPY", None): e.BROAD, ("TQQQ", None): e.LEVERAGED, ("IBIT", None): e.CRYPTO,
            ("XYZ1", "Direxion Daily Semiconductor Bull 3X Shares"): e.LEVERAGED,
            ("XYZ2", "iShares 0-3 Month Treasury Bond ETF"): e.BOND,
            ("XYZ3", "Vanguard Short-Term Corporate Bond ETF"): e.BOND,
            ("XYZ4", "VanEck Gold Miners ETF"): e.SECTOR,
            ("XYZ5", "abrdn Physical Silver Shares ETF"): e.COMMODITY,
            ("XYZ6", "Goldman Sachs ActiveBeta U.S. Large Cap Equity ETF"): e.BROAD,
            ("XYZ7", "JPMorgan Equity Premium Income ETF"): e.BROAD,
            ("XYZ8", "iShares MSCI Japan ETF"): e.INTL,
            ("XYZ9", "Invesco S&P 500 Low Volatility ETF"): e.BROAD,
            ("XYZA", "iPath Series B S&P 500 VIX Short-Term Futures ETN"): e.VOLATILITY,
            ("XYZB", "Global X Robotics & Artificial Intelligence ETF"): e.SECTOR,
            ("XYZC", "Grayscale Ethereum Trust"): e.CRYPTO,
            ("XYZD", "Some Unclassifiable Fund"): e.OTHER,
        }
        for (ticker, name), expected in cases.items():
            self.assertEqual(e.etf_category(ticker, name), expected, (ticker, name))

    def test_industry_names(self):
        self.assertEqual(e.industry_zh("Semiconductors"), "半导体")
        self.assertEqual(e.industry_zh(""), "其他")
        self.assertEqual(e.industry_zh("Very Long Unknown Industry Name Here"), "Very Long Unknown ")


class Board(unittest.TestCase):
    META = {"AAA": {"kind": "stock", "name": "A", "sector": "Technology", "industry": "Semiconductors"},
            "BBB": {"kind": "stock", "name": "B", "sector": "Energy", "industry": "Integrated oil Companies"},
            "CCC": {"kind": "stock", "name": "C", "sector": "Technology", "industry": "Semiconductors"},
            "QQQ": {"kind": "etf", "name": "Q", "category": e.BROAD},
            "SQQQ": {"kind": "etf", "name": "S", "category": e.LEVERAGED},
            "WWW": {"kind": None}}

    def rows(self, **overrides):
        ticks = {"AAA": tick("AAA", last=100, chg_pct=4.0, volume=1e7),       # +4% -> full inflow 1e9
                 "BBB": tick("BBB", last=50, chg_pct=-1.0, volume=2.2e7),     # -1% -> half outflow
                 "CCC": tick("CCC", last=20, chg_pct=0.0, volume=1e6),
                 "QQQ": tick("QQQ", last=500, chg_pct=1.0, volume=4e6),
                 "SQQQ": tick("SQQQ", last=10, chg_pct=-3.0, volume=5e7),
                 "WWW": tick("WWW")}
        ticks.update(overrides)
        return e.board(ticks, self.META, {"AAA": 2e6}, at("2026-10-28T12:45:00"), at("2026-10-28T09:30:00"))

    def test_flows_rvol_and_grouping(self):
        rows = self.rows()
        by = {r["symbol"]: r for r in rows}
        self.assertNotIn("WWW", by)
        self.assertAlmostEqual(by["AAA"]["flow"], 1e9)
        self.assertAlmostEqual(by["BBB"]["flow"], -5.5e8)
        self.assertEqual(by["AAA"]["industry"], "半导体")
        fraction = (3.25 * 60) / 390
        self.assertAlmostEqual(by["AAA"]["rvol"], 1e7 / (2e6 * fraction ** 0.7))
        ov = e.equity_overview(rows)
        self.assertEqual([r["symbol"] for r in ov["top"]], ["BBB", "AAA", "CCC"])
        self.assertEqual(ov["sectors"][0]["name"], "科技")
        self.assertEqual(ov["etf_in"][0]["symbol"], "QQQ")
        self.assertEqual([g["name"] for g in ov["categories"]], [e.BROAD, e.LEVERAGED])

    def test_anomalies_live_gating_and_velocity(self):
        rows = self.rows(AAA=tick("AAA", last=100, chg_pct=13.0, volume=1e7))
        past = {15: {"AAA": 96.0}}
        found = {(a["symbol"], a["type"]): a for a in e.equity_anomalies(rows, at("2026-10-28T12:45:00"), past)}
        self.assertTrue(found[("AAA", "move")]["major"])
        self.assertIn(("AAA", "velocity15"), found)
        self.assertIn(("AAA", "volume"), found)
        self.assertNotIn(("SQQQ", "move"), found)          # leveraged ETFs are skipped
        closed = [dict(r, live=False) for r in rows]
        moves = [a for a in e.equity_anomalies(closed, at("2026-10-28T20:30:00")) if a["type"] == "move"]
        self.assertTrue(moves and not any(a["major"] for a in moves))
        self.assertTrue(found[("AAA", "move")]["key"].startswith("move:AAA:up:2026-10-28"))


class Universe(unittest.TestCase):
    def test_select_universe_and_daily_bars(self):
        meta = {f"S{i}": {"ticker": f"S{i}", "kind": "stock", "ref_turnover": float(i)} for i in range(10)}
        meta.update({"E1": {"ticker": "E1", "kind": "etf", "ref_turnover": 5.0},
                     "SPY": {"ticker": "SPY", "kind": "etf", "ref_turnover": None}})
        rows = e.select_universe(meta, set(meta) - {"S9"}, n_stocks=3, n_etfs=5)
        self.assertEqual([r["ticker"] for r in rows], ["S8", "S7", "S6", "E1", "SPY"])
        self.assertIsNone(e.daily_bar("NVDA", tick(status="open"), "2026-10-27"))
        self.assertIsNone(e.daily_bar("NVDA", tick(status="after"), "2026-10-27"))
        bar = e.daily_bar("NVDA", e.parse_tick(NVDA_PRE), "2026-10-27")
        self.assertAlmostEqual(bar["close"], 217.94)
        hist = e.history_bars("NVDA", [{"date": "10/27/2026", "close": "$217.94", "volume": "84,799,700"},
                                        {"date": "bad", "close": "$1", "volume": "1"}])
        self.assertEqual([h["date"] for h in hist], ["2026-10-27"])

    def test_session_dates_and_extended_hours(self):
        pre = {"NVDA": e.parse_tick(NVDA_PRE)}
        s = e.session_info(INFO, pre, at("2026-10-28T04:30:00"))
        self.assertEqual((s["phase"], s["date"], s["unverified"]), ("pre", "2026-10-27", False))
        live = e.session_info(INFO, {"A": tick()}, at("2026-10-28T11:00:00"))
        self.assertEqual((live["phase"], live["date"], live["as_of"]), ("open", "2026-10-28", "11:00"))
        self.assertEqual(e.completed_session_date(INFO, at("2026-10-28T17:00:00")), "2026-10-28")
        self.assertTrue(e.extended_hours(INFO, at("2026-10-28T04:00:00")))
        self.assertFalse(e.extended_hours(INFO, at("2026-10-28T20:00:00")))
        self.assertTrue(e.extended_hours({}, 0))


if __name__ == "__main__":
    unittest.main()
