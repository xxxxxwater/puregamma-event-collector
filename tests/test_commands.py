import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import bot_commands as commands


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + "/events.db"
        with sqlite3.connect(self.path) as con:
            con.executescript("""
                CREATE TABLE sources (id TEXT PRIMARY KEY,seeded INTEGER,last_ok INTEGER,last_error TEXT);
                CREATE TABLE events (id TEXT PRIMARY KEY,title TEXT,source TEXT,link TEXT,priority TEXT);
                CREATE TABLE outbox (id INTEGER PRIMARY KEY,event_id TEXT,status TEXT,sent_at INTEGER);
                CREATE TABLE translation_cache (input_hash TEXT PRIMARY KEY,text_zh TEXT);
            """)
            con.execute("INSERT INTO sources VALUES ('official',1,1792800000,NULL)")
            for i in range(12):
                con.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                            (str(i), f"Headline {i} <test>", "Official", "https://example.com/" + str(i), "P2"))
                con.execute("INSERT INTO outbox VALUES (?,?,?,?)",
                            (i + 1, str(i), "sent" if i != 11 else "pending", 1792800000 + i))
            import hashlib
            title = "Headline 10 <test>"
            key = hashlib.sha256(("en-zh-v1|" + title).encode()).hexdigest()
            con.execute("INSERT INTO translation_cache VALUES (?,?)", (key, "第十条新闻"))
        self.now = 1792800010
        self.sources = [{"id": "official", "interval_seconds": 60}]
        self.heartbeat = {"started": 1792799000, "last_tick": self.now - 2}

    def tearDown(self):
        self.tmp.cleanup()

    def update(self, chat_id, text, date=None):
        return {"update_id": 1, "message": {
            "message_id": 9, "date": self.now if date is None else date,
            "chat": {"id": chat_id}, "text": text
        }}

    def test_news_ago_returns_ten_sent_items_newest_first(self):
        response = commands.command_reply(
            self.update(123, "/news ago"), "123", self.path, self.heartbeat, self.sources, self.now
        )
        self.assertEqual(response[:2], (123, 9))
        body = response[2]
        self.assertEqual(body.count("P2 · "), 10)
        self.assertIn("第十条新闻", body)
        self.assertNotIn("Headline 11", body)
        self.assertNotIn("Headline 0", body)
        self.assertIn("Headline 9 &lt;test&gt;", body)

    def test_logs_is_on_demand_and_read_only(self):
        response = commands.command_reply(
            self.update(123, "/logs"), "123", self.path, self.heartbeat, self.sources, self.now
        )
        self.assertIn("最近循环：2 秒前", response[2])
        self.assertIn("订阅源：1/1", response[2])
        self.assertIn("已发 11 · 待发 1", response[2])
        self.assertIsNone(commands.command_reply(
            self.update(999, "/logs"), "123", self.path, self.heartbeat, self.sources, self.now
        ))
        self.assertIsNone(commands.command_reply(
            self.update(123, "/delete"), "123", self.path, self.heartbeat, self.sources, self.now
        ))

    def test_poller_ignores_old_and_unauthorized_commands(self):
        stop = threading.Event()
        replies = []
        old = self.update(123, "/logs", self.now - 1)
        old["update_id"] = 1
        foreign = self.update(999, "/logs")
        foreign["update_id"] = 2
        fresh = self.update(123, "/news ago")
        fresh["update_id"] = 3

        def api(_token, method, fields, timeout=35):
            if method == "getUpdates":
                return [old, foreign, fresh]
            if method == "getMe":
                return {"username": "pg_news_bot"}
            if method == "setMyCommands":
                return True
            replies.append(fields)
            stop.set()
            return {"message_id": 10}

        with patch.object(commands, "telegram_api", side_effect=api):
            commands.run_command_poller(
                "redacted", "123", self.path, self.heartbeat, threading.Lock(),
                self.sources, stop, lambda: self.now
            )
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["reply_to_message_id"], 9)
        self.assertIn("最近 10 条", replies[0]["text"])


if __name__ == "__main__":
    unittest.main()
