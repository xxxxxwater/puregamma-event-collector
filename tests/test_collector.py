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


if __name__ == "__main__":
    unittest.main()
