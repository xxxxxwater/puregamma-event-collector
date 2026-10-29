import gc
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import warnings
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main as collector

OFFICIAL = {"id": "official", "name": "Official", "trust": "A", "default_priority": "P3", "interval_seconds": 60}


class ClassifyTests(unittest.TestCase):
    def test_chinese_keywords_match_inside_a_sentence(self):
        self.assertEqual(collector.classify("美联储宣布降息25个基点", OFFICIAL), ("macro.monetary", "P1"))
        self.assertEqual(collector.classify("某交易所遭攻击损失1亿美元", OFFICIAL), ("crypto.security", "P0"))
        self.assertEqual(collector.classify("比特币突破10万美元", OFFICIAL), ("crypto.market", "P3"))
        self.assertEqual(collector.classify("苹果发布财报", OFFICIAL), ("equities.earnings", "P1"))

    def test_english_keywords_still_need_word_boundaries(self):
        self.assertEqual(collector.classify("SEC approves bitcoin ETF", OFFICIAL), ("regulation", "P1"))
        self.assertEqual(collector.classify("Fedora release notes", OFFICIAL), ("general", "P3"))
        self.assertEqual(collector.classify("Ethics committee meets", OFFICIAL), ("general", "P3"))


class ParseFeedTests(unittest.TestCase):
    def test_rss2_item_fields(self):
        raw = b"""<rss version="2.0"><channel><title>c</title><item><title>Fed holds rates</title>
            <link>https://example.com/a</link><guid>g-1</guid><description>&lt;p&gt;Body&lt;/p&gt;</description>
            <pubDate>Wed, 28 Oct 2026 18:00:00 GMT</pubDate></item></channel></rss>"""
        [entry] = collector.parse_feed(raw)
        self.assertEqual(entry["title"], "Fed holds rates")
        self.assertEqual(entry["link"], "https://example.com/a")
        self.assertEqual(entry["guid"], "g-1")
        self.assertEqual(entry["summary"], "Body")
        self.assertEqual(entry["published"], 1793210400)

    def test_rss1_rdf_items_are_namespaced(self):
        raw = b"""<?xml version="1.0"?>
            <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
                     xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
              <channel><title>c</title></channel>
              <item><title>Hello world</title><link>https://example.com/b</link>
                <description>Text</description><dc:date>2026-10-28T18:00:00Z</dc:date></item>
            </rdf:RDF>"""
        [entry] = collector.parse_feed(raw)
        self.assertEqual(entry["title"], "Hello world")
        self.assertEqual(entry["link"], "https://example.com/b")
        self.assertEqual(entry["summary"], "Text")
        self.assertEqual(entry["published"], 1793210400)

    def test_atom_alternate_link_and_naive_date_is_utc(self):
        raw = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Release v1</title><id>tag:1</id>
            <link rel="self" href="https://example.com/self"/><link href="https://example.com/alt"/>
            <updated>2026-10-28T18:00:00</updated></entry></feed>"""
        [entry] = collector.parse_feed(raw)
        self.assertEqual(entry["link"], "https://example.com/alt")
        self.assertEqual(entry["guid"], "tag:1")
        self.assertEqual(entry["published"], 1793210400)

    def test_html_error_page_is_rejected(self):
        with self.assertRaises(ValueError):
            collector.parse_feed(b"<html><body>503</body></html>")


NOW = 1793210400
SETTINGS = {"max_notification_age_hours": 24, "max_p2_age_hours": 6}
NEWS = {"id": "news", "name": "News", "trust": "B", "default_priority": "P1", "interval_seconds": 60}


def entry(i, published=NOW - 60, title=None):
    return {"title": title or f"Distinct headline number {i} about markets", "link": f"https://t.me/news/{i}",
            "guid": "", "summary": "", "published": published}


class CollectorDb(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        collector.init_db(self.con, [OFFICIAL, NEWS])

    def tearDown(self):
        self.con.close()

    def scalar(self, sql, *args):
        return self.con.execute(sql, args).fetchone()[0]


class SeedingTests(CollectorDb):
    def test_first_non_empty_fetch_is_baseline_only(self):
        collector.ingest_result(self.con, NEWS, ([entry(i) for i in range(5)], "e1", None, False), NOW, SETTINGS)
        self.assertEqual(self.scalar("SELECT seeded FROM sources WHERE id='news'"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM outbox"), 0)
        collector.ingest_result(self.con, NEWS, ([entry(i) for i in range(6)], "e2", None, False), NOW + 60, SETTINGS)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM outbox"), 1)

    def test_empty_first_fetch_does_not_complete_baseline(self):
        collector.ingest_result(self.con, NEWS, ([], "empty-etag", None, False), NOW, SETTINGS)
        row = self.con.execute("SELECT seeded, etag FROM sources WHERE id='news'").fetchone()
        self.assertEqual((row["seeded"], row["etag"]), (0, None))
        backlog = [entry(i, published=NOW - 600 * i) for i in range(50)]
        collector.ingest_result(self.con, NEWS, (backlog, "e2", None, False), NOW + 60, SETTINGS)
        self.assertEqual(self.scalar("SELECT seeded FROM sources WHERE id='news'"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM outbox"), 0)

    def test_not_modified_before_seed_clears_validators(self):
        collector.ingest_result(self.con, NEWS, ([], "stale", "Wed", True), NOW, SETTINGS)
        row = self.con.execute("SELECT seeded, etag, modified FROM sources WHERE id='news'").fetchone()
        self.assertEqual(tuple(row), (0, None, None))


class FetchTests(unittest.TestCase):
    FEED = b"<rss><channel><item><title>Fed holds rates</title><link>https://example.com/a</link></item></channel></rss>"

    def serve(self, handler):
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)

        def run():
            conn, _ = listener.accept()
            with conn:
                conn.recv(65536)
                handler(conn)
        threading.Thread(target=run, daemon=True).start()
        return {"url": "http://127.0.0.1:%d/feed" % listener.getsockname()[1]}

    def test_reads_feed_and_validators(self):
        def handler(conn):
            conn.sendall(b"HTTP/1.1 200 OK\r\nETag: \"v1\"\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                         % len(self.FEED) + self.FEED)
        entries, etag, _modified, not_changed = collector.fetch(
            self.serve(handler), None, None, {"http_timeout_seconds": 5, "max_feed_bytes": 4096})
        self.assertEqual([e["title"] for e in entries], ["Fed holds rates"])
        self.assertEqual((etag, not_changed), ('"v1"', False))

    def test_body_over_limit_without_content_length_is_rejected(self):
        def handler(conn):
            conn.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + b" " * 5000)
        with self.assertRaisesRegex(ValueError, "max length"):
            collector.fetch(self.serve(handler), None, None, {"http_timeout_seconds": 5, "max_feed_bytes": 4096})

    def test_trickling_server_hits_total_deadline(self):
        def handler(conn):
            conn.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n<rss><channel>")
            try:
                for _ in range(40):
                    time.sleep(0.1)
                    conn.sendall(b" ")
            except OSError:
                pass
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            collector.fetch(self.serve(handler), None, None,
                            {"http_timeout_seconds": 1, "http_total_timeout_seconds": 0.5, "max_feed_bytes": 4096})
        self.assertLess(time.monotonic() - started, 2)


class OutboxTests(CollectorDb):
    def queue(self, event_id, priority, last_seen, status="pending"):
        self.con.execute("INSERT INTO events(id,title,canonical_title,source,link,category,priority,trust,published,discovered,last_seen)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (event_id, "Headline " + event_id, "headline", "News", "", "general", priority, "B", None, last_seen, last_seen))
        self.con.execute("INSERT INTO outbox(event_id,priority,status,due_at) VALUES (?,?,?,?)", (event_id, priority, status, last_seen))
        self.con.commit()

    def status(self, event_id):
        return self.scalar("SELECT status FROM outbox WHERE event_id=?", event_id)

    def test_stale_pending_notifications_expire_instead_of_sending(self):
        self.queue("fresh_p1", "P1", NOW - 23 * 3600)
        self.queue("stale_p1", "P1", NOW - 25 * 3600)
        self.queue("fresh_p2", "P2", NOW - 5 * 3600)
        self.queue("stale_p2", "P2", NOW - 7 * 3600)
        with patch.object(collector, "telegram_send", return_value=(True, 42, 0, None)) as send, \
                patch.object(collector.time, "sleep"):
            delivered = collector.process_outbox(self.con, "t:k", "1", NOW, max_age_h=24, p2_age_h=6)
        self.assertEqual(delivered, 2)
        self.assertEqual(send.call_count, 2)
        self.assertEqual([self.status(e) for e in ("fresh_p1", "stale_p1", "fresh_p2", "stale_p2")],
                         ["sent", "expired", "sent", "expired"])

    def test_failed_send_is_retried_later_then_expires(self):
        self.queue("evt", "P1", NOW - 60)
        with patch.object(collector, "telegram_send", return_value=(False, None, 60, "HTTP 400")), \
                patch.object(collector.time, "sleep"):
            collector.process_outbox(self.con, "t:k", "1", NOW)
            row = self.con.execute("SELECT status, attempts, due_at FROM outbox").fetchone()
            self.assertEqual(tuple(row), ("pending", 1, NOW + 60))
            collector.process_outbox(self.con, "t:k", "1", NOW + 25 * 3600)
        self.assertEqual(self.status("evt"), "expired")

    def test_cleanup_purges_old_expired_rows_and_their_events(self):
        old = NOW - 61 * 86400
        self.queue("expired_old", "P1", old, status="expired")
        self.queue("pending_old", "P1", old)
        with patch.object(collector, "utc_now", return_value=NOW):
            collector.cleanup(self.con, 60)
        self.assertIsNone(self.con.execute("SELECT 1 FROM events WHERE id='expired_old'").fetchone())
        self.assertEqual(self.status("pending_old"), "pending")


class HealthServerTests(unittest.TestCase):
    def test_status_reports_counts_and_closes_its_connection(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = tmp.name + "/events.db"
        con = collector.db_connect(path)
        collector.init_db(con, [NEWS])
        con.close()
        srv = collector.launch_health_server(path, 0, [NEWS])
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            with urllib.request.urlopen("http://127.0.0.1:%d/status" % srv.server_address[1], timeout=5) as resp:
                payload = json.load(resp)
            gc.collect()
        self.assertEqual((payload["events"], payload["pending"], [s["id"] for s in payload["sources"]]), (0, 0, ["news"]))
        self.assertFalse([w for w in caught if "unclosed database" in str(w.message)])


if __name__ == "__main__":
    unittest.main()
