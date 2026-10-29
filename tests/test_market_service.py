import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import market_report as m
import market_store as store
from fake_market import SLOT, FakeMarket


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + "/market.sqlite"
        self.con = store.connect(self.path)
        self.sent = []
        self.fail_on = set()
        self.telegram_down = False
        self.make()

    def make(self, market=None):
        self.market = market or FakeMarket()
        self.service = m.MarketService(self.con, self.send, fetch=self.market, sleep=lambda _s: None)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def send(self, text, silent=False):
        if self.telegram_down:
            raise m.TelegramError("telegram network failure")
        if text in self.fail_on:
            self.fail_on.discard(text)
            raise m.TelegramError("telegram HTTP 502")
        self.sent.append((text, silent))

    def texts(self):
        return [t for t, _ in self.sent]

    def test_first_tick_collects_everything_and_broadcasts_three_parts(self):
        self.service.tick(SLOT + 60)
        jobs = {j["name"]: j for j in store.jobs_health(self.con)}
        self.assertEqual(len(jobs), 15)
        self.assertEqual([n for n, j in jobs.items() if j["last_error"]], [])
        texts = self.texts()
        self.assertEqual(len(texts), 3)
        self.assertIn("1/3 · 加密", texts[0])
        self.assertIn("主动买入", texts[0])
        self.assertIn("BTC 永续", texts[0])
        self.assertIn("2/3 · 美股", texts[1])
        self.assertIn("盘前 · 展示 10-27 收盘数据", texts[1])
        self.assertIn("细分行业流入：半导体", texts[1])
        self.assertIn("类别资金：", texts[1])
        self.assertIn("3/3 · 宏观与研判", texts[2])
        self.assertIn("美股500", texts[2])
        self.assertEqual(store.broadcast_get(self.con, SLOT)["outcome"], "sent")
        self.assertFalse(any(silent for _, silent in self.sent))          # 12:00 Beijing: not quiet hours
        self.assertIn("🔎 <b>S1099</b>", store.quote_messages(self.path, "s1099", SLOT + 120)[0])
        self.assertIn("🔎 <b>BTC</b>", store.quote_messages(self.path, "BTC", SLOT + 120)[0])
        self.assertEqual(store.meta_get(self.con, "etf_sweep_done"), str(SLOT + 60))

    def test_no_resend_and_refresh_every_15_minutes(self):
        self.service.tick(SLOT + 60)
        self.service.tick(SLOT + 120)
        self.service.tick(SLOT + 60 + 900)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 2)

    def test_night_brief_is_silent(self):
        night = SLOT - 8 * 3600                               # 04:00 Beijing
        self.service.tick(night + 60)
        self.assertTrue(self.sent and all(silent for _, silent in self.sent))

    def test_equity_outage_keeps_labelled_data_then_placeholder(self):
        self.service.tick(SLOT + 60)
        self.market.down.add("api.nasdaq.com")
        snap = self.service.build_snapshot(SLOT + 1800)
        self.assertEqual(snap["status"]["equity"], "ok")      # last quotes (fetched at SLOT+60) still < 3h old
        self.service.collector.run_due(SLOT + 4 * 3600)
        self.assertIn("equity_live", [j["name"] for j in store.jobs_health(self.con) if j["last_error"]])
        later = self.service.build_snapshot(SLOT + 4 * 3600)
        self.assertIn("暂不可用", later["sections"]["equity_top"])   # quotes > 3h old, last good > 1h old
        self.assertEqual(later["status"]["crypto"], "ok")
        self.assertIn("美股行情", store.status_line(self.path, SLOT + 4 * 3600))

    def test_all_sources_down_retries_then_sends_one_notice(self):
        self.market.down.update({"api.coinbase.com", "api.nasdaq.com"})
        self.service.tick(SLOT + 60)
        self.assertEqual(self.sent, [])
        self.assertIsNone(store.broadcast_get(self.con, SLOT))
        self.service.tick(SLOT + 2200)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("本期数据获取失败", self.texts()[0])
        self.assertEqual(store.broadcast_get(self.con, SLOT)["outcome"], "failed")

    def test_resume_sends_remaining_parts_only(self):
        store.broadcast_start(self.con, SLOT, SLOT + 60, None, ["part-1", "part-2", "part-3"])
        store.broadcast_progress(self.con, SLOT, 1)                      # part-1 already delivered
        self.fail_on.add("part-3")
        self.service.tick(SLOT + 60)
        self.assertEqual(self.texts(), ["part-2"])
        self.assertEqual(store.broadcast_get(self.con, SLOT)["parts_sent"], 2)
        self.service.tick(SLOT + 60 + 300)
        self.assertEqual(self.texts(), ["part-2", "part-3"])                # same build, never mixed
        self.assertEqual(store.broadcast_get(self.con, SLOT)["outcome"], "sent")

    def test_unsent_brief_is_rebuilt_from_fresh_data(self):
        self.market.down.add("api.nasdaq.com")
        self.telegram_down = True
        self.service.tick(SLOT + 60)                                       # built without US stocks, not delivered
        stored = store.broadcast_get(self.con, SLOT)
        self.assertEqual((stored["parts_sent"], stored["outcome"]), (0, "sending"))
        self.assertIn("暂不可用", stored["messages"][1])
        self.market.down.clear()
        self.telegram_down = False
        self.service.tick(SLOT + 60 + 300)
        texts = self.texts()
        self.assertEqual(len(texts), 3)
        self.assertIn("盘前 · 展示 10-27 收盘数据", texts[1])               # rebuilt from the recovered data
        self.assertNotIn("暂不可用", texts[1])
        self.assertEqual(store.broadcast_get(self.con, SLOT)["outcome"], "sent")

    def test_live_session_major_anomalies_pushed_once(self):
        self.make(FakeMarket(status="Market Open", change=15.0, volume=30_000_000))
        live_now = SLOT + 11 * 3600                          # 11:00 ET, 23:00 Beijing
        self.service.tick(live_now)
        pushes = [t for t in self.texts() if "异动提醒" in t]
        self.assertEqual(len(pushes), 1)
        lines = [l for l in pushes[0].splitlines() if l.startswith("• ")]
        self.assertTrue(0 < len(lines) <= 6)
        symbols = [l[2:].removeprefix("ETF ").split()[0] for l in lines]
        self.assertEqual(len(set(symbols)), len(lines))                       # one line per symbol
        self.assertTrue(all("+15.0%" in l for l in lines))
        self.service.build_snapshot(live_now + 900)
        self.assertEqual(len([t for t in self.texts() if "异动提醒" in t]), 1)
        self.assertGreater(self.con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 1)


class TelegramSenderTests(unittest.TestCase):
    TOKEN = "123:SECRET"

    def http_error(self, code, body=b"{}"):
        return urllib.error.HTTPError("https://api.telegram.org/bot" + self.TOKEN + "/sendMessage",
                                      code, "err", {}, io.BytesIO(body))

    def opener(self, outcomes, seen):
        def open_(req, timeout=20):
            seen.append(dict(p.split("=", 1) for p in req.data.decode().split("&")))
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return io.BytesIO(json.dumps({"ok": True}).encode())
        return open_

    def test_rate_limit_waits_retry_after_and_silent_flag(self):
        waits, seen = [], []
        send = m.telegram_sender(self.TOKEN, "42", sleep=waits.append, opener=self.opener(
            [self.http_error(429, b'{"parameters":{"retry_after":3}}'), "ok"], seen))
        send("<b>x</b>", silent=True)
        self.assertEqual((waits, len(seen)), ([3], 2))
        self.assertEqual(seen[1]["disable_notification"], "true")

    def test_bad_html_falls_back_to_plain_text(self):
        seen = []
        send = m.telegram_sender(self.TOKEN, "42", sleep=lambda _s: None,
                                 opener=self.opener([self.http_error(400), "ok"], seen))
        send("<b>A &amp; B</b>")
        self.assertIn("parse_mode", seen[0])
        self.assertNotIn("parse_mode", seen[1])
        self.assertEqual(urllib.parse.unquote_plus(seen[1]["text"]), "A & B")

    def test_errors_never_leak_token(self):
        send = m.telegram_sender(self.TOKEN, "42", sleep=lambda _s: None,
                                 opener=self.opener([self.http_error(502)] * 3, []))
        with self.assertRaises(m.TelegramError) as ctx:
            send("x")
        self.assertNotIn("SECRET", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
