import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import group_feed
import main as collector


class GroupFeedTests(unittest.TestCase):
    def test_feed_round_trips_through_collector_parser(self):
        items = [{"id": i, "text": f"Bitcoin ETF headline number {i} from the group", "date": 1793210400 + i}
                 for i in range(3)]
        entries = collector.parse_feed(group_feed.make_rss(items))
        self.assertEqual([e["guid"] for e in entries], ["watcherguruchat:2", "watcherguruchat:1", "watcherguruchat:0"])
        self.assertEqual(entries[0]["published"], 1793210402)

    def test_control_characters_do_not_break_the_feed(self):
        items = [{"id": 1, "text": "Bitcoin ETF \x08approved\x1b by SEC today\x00", "date": 1793210400},
                 {"id": 2, "text": "Ethereum upgrade goes live on mainnet now", "date": 1793210460}]
        entries = collector.parse_feed(group_feed.make_rss(items))
        self.assertEqual([e["title"] for e in entries],
                         ["Ethereum upgrade goes live on mainnet now", "Bitcoin ETF approved by SEC today"])


if __name__ == "__main__":
    unittest.main()
