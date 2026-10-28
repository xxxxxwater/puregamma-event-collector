import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import market_store as s

NOW = 1_793_165_000  # 2026-10-28 05:23 UTC


def sections(**extra):
    base = {k: f"<b>{k}</b> body" for k in s.SECTIONS}
    base.update(extra)
    return base


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + "/market.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_v1_runs_migrate_to_broadcasts(self):
        with sqlite3.connect(self.path) as con:
            con.execute("CREATE TABLE runs (slot INTEGER PRIMARY KEY, sent_at INTEGER, crypto_total REAL)")
            con.execute("INSERT INTO runs VALUES (1793160000, 1793160075, 9.2e8)")
        con = s.connect(self.path)
        row = s.broadcast_get(con, 1793160000)
        self.assertEqual((row["outcome"], row["finished_at"]), ("sent", 1793160075))
        self.assertIsNone(con.execute("SELECT 1 FROM sqlite_master WHERE name='runs'").fetchone())
        s.connect(self.path)  # idempotent

    def test_snapshot_round_trip_schema_and_pruning(self):
        con = s.connect(self.path)
        old = s.save_snapshot(con, NOW - 8 * 86400, sections(), {}, {})
        s.broadcast_start(con, 1, NOW, old, ["x"])           # still sending: must survive pruning
        stale = s.save_snapshot(con, NOW - 8 * 86400, sections(), {}, {})
        newest = s.save_snapshot(con, NOW, sections(), {"crypto": {"total_24h": 1}}, {"crypto": "ok"})
        ids = {r[0] for r in con.execute("SELECT id FROM snapshots")}
        self.assertEqual(ids, {old, newest})
        self.assertNotIn(stale, ids)
        snap = s.latest_snapshot(con)
        self.assertEqual((snap["id"], snap["metrics"]["crypto"]["total_24h"], snap["status"]["crypto"]),
                         (newest, 1, "ok"))
        con.execute("UPDATE snapshots SET schema=99")
        self.assertIsNone(s.latest_snapshot(con))

    def test_ticker_cache_union_halt_removal_and_partial_pages(self):
        con = s.connect(self.path)
        con.execute("INSERT INTO cb_equities(ticker,last_seen) VALUES ('OLD', ?), ('KEPT', ?)",
                    (NOW - 8 * 86400, NOW - 3600))

        def pages():
            yield {"equity_product_details": {"ticker": "new"}}
            yield {"equity_product_details": {"ticker": "KEPT", "liquidate_only": True}}
            raise RuntimeError("page 3 failed")

        with self.assertRaises(RuntimeError):
            s.merge_tickers(con, pages(), NOW)
        self.assertEqual(s.tradable_tickers(con, NOW), {"NEW"})

    def test_compose_splits_on_section_boundaries(self):
        parts = s.compose("H", ["a" * 2500, "b" * 2500, "c" * 10, "z" * 5000], limit=4000)
        self.assertEqual(len(parts), 2)
        self.assertTrue(parts[0].startswith("H\n\n" + "a"))
        self.assertTrue(parts[1].startswith("b"))
        self.assertIn("已省略", parts[1])
        self.assertTrue(all(len(p) <= 4000 for p in parts))

    def test_view_messages_missing_db_views_and_staleness(self):
        self.assertIn("还没有生成数据", s.view_messages(self.path + ".missing", "market", NOW)[0])
        con = s.connect(self.path)
        s.save_snapshot(con, NOW - 300, sections(), {}, {})
        crypto = s.view_messages(self.path, "crypto", NOW)[0]
        self.assertIn("crypto_top", crypto)
        self.assertNotIn("equity_top", crypto)
        self.assertIn("5 分钟前", crypto)
        self.assertNotIn("过期", crypto)
        self.assertIn("过期", s.view_messages(self.path, "flow", NOW + 7200)[0])

    def test_status_line(self):
        self.assertIn("未运行", s.status_line(self.path + ".missing", NOW))
        con = s.connect(self.path)
        s.meta_set(con, "heartbeat", NOW - 30)
        s.meta_set(con, "interval_hours", 4)
        s.save_snapshot(con, NOW - 120, sections(), {}, {})
        s.broadcast_start(con, 1793160000, 1793160010, 1, ["m"])
        s.broadcast_finish(con, 1793160000, "sent", 1793160075)
        line = s.status_line(self.path, NOW)
        self.assertIn("运行中", line)
        self.assertIn("快照 2 分钟前", line)
        self.assertIn("最近播报 10-28 12:01 已发送", line)
        self.assertIn("下期 16:00", line)
        self.assertIn("心跳超时", s.status_line(self.path, NOW + 3600))

    def test_schedule_labels(self):
        self.assertEqual(s.schedule_label(4), "北京 00/04/08/12/16/20 点")
        self.assertEqual(s.schedule_label(6), "北京 02/08/14/20 点")
        self.assertEqual(s.slot_start(NOW, 4), 1793160000)


class StoreV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + "/market.sqlite"
        self.con = s.connect(self.path)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_brief_groups_into_three_titled_messages(self):
        snap = {"taken_at": NOW, "sections": {k: f"[{k}]" for k in s.SECTIONS}}
        messages = s.brief_messages(snap, 4)
        self.assertEqual(len(messages), 3)
        self.assertIn("1/3 · 加密", messages[0])
        self.assertIn("[crypto_deriv]", messages[0])
        self.assertIn("2/3 · 美股", messages[1])
        self.assertIn("[etf]", messages[1])
        self.assertIn("3/3 · 宏观与研判", messages[2])
        self.assertNotIn("[etf]", messages[2])

    def test_quotes_lookup(self):
        s.replace_quotes(self.con, [{"symbol": "nvda", "kind": "stock", "text": "card-nvda"},
                                    {"symbol": "LINK", "kind": "crypto", "text": "card-link-c"},
                                    {"symbol": "LINK", "kind": "stock", "text": "card-link-s"}], NOW - 60)
        self.assertIn("card-nvda", s.quote_messages(self.path, "$nvda", NOW)[0])
        self.assertEqual(len(s.quote_messages(self.path, "link", NOW)), 2)
        self.assertIn("没有找到 ZZZ", s.quote_messages(self.path, "zzz", NOW)[0])

    def test_job_health_and_quality_in_status(self):
        s.job_record(self.con, "spot", NOW, True, 120, detail="404 assets")
        s.job_record(self.con, "equity_live", NOW, False, 900, error="HTTP 503 from api.nasdaq.com")
        s.meta_set(self.con, "quality_checks", '{"逐笔方向": {"ok": false}}')
        line = s.status_line(self.path, NOW)
        self.assertIn("数据源 1/2 正常", line)
        self.assertIn("美股行情（HTTP 503", line)
        self.assertIn("数据自检未通过：逐笔方向", line)
        s.job_record(self.con, "equity_live", NOW + 60, True, 800)
        row = {j["name"]: j for j in s.jobs_health(self.con)}["equity_live"]
        self.assertEqual((row["runs"], row["failures"], row["last_error"]), (2, 1, None))

    def test_baselines_flows_derivs_forecasts_and_prune(self):
        s.upsert_daily(self.con, [{"ticker": "A", "date": f"2026-10-{d:02d}", "close": 1, "change": 0, "volume": d,
                                   "turnover": d, "source": "t"} for d in range(1, 28)])
        self.assertAlmostEqual(s.volume_baselines(self.con, "2026-10-27")["A"], sum(range(7, 27)) / 20)
        self.assertEqual(s.volume_baselines(self.con, "2026-10-05"), {})
        s.add_flow(self.con, "BTC", {900: {"buy": 1.0, "sell": 2.0, "trades": 3, "covered": 600.0}})
        s.add_flow(self.con, "BTC", {900: {"buy": 1.0, "sell": 0.0, "trades": 1, "covered": 600.0}})
        self.assertEqual(s.flow_window(self.con, 0, 1800)["BTC"], {"buy": 2.0, "sell": 2.0, "trades": 4, "covered": 900.0})
        contract = {"product_id": "BIP", "root": "BTC", "asset_type": "crypto", "perp": True, "price": 1.0,
                    "index_price": 1.0, "funding": 0.0, "oi": 10.0, "oi_usd": 10.0, "chg24": 0.0, "volume24": 0.0,
                    "expiry": None}
        s.insert_deriv_ticks(self.con, NOW - 4 * 3600 + 300, [contract])
        self.assertEqual(s.deriv_near(self.con, NOW - 4 * 3600)["BIP"]["oi_contracts"], 10.0)
        self.assertEqual(s.deriv_near(self.con, NOW - 12 * 3600), {})
        row = {"created_at": NOW - 25 * 3600, "target": "BTC", "horizon_h": 24, "price": 1.0, "score": 30.0,
               "direction": 1, "sigma": 0.01, "low": 0.9, "high": 1.1, "factors": {"trend": 0.5}}
        self.assertTrue(s.add_forecast(self.con, row))
        self.assertFalse(s.add_forecast(self.con, row))
        due = s.due_forecasts(self.con, NOW)
        s.finish_forecast(self.con, due[0]["id"], NOW, 2.0, 1, 1)
        self.assertEqual(s.forecast_record(self.con, "BTC", 0), {"evaluated": 1, "directional": 1, "hits": 1, "in_range": 1})
        s.insert_deriv_ticks(self.con, NOW - 9 * 86400, [contract])
        self.assertGreaterEqual(s.prune(self.con, NOW), 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM deriv_ticks").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
