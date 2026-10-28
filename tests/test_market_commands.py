import contextlib
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import bot_commands as commands
import market_store as store

NOW = 1_793_165_000


class MarketCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.news_db = self.tmp.name + "/events.db"
        with contextlib.closing(sqlite3.connect(self.news_db)) as con:
            con.executescript("""
                CREATE TABLE sources (id TEXT PRIMARY KEY,seeded INTEGER,last_ok INTEGER,last_error TEXT);
                CREATE TABLE events (id TEXT PRIMARY KEY,title TEXT,source TEXT,link TEXT,priority TEXT);
                CREATE TABLE outbox (id INTEGER PRIMARY KEY,event_id TEXT,status TEXT,sent_at INTEGER);
            """)
        self.market_db = self.tmp.name + "/market_report.sqlite"
        con = store.connect(self.market_db)
        store.meta_set(con, "heartbeat", NOW - 20)
        store.meta_set(con, "interval_hours", 4)
        store.save_snapshot(con, NOW - 600, {k: f"[{k}]" for k in store.SECTIONS}, {}, {"crypto": "ok"})
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def reply(self, text, chat=123, bot_username="pg_news_bot", market_db=None):
        update = {"update_id": 1, "message": {"message_id": 9, "date": NOW, "chat": {"id": chat}, "text": text}}
        return commands.command_reply(update, "123", self.news_db, {"started": NOW - 99, "last_tick": NOW - 1},
                                      [], NOW, market_db_path=market_db or self.market_db, bot_username=bot_username)

    def test_views_select_sections(self):
        market = self.reply("/market")[2]
        self.assertIsInstance(market, list)
        self.assertIn("Coinbase 市场快照", market[0])
        self.assertIn("10 分钟前", market[0])
        joined = "\n".join(market)
        for key in store.SECTIONS:
            self.assertIn(f"[{key}]", joined)
        expected = {"/crypto": ("crypto_deriv", "equity_top"), "/deriv": ("macro", "crypto_top"),
                    "/stocks": ("equity_movers", "sectors"), "/sectors": ("sectors", "etf"),
                    "/etf": ("etf", "sectors"), "/alerts": ("equity_alerts", "crypto_flow"),
                    "/macro": ("macro", "forecast"), "/predict": ("forecast", "macro"),
                    "/flow": ("sectors", "equity_top")}
        for command, (present, absent) in expected.items():
            text = "\n".join(self.reply(command)[2])
            self.assertIn(f"[{present}]", text, command)
            self.assertNotIn(f"[{absent}]", text, command)

    def test_quote_lookup(self):
        con = store.connect(self.market_db)
        store.replace_quotes(con, [{"symbol": "NVDA", "kind": "stock", "text": "card-nvda"}], NOW - 60)
        con.close()
        self.assertIn("card-nvda", self.reply("/q nvda")[2][0])
        self.assertIn("用法：/q", self.reply("/q")[2])
        self.assertIn("没有找到 ZZZ", self.reply("/q zzz")[2][0])

    def test_addressing_and_authorization(self):
        self.assertIsNotNone(self.reply("/MARKET"))
        self.assertIsNotNone(self.reply("/market@PG_News_Bot"))
        self.assertIsNone(self.reply("/market@other_bot"))
        self.assertIsNone(self.reply("/market@pg_news_bot", bot_username=None))
        self.assertIsNone(self.reply("/market now"))
        self.assertIsNone(self.reply("/market", chat=999))
        self.assertIsNone(self.reply("market"))

    def test_help_logs_and_missing_market_db(self):
        help_text = self.reply("/help")[2]
        for name, _ in commands.COMMAND_MENU:
            if name != "help":
                self.assertIn("/" + name, help_text)
        self.assertIn("北京 00/04/08/12/16/20 点", help_text)
        logs = self.reply("/logs")[2]
        self.assertIn("RSS 进程心跳", logs)
        self.assertIn("📊 行情简报：运行中", logs)
        missing = self.tmp.name + "/none.sqlite"
        self.assertIn("还没有生成数据", self.reply("/market", market_db=missing)[2][0])
        self.assertIn("未运行", self.reply("/logs", market_db=missing)[2])

    def test_poller_registers_menu_and_sends_every_part(self):
        stop = threading.Event()
        calls = []
        update = {"update_id": 5, "message": {"message_id": 9, "date": NOW, "chat": {"id": 123}, "text": "/market"}}

        def api(_token, method, fields, timeout=35):
            calls.append((method, fields))
            if method == "getMe":
                return {"username": "pg_news_bot"}
            if method == "getUpdates":
                if any(m == "sendMessage" for m, _ in calls):
                    stop.set()
                    return []
                return [update]
            return True

        with patch.object(commands, "MARKET_DB_PATH", self.market_db), \
                patch.object(commands.market_store, "view_messages", return_value=["part-1", "part-2"]), \
                patch.object(commands, "telegram_api", side_effect=api):
            commands.run_command_poller("redacted", "123", self.news_db, {}, threading.Lock(), [], stop, lambda: NOW)
        methods = [m for m, _ in calls]
        self.assertEqual(methods[:2], ["getMe", "setMyCommands"])
        sends = [f for m, f in calls if m == "sendMessage"]
        self.assertEqual([f["text"] for f in sends], ["part-1", "part-2"])
        self.assertEqual(sends[0]["reply_to_message_id"], 9)
        self.assertNotIn("reply_to_message_id", sends[1])
        menu = dict(calls)["setMyCommands"]["commands"]
        self.assertIn('"command": "market"', menu)

    def test_poller_survives_read_timeouts(self):
        stop = threading.Event()
        stop.wait = lambda timeout=None: stop.is_set()  # skip the real back-off sleep
        calls = []

        def api(_token, method, fields, timeout=35):
            calls.append(method)
            if method in ("getMe", "setMyCommands"):
                raise TimeoutError("read timed out")
            if calls.count("getUpdates") == 1:
                raise TimeoutError("read timed out")
            stop.set()
            return []

        with patch.object(commands, "telegram_api", side_effect=api):
            commands.run_command_poller("redacted", "123", self.news_db, {}, threading.Lock(), [],
                                        stop, lambda: NOW)
        self.assertEqual(calls.count("getUpdates"), 2)


if __name__ == "__main__":
    unittest.main()
