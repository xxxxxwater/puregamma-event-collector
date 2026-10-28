import json
import smtplib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import market_email as em
import market_report as m
import market_store as store
from fake_market import SLOT, FakeMarket

ZH_DUE = SLOT - 4 * 3600 + 30 * 60      # 00:30 UTC = 08:30 Beijing
EN_DUE = SLOT + 4 * 3600 + 30 * 60      # 08:30 UTC
RECIPIENTS = [{"email": "zh1@example.com", "locale": "zh", "timezone": "Asia/Shanghai", "local_time": "08:30"},
              {"email": "zh2@example.com", "locale": "zh", "timezone": "Asia/Shanghai", "local_time": "08:30"},
              {"email": "en1@example.com", "locale": "en", "timezone": "UTC", "local_time": "08:30"}]


class EmailFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = store.connect(self.tmp.name + "/market.sqlite")
        self.mails, self.fail_for = [], {}
        self.service = m.MarketService(self.con, lambda *a, **k: None, fetch=FakeMarket(), sleep=lambda _s: None,
                                       email_send=self.mail, email_recipients=lambda: em.load_recipients(None, ())
                                       or [dict(r) for r in RECIPIENTS])

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def mail(self, to, subject, body, text):
        if self.fail_for.get(to):
            self.fail_for[to] -= 1
            raise em.EmailError("SMTP 发送失败（ConnectionResetError）")
        self.mails.append((to, subject, body, text))

    def snapshot(self, now=ZH_DUE + 60):
        self.service.collector.run_due(now)
        return self.service.build_snapshot(now)


class RenderTests(EmailFixture):
    def test_chinese_render_is_email_safe_and_complete(self):
        subject, body, text = em.Renderer(None, "zh").render(self.snapshot())
        self.assertTrue(subject.startswith("市场简报 10-28 08:31"))
        self.assertIn("美股资金", subject)
        for part in ("今日要点", "加密资金与 Top 10", "加密衍生品", "美股概览与 Top 20 个股", "行业资金", "ETF 资金",
                     "跨资产", "市场研判", "不构成投资建议", "如需退订"):
            self.assertIn(part, body)
        for banned in ("<script", "<img", "cid:", "http://", "https://", "/alerts", "/market", "Telegram"):
            self.assertNotIn(banned, body)
        self.assertIn("#d92d20", body)                     # red = up for Chinese readers
        self.assertNotIn("<", text)

    def test_english_render_translates_everything(self):
        subject, body, text = em.Renderer(None, "en").render(self.snapshot())
        self.assertTrue(subject.startswith("Market Brief Oct 28 00:31 UTC"))
        self.assertIn("US stocks net", subject)
        visible = __import__("html").unescape(body)
        for part in ("Today's highlights", "Crypto flows & Top 10", "Sector flows", "Technology", "Semiconductors",
                     "Bonds", "Gold", "US 500", "Market outlook", "not investment advice", "To unsubscribe"):
            self.assertIn(part, visible)
        self.assertIsNone(__import__("re").search(r"[一-鿿]", body + subject + text))   # no Chinese left
        self.assertIn("$", body)
        self.assertEqual(em.Renderer(None, "en").tone(1.0), "#079455")                         # green = up

    def test_money_and_escaping(self):
        self.assertEqual([em.money_en(v) for v in (1.49e11, 2.3e10, 9.53e8, 4.87e7, 1.2e6, 12_300, 950, None)],
                         ["$149B", "$23.0B", "$953M", "$48.7M", "$1.20M", "$12.3K", "$950", "-"])
        self.assertEqual(em.money_en(-8.4e6, True), "-$8.40M")
        snap = self.snapshot()
        snap["metrics"]["crypto"]["digest"]["top"][0][0] = "<X&Y>"
        _, body, _ = em.Renderer(None, "en").render(snap)
        self.assertIn("&lt;X&amp;Y&gt;", body)

    def test_missing_and_reused_parts_are_labelled(self):
        snap = self.snapshot()
        snap["metrics"].pop("equity")
        snap["status"]["equity"] = "HTTP 503 from api.nasdaq.com"
        snap["status"]["crypto"] = "reused: HTTP 503 from api.coinbase.com"
        snap["metrics"]["crypto"]["reused_from"] = ZH_DUE - 900
        _, zh, _ = em.Renderer(None, "zh").render(snap)
        self.assertIn("暂不可用：HTTP 503 from api.nasdaq.com", zh)
        self.assertIn("以下沿用 08:15 的数据", zh)
        _, en, _ = em.Renderer(None, "en").render(snap)
        self.assertIn("Unavailable: HTTP 503", en)
        self.assertIn("showing data from 00:15", en)


class RecipientTests(unittest.TestCase):
    def test_load_recipients_validates_and_dedupes(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump([{"email": "A@x.com", "locale": "en"}, {"email": "a@x.com"}, {"email": "bad"},
                       {"email": "c@x.com", "timezone": "Mars/Base", "local_time": "25"},
                       {"email": "d@x.com", "enabled": False}], f)
        rows = em.load_recipients(f.name, ["e@x.com"], "zh", "08:30")
        self.assertEqual([r["email"] for r in rows], ["A@x.com", "c@x.com", "e@x.com"])
        self.assertEqual((rows[0]["locale"], rows[0]["timezone"]), ("en", "UTC"))
        self.assertEqual((rows[1]["timezone"], rows[1]["local_time"]), ("Asia/Shanghai", "08:30"))
        self.assertEqual(em.load_recipients("/nonexistent", ()), [])

    def test_due_today_window(self):
        zh = RECIPIENTS[0]
        self.assertIsNone(em.due_today(zh, ZH_DUE - 60, 2700))
        key, date, due = em.due_today(zh, ZH_DUE + 60, 2700)
        self.assertEqual((date, due), ("2026-10-28", ZH_DUE))
        self.assertNotIn("zh1@", key)                                     # addresses are stored only hashed
        self.assertIsNone(em.due_today(zh, ZH_DUE + 2701, 2700))


class SchedulingTests(EmailFixture):
    def test_each_recipient_once_per_local_day_in_own_language(self):
        self.service.tick(ZH_DUE + 60)
        self.assertEqual([to for to, *_ in self.mails], ["zh1@example.com", "zh2@example.com"])
        self.assertTrue(all(subject.startswith("市场简报") for _, subject, _, _ in self.mails))
        self.service.tick(ZH_DUE + 600)
        self.assertEqual(len(self.mails), 2)                               # no duplicate the same day
        self.service.tick(EN_DUE + 60)
        self.assertEqual(self.mails[-1][0], "en1@example.com")
        self.assertTrue(self.mails[-1][1].startswith("Market Brief"))
        self.assertEqual(len(self.mails), 3)
        self.service.tick(ZH_DUE + 86400 + 60)
        self.assertEqual(len(self.mails), 5)                               # next day: again once each
        self.assertEqual(store.email_stats(self.con, 0), {"sent": 5})

    def test_failed_recipient_retried_alone_then_given_up(self):
        self.fail_for = {"zh2@example.com": 99}
        self.service.tick(ZH_DUE + 60)
        self.assertEqual([to for to, *_ in self.mails], ["zh1@example.com"])
        self.service.tick(ZH_DUE + 60 + 300)
        self.assertEqual([to for to, *_ in self.mails], ["zh1@example.com"])  # zh1 not re-sent
        self.service.tick(ZH_DUE + 2400)
        stats = store.email_stats(self.con, 0)
        self.assertEqual((stats.get("sent"), stats.get("failed")), (1, 1))

    def test_transient_failure_recovers(self):
        self.fail_for = {"zh1@example.com": 1}
        self.service.tick(ZH_DUE + 60)
        self.service.tick(ZH_DUE + 60 + 300)
        self.assertEqual(sorted(to for to, *_ in self.mails), ["zh1@example.com", "zh2@example.com"])

    def test_missed_window_is_not_sent_late(self):
        self.service.tick(ZH_DUE + 2700 + 60)
        self.assertEqual(self.mails, [])

    def test_status_line_reports_email(self):
        self.service.tick(ZH_DUE + 60)
        self.assertIn("✉️ 邮件简报：近 24 小时已发 2 封", store.status_line(self.tmp.name + "/market.sqlite", ZH_DUE + 120))


class SmtpTests(unittest.TestCase):
    class FakeSMTP:
        def __init__(self, log, failures):
            self.log, self.failures = log, failures

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, user, password):
            self.log.append(("login", user))
            if password == "bad":
                raise smtplib.SMTPAuthenticationError(535, b"auth failed")

        def send_message(self, message):
            if self.failures:
                self.failures.pop()
                raise smtplib.SMTPServerDisconnected("reset")
            self.log.append(("send", message["To"], message["Subject"], message["Reply-To"],
                             [p.get_content_type() for p in message.iter_parts()]))

    def sender(self, password="secret-pass", failures=None, port=587, reply_to=None):
        log, modes, waits = [], [], []

        def factory(mode):
            modes.append(mode)
            return self.FakeSMTP(log, failures if failures is not None else [])
        send = em.smtp_sender("smtp.example.com", port, "bot@example.com", password, "PureGamma AI <bot@example.com>",
                              reply_to=reply_to, sleep=waits.append, smtp_factory=factory)
        return send, log, modes, waits

    def test_one_recipient_per_message_over_starttls(self):
        send, log, modes, _ = self.sender(reply_to="team@example.com")
        send("a@example.com", "主题", "<b>html</b>", "text")
        self.assertEqual(modes, ["starttls"])
        self.assertEqual(log[-1], ("send", "a@example.com", "主题", "team@example.com", ["text/plain", "text/html"]))

    def test_ssl_for_465_and_transient_retry(self):
        send, log, modes, waits = self.sender(failures=[1], port=465)
        send("a@example.com", "s", "<p>h</p>", "t")
        self.assertEqual((modes, waits, log[-1][0]), (["ssl", "ssl"], [5], "send"))

    def test_auth_error_is_sanitized(self):
        send, _, _, _ = self.sender(password="bad")
        with self.assertRaises(em.EmailError) as ctx:
            send("a@example.com", "s", "h", "t")
        self.assertNotIn("bad", str(ctx.exception))
        self.assertIn("认证失败", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
