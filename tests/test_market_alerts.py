import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import market_alerts as a
import market_store as store

DAY_NOON = 1_793_160_000      # 2026-10-28 12:00 Beijing
NIGHT = DAY_NOON - 10 * 3600  # 02:00 Beijing


def anomaly(key, major=True, severity=1.0):
    return {"key": key, "kind": "stock", "symbol": key, "severity": severity, "major": major, "text": key}


class Alerts(unittest.TestCase):
    def setUp(self):
        self.con = store.connect(":memory:")

    def test_dedupe_major_only_and_batching(self):
        push = a.plan(self.con, [anomaly("A"), anomaly("B", major=False), anomaly("C", severity=5)], DAY_NOON)
        self.assertEqual(set(push["keys"]), {"A", "C"})
        self.assertLess(push["text"].index("• C"), push["text"].index("• A"))   # most severe first
        self.assertFalse(push["silent"])
        self.assertIn("今日第 1/8 条", push["text"])
        store.mark_pushed(self.con, push["keys"], push["silent"])
        self.assertIsNone(a.plan(self.con, [anomaly("A"), anomaly("C")], DAY_NOON + 900))   # already recorded

    def test_minor_sighting_escalates_to_one_push(self):
        early = dict(anomaly("move:F", major=False), symbol="F", text="F 大跌 -8.9%")
        self.assertIsNone(a.plan(self.con, [early], DAY_NOON))                       # recorded, not pushed
        self.assertIsNone(a.plan(self.con, [early], DAY_NOON + 900))                 # still minor: nothing
        big = dict(early, major=True, severity=2.0, text="F 大跌 -15.1%")
        push = a.plan(self.con, [big], DAY_NOON + 1800)
        self.assertEqual(push["keys"], ["move:F"])
        self.assertIn("F 大跌 -15.1%", push["text"])
        store.mark_pushed(self.con, push["keys"], push["silent"])
        self.assertIsNone(a.plan(self.con, [big], DAY_NOON + 2700))                  # pushed once only
        row = self.con.execute("SELECT ts, severity, text FROM alerts WHERE key='move:F'").fetchone()
        self.assertEqual((row["ts"], row["severity"], row["text"]), (DAY_NOON + 1800, 2.0, "F 大跌 -15.1%"))
        self.assertEqual(store.pushed_since(self.con, a.beijing_midnight(DAY_NOON)), 1)

    def test_major_held_back_by_the_cap_is_not_retried(self):
        for i in range(2):
            store.mark_pushed(self.con, a.plan(self.con, [anomaly(f"K{i}")], DAY_NOON + i * 60, max_per_day=2)["keys"], False)
        self.assertIsNone(a.plan(self.con, [anomaly("LATE")], DAY_NOON + 300, max_per_day=2))
        self.assertIsNone(a.plan(self.con, [anomaly("LATE")], DAY_NOON + 1200, max_per_day=3))  # recorded as major

    def test_one_line_per_symbol_but_all_keys_marked(self):
        twin = [dict(anomaly("move:X"), symbol="X", severity=2.0), dict(anomaly("volume:X"), symbol="X", severity=3.0)]
        push = a.plan(self.con, twin, DAY_NOON)
        self.assertEqual(push["text"].count("• "), 1)
        self.assertEqual(set(push["keys"]), {"move:X", "volume:X"})

    def test_daily_cap_and_quiet_hours(self):
        for i in range(3):
            push = a.plan(self.con, [anomaly(f"K{i}")], DAY_NOON + i * 60, max_per_day=2)
            if push:
                store.mark_pushed(self.con, push["keys"], push["silent"])
        self.assertEqual(store.pushed_since(self.con, a.beijing_midnight(DAY_NOON)), 2)
        self.assertTrue(a.plan(self.con, [anomaly("Q")], NIGHT)["silent"])
        self.assertIsNone(a.plan(self.con, [anomaly("Z")], DAY_NOON, enabled=False))

    def test_quiet_hour_parsing(self):
        self.assertEqual(a.parse_hours("23-7"), (23, 7))
        self.assertEqual(a.parse_hours("bad"), (0, 8))
        self.assertTrue(a.in_quiet_hours(NIGHT, (23, 7)))
        self.assertFalse(a.in_quiet_hours(DAY_NOON, (23, 7)))


if __name__ == "__main__":
    unittest.main()
