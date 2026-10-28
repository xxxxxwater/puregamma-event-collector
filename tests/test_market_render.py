import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import market_analysis as a
import market_equities as e
import market_render as r

NOW = 1_793_174_400


def asset(symbol, volume, change, price=1.0):
    return {"symbol": symbol, "volume": volume, "change": change, "price": price, "prev_volume": volume}


def equity_overview():
    meta = {"NVDA": {"kind": "stock", "name": "NVIDIA", "sector": "Technology", "industry": "Semiconductors"},
            "XOM": {"kind": "stock", "name": "Exxon", "sector": "Energy", "industry": "Integrated oil Companies"},
            "QQQ": {"kind": "etf", "name": "Invesco QQQ", "category": e.BROAD},
            "TLT": {"kind": "etf", "name": "iShares 20+", "category": e.BOND}}
    ticks = {"NVDA": {"ticker": "NVDA", "status": "open", "last": 200.0, "chg_abs": 4.0, "chg_pct": 2.5,
                      "volume": 5e7, "prev_close": 196.0, "open": 197.0, "trade_ts": None},
             "XOM": {"ticker": "XOM", "status": "open", "last": 100.0, "chg_abs": -3.0, "chg_pct": -3.0,
                     "volume": 1e7, "prev_close": 103.0, "open": 102.0, "trade_ts": None},
             "QQQ": {"ticker": "QQQ", "status": "open", "last": 500.0, "chg_abs": 5.0, "chg_pct": 1.0,
                     "volume": 3e7, "prev_close": 495.0, "open": 496.0, "trade_ts": None},
             "TLT": {"ticker": "TLT", "status": "open", "last": 90.0, "chg_abs": -0.9, "chg_pct": -1.0,
                     "volume": 2e7, "prev_close": 90.9, "open": 90.5, "trade_ts": None}}
    rows = e.board(ticks, meta, {}, NOW)
    return rows, e.equity_overview(rows)


class Formats(unittest.TestCase):
    def test_units_and_signs(self):
        self.assertEqual(r.fmt_usd(658_815_000_000), "$6588亿")
        self.assertEqual(r.fmt_usd(20_792_000_000), "$207.9亿")
        self.assertEqual(r.fmt_usd(345_000_000), "$3.45亿")
        self.assertEqual(r.fmt_usd(72_430_000), "$7243万")
        self.assertEqual(r.fmt_usd(-8_443_000), "-$844.3万")
        self.assertEqual(r.fmt_usd(5e8, signed=True), "+$5.00亿")
        self.assertEqual(r.fmt_usd(None), "-")
        self.assertEqual([r.fmt_price(v) for v in (80990, 102.06, 0.9419, 4.73e-6, 0, None)],
                         ["80,990", "102.06", "0.942", "0.00000473", "0", "-"])
        self.assertEqual((r.fmt_pct(1.234), r.fmt_pct(-16.96, 1), r.fmt_pct(None)), ("+1.23%", "-17.0%", "-"))

    def test_tables_align_in_display_cells(self):
        html_table = r.table(("#", "ETF", "TYPE"), [("1", "QQQ", "宽基"), ("10", "SOXL", "杠杆/反向")], (2, 6, 10))
        lines = html_table.removeprefix("<pre>").removesuffix("</pre>").split("\n")
        self.assertEqual(len({r.display_width(line) for line in lines}), 1)


class CryptoSections(unittest.TestCase):
    def overview(self):
        return a.crypto_overview([]) | {"top": [asset("BTC", 3e8, 1.5, 80000), asset("<X>", 1e6, -2, 0.5)],
                                        "flow": a.flow_summary([asset("BTC", 3e8, 1.5), asset("<X>", 1e6, -2)]),
                                        "major_share": 99.0, "stables": [asset("USDT", 5e7, 0)]}

    def test_flow_with_taker_window_books_and_escaping(self):
        window = {"hours": 4, "end": NOW, "assets": 30, "total": 9e7, "inflow": 4e7, "outflow": 5e7, "net": -1e7,
                  "vs_prior": -30.6, "top_in": [{"symbol": "ETH", "net": 4.2e6}], "top_out": [{"symbol": "<X>", "net": -8.4e6}]}
        taker = {"buy": 6e6, "sell": 4e6, "net": 2e6, "buy_share": 60.0, "coverage": 0.3,
                 "rows": [{"symbol": "BTC", "buy_share": 61.0}]}
        text = r.crypto_flow(self.overview(), window, taker=taker, books={"BTC": {"ratio": 1.25}})
        self.assertIn("主动买入 $600.0万 / 主动卖出 $400.0万 → 净买入 <b>$200.0万</b>", text)
        self.assertIn("覆盖率 30%，数据积累中", text)
        self.assertIn("量价净流出 <b>$1000万</b>", text)
        self.assertIn("流出居前：&lt;X&gt; -$840.0万", text)
        self.assertIn("盘口 ±1% 买/卖挂单比：BTC 1.25", text)
        self.assertNotIn("<X>", text)

    def test_flow_without_window_and_deriv(self):
        self.assertIn("近 4 小时量价：暂不可用（K 线不足）", r.crypto_flow(self.overview(), None, "K 线不足"))
        perps = [{"asset": "BTC", "funding_ann": 1.8, "oi_usd": 1.5e8, "oi_chg4": 3.0, "oi_chg24": None, "premium": -0.01}]
        term = {"BTC": [{"expiry": NOW + 30 * 86400, "annualized": 7.2}]}
        text = r.crypto_deriv(perps, term, 3.2e8, 5.0)
        self.assertIn("BTC 永续：资金费率年化 +1.8% · 持仓 $1.50亿（4h +3.0%） · 溢价 -0.01%", text)
        self.assertIn("年化基差 +7.2%", text)
        self.assertIn("加密永续总持仓 $3.20亿（24h +5.0%）", text)

    def test_alerts_section(self):
        anomalies = [{"text": f"A{i}", "major": i == 0} for i in range(10)]
        text = r.alerts_section("⚡ T", anomalies, limit=3)
        self.assertIn("（10 条）", text)
        self.assertIn("🔴 A0", text)
        self.assertIn("…另有 7 条", text)
        self.assertIn("暂无异动", r.alerts_section("⚡ T", []))


class EquitySections(unittest.TestCase):
    def test_session_labels(self):
        self.assertEqual(r.session_label({"phase": "open", "date": "2026-10-28", "as_of": "12:00"}),
                         "盘中 · 10-28 截至 12:00 美东（当日累计）")
        self.assertEqual(r.session_label({"phase": "pre", "date": "2026-10-27"}), "盘前 · 展示 10-27 收盘数据，盘前涨跌另计")
        self.assertEqual(r.session_label({"phase": "after", "date": "2026-10-28", "unverified": True}),
                         "盘后 · 展示 10-28 收盘数据，盘后涨跌另计（盘后字段语义待校验）")
        self.assertEqual(r.session_label({}), "最近交易日")

    def test_equity_sections(self):
        rows, ov = equity_overview()
        session = {"phase": "open", "date": "2026-10-28", "as_of": "12:00"}
        self.assertIn("个股 2 只：成交 $110.0亿", r.equity_overview(ov, session))
        self.assertIn("NVDA", r.equity_top(ov))
        movers = r.equity_movers(ov)
        self.assertIn("资金流入：NVDA +$100.0亿", movers)
        self.assertIn("资金流出：XOM -$10.00亿", movers)
        self.assertIn("领涨（成交≥$5000万）：NVDA +2.5%\n", movers)
        self.assertNotIn("NVDA +2.5%", movers.split("领跌")[1])
        sectors = r.sectors(ov)
        self.assertIn("板块：科技 +$100.0亿（+2.5%） · 能源 -$10.00亿（-3.0%）", sectors)
        etf = r.etf(ov)
        self.assertIn("类别资金：宽基 +$75.00亿 · 债券 -$9.00亿", etf)
        self.assertIn("宽基", etf.split("<pre>")[1])
        card = r.equity_card(next(x for x in rows if x["symbol"] == "NVDA"), session)
        self.assertIn("NVDA", card)
        self.assertIn("科技 / 半导体", card)

    def test_bounded_lengths(self):
        many = {"top": [asset("VERYLONGSYMBOL", 1e9, -99.99, 0.000001)] * 20}
        self.assertLess(len(r.crypto_top(many)), 2000)


class MacroForecastAndNotices(unittest.TestCase):
    def test_macro(self):
        board = [{"kind": "index", "label": "美股500", "price": 3120.8, "chg24": 0.84},
                 {"kind": "commodity", "label": "黄金", "price": 4063.7, "chg24": 1.4}]
        text = r.macro(board)
        self.assertIn("股指：美股500 3,121 +0.84%", text)
        self.assertIn("商品：黄金 4,064 +1.40%", text)

    def test_forecast_shows_significance_and_records(self):
        outlook = {"price": 81000.0, "score": 25.0, "direction": 1, "low": 80000.0, "high": 82000.0, "band_pct": 1.3,
                   "parts": [("momentum", 0.17), ("book", -0.02 - 1e-9)]}
        text = r.forecast([("BTC", outlook), ("ETH", None)],
                          {"BTC": {"evaluated": 7, "directional": 5, "hits": 3, "in_range": 6}},
                          {"BTC": {"signals": 79, "hit_rate": 37.0, "coverage": 75.0, "hours": 1416, "effective": 19.8,
                                   "significant": False}})
        self.assertIn("BTC 81,000：<b>偏多</b>（+25）", text)
        self.assertIn("支撑：动量 +17", text)
        self.assertIn("拖累：盘口 -2", text)
        self.assertIn("回测 59 天命中 37%（79 次，有效样本约 20，与 50% 无显著差异）", text)
        self.assertIn("实盘 3/5 · 区间 6/7", text)
        self.assertIn("ETH：数据不足", text)
        self.assertIn("ETH：样本积累中", text)

    def test_cards_push_and_failure_notice(self):
        card = r.crypto_card(asset("BTC", 3e8, 1.5, 80000), {"ret_1h": 0.5, "ret_4h": 1.0, "rvol_1h": 1.2,
                                                             "low_7d": 76000, "high_7d": 82000},
                             {"funding_ann": 1.8, "oi_usd": 1.5e8}, {"buy_share": 55.0, "net": 1e6})
        self.assertIn("1h 量比 1.2", card)
        self.assertIn("主动买入占比 55%", card)
        push = r.alert_push([{"text": "<NVDA> 15 分钟急拉 +3.1%"}], 2, 8, NOW)
        self.assertIn("&lt;NVDA&gt;", push)
        self.assertIn("今日第 2/8 条推送", push)
        notice = r.failure_notice(NOW, {"crypto": "HTTP 503 from api.coinbase.com", "equity": "<x>"}, NOW + 4 * 3600)
        self.assertIn("HTTP 503", notice)
        self.assertIn("&lt;x&gt;", notice)
        self.assertNotIn("衍生品", notice)
        self.assertIn("下期 20:00 自动重试", notice)


if __name__ == "__main__":
    unittest.main()
