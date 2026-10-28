import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import market_report as m
import market_store as store
from fake_market import SLOT, FakeMarket

MANAGE = {"zh": "这是你的 PureGamma AI 账户的每日简报。如需修改发送时间或关闭，请登录 puregamma.ai，在「每日推送」中设置。",
          "en": "This is the daily brief for your PureGamma AI account. To change the time or turn it off, "
                "sign in at puregamma.ai and open Daily Push."}


class BriefServerTests(unittest.TestCase):
    """The headless market-brief sidecar: collect, keep snapshots fresh, serve the rendered e-mail."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + "/market.sqlite"
        self.con = store.connect(self.path)
        self.now = SLOT + 60
        self.service = m.MarketService(self.con, None, fetch=FakeMarket(), sleep=lambda _s: None)
        self.server = m.BriefServer(self.path, manage=MANAGE, wordmark="PureGamma AI", clock=lambda: self.now)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_headless_service_collects_but_never_broadcasts(self):
        self.service.tick(self.now)
        self.assertIsNone(store.broadcast_get(self.con, SLOT))
        self.assertIsNotNone(store.latest_snapshot(self.con))

    def test_brief_is_latest_snapshot_rendered_with_account_footer(self):
        self.service.tick(self.now)
        self.now += 120
        code, data = self.server.brief("zh")
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["snapshot_id"], store.latest_snapshot(self.con)["id"])
        self.assertEqual(data["age_seconds"], 120)
        self.assertTrue(data["subject"].startswith("市场简报"))
        self.assertEqual(set(data["parts"].values()), {"ok"})
        for body in (data["html"], data["text"]):
            self.assertIn("PureGamma AI", body)
            self.assertIn("「每日推送」中设置", body)
            self.assertNotIn("回复本邮件", body)
        code, en = self.server.brief("en")
        self.assertEqual(code, 200)
        self.assertTrue(en["subject"].startswith("Market Brief"))
        self.assertIn("open Daily Push", en["html"])
        self.assertEqual(self.server.brief("fr")[0], 400)

    def test_rendering_is_cached_per_snapshot(self):
        self.service.tick(self.now)
        first = self.server.brief("zh")[1]
        self.assertIs(self.server.cache[(first["snapshot_id"], "zh")][1], first["html"])
        self.now += 16 * 60
        self.service.tick(self.now)                      # refresh builds a new snapshot
        second = self.server.brief("zh")[1]
        self.assertGreater(second["snapshot_id"], first["snapshot_id"])
        self.assertEqual(list(self.server.cache), [(second["snapshot_id"], "zh")])

    def test_no_usable_snapshot_is_503(self):
        self.assertEqual(m.BriefServer(self.tmp.name + "/missing.sqlite").brief("zh")[0], 503)
        self.assertEqual(self.server.brief("zh"), (503, {"ok": False, "error": "no snapshot yet"}))
        failed = {"crypto": "HTTP 503", "deriv": "HTTP 503", "equity": "HTTP 503", "forecast": "HTTP 503"}
        store.save_snapshot(self.con, self.now, {}, {}, failed)
        code, data = self.server.brief("en")
        self.assertEqual((code, data["error"]), (503, "all market sources failed"))

    def test_partial_snapshot_reports_parts(self):
        self.service.tick(self.now)
        snapshot = store.latest_snapshot(self.con)
        status = dict(snapshot["status"], equity="HTTP 503 from api.nasdaq.com")
        metrics = {k: v for k, v in snapshot["metrics"].items() if k != "equity"}
        store.save_snapshot(self.con, self.now + 60, snapshot["sections"], metrics, status)
        code, data = self.server.brief("en")
        self.assertEqual(code, 200)
        self.assertEqual(data["parts"]["equity"], "unavailable")
        self.assertIn("Unavailable: HTTP 503", data["html"])

    def test_http_roundtrip(self):
        self.service.tick(self.now)
        httpd = m.serve_briefs(self.server, 0, "127.0.0.1")
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/brief?locale=en", timeout=10) as resp:
                self.assertEqual(resp.headers["Content-Type"], "application/json; charset=utf-8")
                data = json.loads(resp.read())
            self.assertTrue(data["ok"] and data["subject"].startswith("Market Brief"))
            with urllib.request.urlopen(base + "/health", timeout=10) as resp:
                health = json.loads(resp.read())
            self.assertTrue(health["ok"])
            self.assertEqual(health["jobs_ok"], health["jobs_total"])
            self.assertGreater(health["jobs_total"], 0)
            for path, code in (("/brief?locale=fr", 400), ("/nope", 404)):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(base + path, timeout=10)
                self.assertEqual(ctx.exception.code, code)
            self.now += store.HEARTBEAT_MAX_AGE + 1
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(base + "/health", timeout=10)
            self.assertEqual(ctx.exception.code, 503)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
