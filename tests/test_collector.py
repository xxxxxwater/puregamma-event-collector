import sqlite3
import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
