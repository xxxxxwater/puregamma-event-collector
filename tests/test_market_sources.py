import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import market_sources as s


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Paging(unittest.TestCase):
    def test_trades_walk_back_dedupe_and_page_cap(self):
        trades = [{"trade_id": str(i), "time": iso(1000 + i * 0.5), "price": "1", "size": "1", "side": "BUY"}
                  for i in range(2500)]                       # 2.5 trades/s over [1000, 2250)
        calls = []

        def fetch(url, params):
            calls.append(params["end"])
            inside = [t for t in trades if params["start"] <= s.iso_ts(t["time"]) < params["end"]]
            return {"trades": sorted(inside, key=lambda t: -s.iso_ts(t["time"]))[:1000]}

        got, covered_from = s.coinbase_trades("BTC-USD", 1000, 2250, max_pages=10, fetch=fetch)
        self.assertEqual(len(got), 2500)
        self.assertEqual(covered_from, 1000.0)
        capped, covered_from = s.coinbase_trades("BTC-USD", 1000, 2250, max_pages=2, fetch=fetch)
        self.assertLess(len(capped), 2500)
        self.assertGreater(covered_from, 1000)

    def test_candles_chunk_through_cap(self):
        seen = []

        def fetch(url, params):
            seen.append((params["start"], params["end"]))
            return {"candles": [{"start": str(t)} for t in range(params["start"], params["end"] + 1, 3600)]}

        rows = s.coinbase_candles("BTC-USD", "ONE_HOUR", 0, 800 * 3600, fetch=fetch)
        self.assertEqual(len(rows), 800)
        self.assertEqual(len(seen), 3)
        self.assertEqual(rows[0]["start"], "0")

    def test_watchlist_batches_of_20_and_failure_budget(self):
        calls = []

        def fetch(url, params, headers=None, timeout=None):
            calls.append(params["symbol"])
            if len(calls) == 2:
                raise s.SourceError("HTTP 503")
            return {"data": [{"symbol": x.split("|")[0].upper()} for x in params["symbol"]]}

        rows = s.nasdaq_watchlist([(f"t{i}", "stocks") for i in range(45)], fetch=fetch, sleep=lambda _x: None)
        self.assertEqual([len(c) for c in calls], [20, 20, 5])
        self.assertEqual(len(rows), 25)
        with self.assertRaises(s.SourceError):
            s.nasdaq_watchlist([(f"t{i}", "etf") for i in range(45)], fetch=lambda *a, **k: (_ for _ in ()).throw(
                s.SourceError("down")), sleep=lambda _x: None)


if __name__ == "__main__":
    unittest.main()
