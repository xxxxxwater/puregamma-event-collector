"""E-mail channel: per-recipient daily HTML brief (zh / en) rendered from snapshot digests, sent via SMTP.

- Email-safe HTML only (tables + inline styles, no images, no scripts, no links).
- Each recipient gets their own message (never a shared To list) once per local day at their
  `local_time` (default 08:30, like the old PureGamma AI daily brief); deduped in `email_deliveries`.
- Chinese recipients: red up / green down; English recipients: green up / red down.
Content comes from the structured digests in snapshot metrics (market_digest), not LLM text.
"""
import datetime as dt
import hashlib
import html
import json
import logging
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from zoneinfo import ZoneInfo

import market_store as store
from market_analysis import SECTOR_ZH
from market_equities import INDUSTRY_ZH
from market_forecast import FACTOR_ZH
from market_render import fmt_pct, fmt_price, fmt_usd

LOG = logging.getLogger("market.email")

PALETTE = {"cn": {"up": "#d92d20", "down": "#079455"}, "us": {"up": "#079455", "down": "#d92d20"}}
INK, MUTED, LINE, CARD, BG, DARK, ACCENT = "#101828", "#667085", "#eaecf0", "#ffffff", "#f2f4f7", "#0b0d12", "#2e90fa"
FONT = "-apple-system,'PingFang SC','Hiragino Sans GB','Microsoft YaHei','Noto Sans CJK SC',Arial,sans-serif"
PARTS = ("crypto", "deriv", "equity", "forecast")
DEFAULT_TIMEZONE = {"zh": "Asia/Shanghai", "en": "UTC"}      # mirrors the old SaaS defaults
SECTOR_EN = {zh: en for en, zh in SECTOR_ZH.items()}
INDUSTRY_EN = {zh: en for en, zh in INDUSTRY_ZH.items()}
CATEGORY_EN = {"宽基": "Broad market", "行业/主题": "Sector / theme", "债券": "Bonds", "商品": "Commodities",
               "加密": "Crypto", "海外": "International", "杠杆/反向": "Leveraged / inverse", "波动率": "Volatility",
               "其他": "Other"}
FACTOR_EN = {"trend": "trend", "momentum": "momentum", "taker": "taker flow", "book": "order book",
             "funding": "funding", "oi": "open interest", "breadth": "breadth", "macro": "US equities"}
TARGET_EN = {"美股500": "US 500"}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
T = {
    "zh": {
        "brand": "市场简报", "scope": "Coinbase 加密 / 衍生品 · 美股（Coinbase 可交易）", "tz": "北京时间",
        "highlights": "今日要点", "c_crypto": "加密资金与 Top 10", "c_deriv": "加密衍生品（Coinbase 永续/期货）",
        "c_calerts": "加密与衍生品异动", "c_equity": "美股概览与 Top 20 个股", "c_sectors": "行业资金", "c_etf": "ETF 资金",
        "c_ealerts": "美股异动", "c_macro": "跨资产（Coinbase 期货 · 24h）", "c_forecast": "市场研判（未来 24 小时）",
        "vol24": "24h 成交", "vs24": "较前 24h", "flow24": "24h 资金估算", "advdec": "涨/跌", "taker4": "近 4h 主动买盘",
        "net": "净", "major": "BTC+ETH 占比", "stables": "稳定币", "window": "近 {h} 小时量价", "asof": "截至",
        "traded": "成交", "vsh": "较前 {h}h", "wnet": "量价净额", "in": "流入", "out": "流出", "buyshare": "买盘占比",
        "cover": "逐笔覆盖率", "books": "盘口 ±1% 买/卖挂单比", "ctop": "成交额 Top 10（24h）",
        "ch": ["#", "币种", "价格", "24h", "成交额"], "dh": ["永续", "资金费率年化", "持仓", "持仓变化", "溢价"],
        "term": "期限结构（年化基差）", "expiry": "到期", "totoi": "加密永续总持仓", "mh": ["品种", "类别", "价格", "24h"],
        "index": "股指", "commodity": "商品", "session": "时段", "svol": "个股成交", "count": "只", "sflow": "个股资金估算",
        "wavg": "成交加权", "evol": "ETF 成交", "eflow": "ETF 资金估算", "stop": "成交额 Top 20 个股（不含 ETF）",
        "sh": ["#", "代码", "价格", "涨跌", "成交额"], "sin": "个股资金流入", "sout": "个股资金流出", "gain": "领涨", "lose": "领跌",
        "sech": ["板块", "资金估算", "加权涨跌", "只数"], "iin": "细分行业流入", "iout": "细分行业流出", "cats": "类别资金",
        "noetf": "（不含杠杆/波动率）", "etop": "成交额 Top 10 ETF", "eh": ["#", "代码", "涨跌", "成交额", "类别"],
        "none": "暂无异动", "badge": "● 重大", "more": "…另有 {n} 条", "dir": {1: "偏多", -1: "偏空", 0: "中性"},
        "range": "24h 区间", "conf68": "约 68%", "why": "依据", "trust": "可信度",
        "bt": "回测命中 {hit:.0f}%（{n} 次，{v}）· 区间覆盖 {cov:.0f}%", "sig": "显著", "nosig": "与 50% 无显著差异",
        "live": "实盘 {h}/{d} · 区间 {r}/{e}",
        "disclaim": "统计信号仅供参考，不构成投资建议；命中率接近 50% 说明方向没有稳定预测力，区间更可靠。",
        "na": "暂不可用", "reused": "本次刷新失败，以下沿用 {t} 的数据。", "inflow": "净流入", "outflow": "净流出",
        "footer": ("资金流向为量价估算（成交额 × 涨跌强度，涨跌 ≥2% 记全额），不是交易所公布的净流入；主动买卖来自 Coinbase 逐笔成交。"
                   "数据：加密、衍生品、跨资产来自 Coinbase 公共接口；股票清单和类型来自 Coinbase，股票和 ETF 行情来自 Nasdaq。"
                   "研判是统计信号，不构成投资建议。"),
        "unsubscribe": "如需退订，请直接回复本邮件。",
    },
    "en": {
        "brand": "Market Brief", "scope": "Coinbase crypto / derivatives · US stocks tradable on Coinbase", "tz": "UTC",
        "highlights": "Today's highlights", "c_crypto": "Crypto flows & Top 10", "c_deriv": "Crypto derivatives (Coinbase perps / futures)",
        "c_calerts": "Crypto & derivatives alerts", "c_equity": "US stocks overview & Top 20", "c_sectors": "Sector flows",
        "c_etf": "ETF flows", "c_ealerts": "US stock alerts", "c_macro": "Cross-asset (Coinbase futures · 24h)",
        "c_forecast": "Market outlook (next 24h)", "vol24": "24h volume", "vs24": "vs prior 24h", "flow24": "24h est. net flow",
        "advdec": "Up/Down", "taker4": "4h taker buys", "net": "Net", "major": "BTC+ETH share", "stables": "Stablecoins",
        "window": "Last {h}h price/volume", "asof": "as of", "traded": "Volume", "vsh": "vs prior {h}h", "wnet": "net",
        "in": "Inflows", "out": "Outflows", "buyshare": "Taker buy share", "cover": "trade coverage",
        "books": "Order book ±1% bid/ask depth", "ctop": "Top 10 by 24h volume", "ch": ["#", "Coin", "Price", "24h", "Volume"],
        "dh": ["Perp", "Funding (ann.)", "Open interest", "OI change", "Premium"], "term": "Term structure (annualized basis)",
        "expiry": "expiry", "totoi": "Total crypto perp open interest", "mh": ["Market", "Type", "Price", "24h"],
        "index": "Index", "commodity": "Commodity", "session": "Session", "svol": "Stock volume", "count": "names",
        "sflow": "Stock est. net flow", "wavg": "volume-weighted", "evol": "ETF volume", "eflow": "ETF est. net flow",
        "stop": "Top 20 stocks by dollar volume (ex-ETF)", "sh": ["#", "Ticker", "Price", "Chg", "Volume"],
        "sin": "Stock inflows", "sout": "Stock outflows", "gain": "Top gainers", "lose": "Top losers",
        "sech": ["Sector", "Est. flow", "Wtd. chg", "Names"], "iin": "Industry inflows", "iout": "Industry outflows",
        "cats": "Flows by category", "noetf": " (ex leveraged / volatility)", "etop": "Top 10 ETFs by dollar volume",
        "eh": ["#", "Ticker", "Chg", "Volume", "Category"], "none": "No alerts", "badge": "● Major", "more": "…and {n} more",
        "dir": {1: "Bullish tilt", -1: "Bearish tilt", 0: "Neutral"}, "range": "24h range", "conf68": "~68%", "why": "Drivers",
        "trust": "Track record", "bt": "backtest hit rate {hit:.0f}% ({n} signals, {v}) · range coverage {cov:.0f}%",
        "sig": "significant", "nosig": "not significantly different from 50%", "live": "live {h}/{d} · range {r}/{e}",
        "disclaim": ("Statistical signal only, not investment advice. A hit rate near 50% means the direction has no "
                     "reliable edge; the range is the sturdier output."),
        "na": "Unavailable", "reused": "Refresh failed; showing data from {t}.", "inflow": "net inflow", "outflow": "net outflow",
        "footer": ("Fund flows are price/volume estimates (turnover × move strength, full weight at ≥2% moves), not "
                   "exchange-reported net inflows; taker flow comes from Coinbase trades. Data: crypto, derivatives and "
                   "cross-asset from Coinbase public APIs; stock list and types from Coinbase; stock and ETF quotes from "
                   "Nasdaq. The outlook is a statistical signal and not investment advice."),
        "unsubscribe": "To unsubscribe, simply reply to this email.",
    },
}


class EmailError(RuntimeError):
    """Sanitized SMTP failure (never includes credentials)."""


def esc(value):
    return html.escape(str(value), quote=True)


def plain(fragment):
    return html.unescape(re.sub(r"<[^>]+>", "", fragment))


def money_en(value, signed=False):
    if value is None:
        return "-"
    sign = "-" if value < 0 else ("+" if signed and value > 0 else "")
    v = abs(value)
    for limit, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if v >= limit:
            n = v / limit
            return f"{sign}${n:.0f}{unit}" if n >= 100 else f"{sign}${n:.1f}{unit}" if n >= 10 else f"{sign}${n:.2f}{unit}"
    if v >= 1e3:
        return f"{sign}${v / 1e3:.1f}K"
    return f"{sign}${v:,.0f}"


def date_en(iso):
    try:
        d = dt.date.fromisoformat(iso)
        return f"{MONTHS[d.month - 1]} {d.day}"
    except (TypeError, ValueError):
        return iso or ""


def session_label_en(session):
    phase, date = session.get("phase"), date_en(session.get("date"))
    label = {"open": f"Live · {date} as of {session.get('as_of')} ET (intraday cumulative)",
             "pre": f"Pre-market · showing {date} close; pre-market moves listed separately",
             "after": f"After hours · showing {date} close; after-hours moves listed separately",
             "closed": f"Market closed · showing {date} close"}.get(phase, "Latest session")
    if session.get("unverified"):
        label += " (after-hours fields pending verification)"
    return label


class Renderer:
    def __init__(self, colors=None, locale="zh"):
        self.locale = "en" if locale == "en" else "zh"
        zh = self.locale == "zh"
        self.t = T[self.locale]
        self.c = PALETTE.get(colors or ("cn" if zh else "us"), PALETTE["cn"])
        self.zone = store.CST if zh else dt.timezone.utc
        self.colon, self.comma = ("：", "，") if zh else (": ", ", ")
        self.lp, self.rp = ("（", "）") if zh else (" (", ")")

    # ----- formatting -----

    def money(self, value, signed=False):
        return fmt_usd(value, signed) if self.locale == "zh" else money_en(value, signed)

    def tone(self, value):
        if value is None or value == 0:
            return INK
        return self.c["up"] if value > 0 else self.c["down"]

    def pct(self, value, digits=2):
        return f'<span style="color:{self.tone(value)};">{esc(fmt_pct(value, digits))}</span>'

    def usd(self, value, signed=True):
        return f'<span style="color:{self.tone(value) if signed else INK};">{esc(self.money(value, signed))}</span>'

    def paren(self, inner):
        return f"{self.lp}{inner}{self.rp}"

    def when(self, ts, fmt):
        local = dt.datetime.fromtimestamp(int(ts), self.zone)
        return local.strftime(fmt.replace("%b", MONTHS[local.month - 1]))

    def name(self, kind, value):
        if self.locale == "zh":
            return value
        return {"sector": SECTOR_EN, "industry": INDUSTRY_EN, "category": CATEGORY_EN}[kind].get(value, value)

    def alert_text(self, a):
        return (a.get("text_en") or a["text"]) if self.locale == "en" else a["text"]

    def session_label(self, session):
        return session.get("label", "") if self.locale == "zh" else session_label_en(session)

    # ----- building blocks -----

    @staticmethod
    def card(title, body, note=None):
        notice = (f'<tr><td style="padding:10px 16px 0 16px;"><div style="background:#fffaeb;border:1px solid #fedf89;'
                  f'color:#93370d;font-size:12px;padding:8px 10px;border-radius:6px;">{note}</div></td></tr>') if note else ""
        return (f'<tr><td style="padding:0 0 14px 0;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                f'style="background:{CARD};border:1px solid {LINE};border-radius:10px;">'
                f'<tr><td style="padding:14px 16px 4px 16px;"><div style="border-left:3px solid {ACCENT};padding-left:8px;'
                f'font-size:15px;font-weight:700;color:{INK};">{esc(title)}</div></td></tr>'
                f'{notice}<tr><td style="padding:6px 16px 14px 16px;font-size:13px;line-height:1.7;color:{INK};">{body}</td></tr>'
                f'</table></td></tr>')

    def tiles(self, items):
        """KPI tiles (label, value_html, sub_html), two per row so values never wrap on phones."""
        def cell(label, value, sub):
            return (f'<td width="50%" valign="top" style="padding:5px;"><div style="background:#f9fafb;'
                    f'border:1px solid {LINE};border-radius:8px;padding:10px;"><div style="font-size:11px;color:{MUTED};">'
                    f'{esc(label)}</div><div style="font-size:17px;font-weight:700;margin-top:2px;white-space:nowrap;">{value}'
                    f'</div><div style="font-size:11px;color:{MUTED};margin-top:2px;">{sub}</div></div></td>')
        rows = []
        for i in range(0, len(items), 2):
            pair = items[i:i + 2]
            rows.append("<tr>" + "".join(cell(*t) for t in pair) + ('<td width="50%"></td>' if len(pair) == 1 else "") + "</tr>")
        return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table>'

    @staticmethod
    def table(headers, rows, align=None):
        align = align or ["left"] + ["right"] * (len(headers) - 1)
        head = "".join(f'<th align="{a}" style="padding:6px 4px;font-size:11px;color:{MUTED};font-weight:600;'
                       f'border-bottom:1px solid {LINE};">{esc(h)}</th>' for h, a in zip(headers, align))
        body = "".join("<tr>" + "".join(f'<td align="{a}" style="padding:6px 4px;font-size:13px;border-bottom:1px solid '
                                        f'{LINE};white-space:nowrap;">{cell}</td>' for cell, a in zip(row, align)) + "</tr>"
                       for row in rows)
        return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">'
                f'<tr>{head}</tr>{body}</table>')

    def chips(self, items):
        sep = "　" if self.locale == "zh" else " · "
        return sep.join(f'<span style="white-space:nowrap;">{esc(name)} {value}</span>' for name, value in items)

    def labelled(self, label, content):
        return f"<b>{esc(label)}</b>{self.colon}{content or '-'}"

    def anomaly_list(self, items, limit=8):
        if not items:
            return f'<span style="color:{MUTED};">{esc(self.t["none"])}</span>'
        items = sorted(items, key=lambda a: not a["major"])     # stable: majors first, then by severity
        rows = [(f'<span style="color:#d92d20;font-weight:700;">{esc(self.t["badge"])}</span> ' if a["major"] else "• ")
                + esc(self.alert_text(a)) for a in items[:limit]]
        more = (f'<br/><span style="color:{MUTED};">{esc(self.t["more"].format(n=len(items) - limit))}</span>'
                if len(items) > limit else "")
        return "<br/>".join(rows) + more

    # ----- sections -----

    def crypto(self, d):
        t, taker = self.t, d.get("taker")
        tiles = [(t["vol24"], esc(self.money(d["total_24h"])), f'{esc(t["vs24"])} ' + self.pct(d["vol_change"])),
                 (t["flow24"], self.usd(d["flow_24h"]), f'{esc(t["advdec"])} {d["advancers"]}/{d["decliners"]}')]
        if taker:
            tiles.append((t["taker4"], f'{taker["buy_share"]:.0f}%', f'{esc(t["net"])} ' + self.usd(taker["net"])))
        if d.get("major_share") is not None:
            tiles.append((t["major"], f'{d["major_share"]:.0f}%',
                          f'{esc(t["stables"])} ' + esc(self.money(sum(v for _, v in d["stables"])))))
        parts = [self.tiles(tiles[:4])]
        w = d.get("window")
        if w:
            end = self.when(w["end"], "%H:%M") + ("" if self.locale == "zh" else " UTC")
            head = f'<b>{esc(t["window"].format(h=w["hours"]))}</b>{self.paren(esc(t["asof"]) + " " + end)}{self.colon}'
            body = (f'{esc(t["traded"])} {esc(self.money(w["total"]))}'
                    f'{self.paren(esc(t["vsh"].format(h=w["hours"])) + " " + self.pct(w["vs_prior"]))}'
                    f' → {esc(t["wnet"])} {self.usd(w["net"])}')
            parts.append(f'<div style="margin-top:8px;">{head}{body}<br/>'
                         f'{self.labelled(t["in"], self.chips((s, self.usd(v)) for s, v in w["top_in"]))}<br/>'
                         f'{self.labelled(t["out"], self.chips((s, self.usd(v)) for s, v in w["top_out"]))}</div>')
        if taker and taker.get("rows"):
            cover = self.paren(f'{esc(t["cover"])} {taker["coverage"] * 100:.0f}%') if taker.get("coverage") is not None else ""
            parts.append('<div style="margin-top:6px;">' + self.labelled(t["buyshare"] + cover, self.chips(
                (s, f"{v:.0f}%") for s, v in taker["rows"])) + "</div>")
        if d.get("books"):
            parts.append('<div style="margin-top:6px;">' + self.labelled(t["books"], self.chips(
                (s, f"{v:.2f}") for s, v in d["books"])) + "</div>")
        rows = [(f"{i}", f"<b>{esc(s)}</b>", esc(fmt_price(p)), self.pct(c), esc(self.money(v)))
                for i, (s, p, c, v) in enumerate(d["top"], 1)]
        parts.append(f'<div style="margin-top:10px;font-weight:700;">{esc(t["ctop"])}</div>'
                     + self.table(t["ch"], rows, ["left", "left", "right", "right", "right"]))
        return "".join(parts)

    def deriv(self, d):
        t = self.t
        rows = [(f"<b>{esc(p['asset'])}</b>", self.pct(p["funding_ann"], 1), esc(self.money(p["oi_usd"])),
                 self.pct(p["oi_chg24"], 1) if p["oi_chg24"] is not None else self.pct(p["oi_chg4"], 1),
                 self.pct(p["premium"], 2)) for p in d["perps"]]
        parts = [self.table(t["dh"], rows)]
        date_fmt = "%m-%d" if self.locale == "zh" else "%b %d"
        terms = [f'{esc(root)}{self.colon}' + self.comma.join(
            f'{self.when(e, date_fmt)} {esc(t["expiry"])} {self.pct(a, 1)}' for e, a in items)
            for root, items in sorted(d["term"].items()) if items]
        if terms:
            parts.append(f'<div style="margin-top:8px;"><b>{esc(t["term"])}</b><br/>' + "<br/>".join(terms) + "</div>")
        if d.get("total_oi"):
            chg = self.paren("24h " + self.pct(d["total_chg24"], 1)) if d.get("total_chg24") is not None else ""
            parts.append(f'<div style="margin-top:6px;">{esc(t["totoi"])} <b>{esc(self.money(d["total_oi"]))}</b>{chg}</div>')
        return "".join(parts)

    def macro(self, d):
        t = self.t
        rows = [(esc(m["label"] if self.locale == "zh" else m.get("label_en", m["label"])), esc(t[m["kind"]]),
                 esc(fmt_price(m["price"])), self.pct(m["chg24"])) for m in d["macro"]]
        return self.table(t["mh"], rows, ["left", "left", "right", "right"])

    def equity(self, d):
        t, s, e = self.t, d["stocks"], d["etfs"]
        parts = [f'<div style="color:{MUTED};font-size:12px;margin-bottom:4px;">{esc(t["session"])}{self.colon}'
                 f'{esc(self.session_label(d["session"]))}</div>',
                 self.tiles([(t["svol"], esc(self.money(s["turnover"])),
                              f'{s["count"]} {esc(t["count"])} · {esc(t["advdec"])} {s["advancers"]}/{s["decliners"]}'),
                             (t["sflow"], self.usd(s["flow"]), f'{esc(t["wavg"])} ' + self.pct(s["wchange"])),
                             (t["evol"], esc(self.money(e["turnover"])), f'{e["count"]} {esc(t["count"])}'),
                             (t["eflow"], self.usd(e["flow"]), f'{esc(t["wavg"])} ' + self.pct(e["wchange"]))])]
        rows = [(f"{i}", f"<b>{esc(sym)}</b>", esc(fmt_price(p)), self.pct(c), esc(self.money(v)))
                for i, (sym, p, c, v, _sector) in enumerate(d["top"], 1)]
        parts.append(f'<div style="margin-top:10px;font-weight:700;">{esc(t["stop"])}</div>'
                     + self.table(t["sh"], rows, ["left", "left", "right", "right", "right"]))
        parts.append('<div style="margin-top:10px;">'
                     + self.labelled(t["sin"], self.chips((x, self.usd(v)) for x, v, _ in d["inflow"])) + "<br/>"
                     + self.labelled(t["sout"], self.chips((x, self.usd(v)) for x, v, _ in d["outflow"])) + "<br/>"
                     + self.labelled(t["gain"], self.chips((x, self.pct(c, 1)) for x, c in d["gainers"])) + "<br/>"
                     + self.labelled(t["lose"], self.chips((x, self.pct(c, 1)) for x, c in d["losers"])) + "</div>")
        return "".join(parts)

    def sectors(self, d):
        t = self.t
        rows = [(esc(self.name("sector", n)), self.usd(f), self.pct(c, 1), str(k)) for n, f, c, k in d["sectors"]]
        parts = [self.table(t["sech"], rows)]
        if d["industries_in"]:
            parts.append('<div style="margin-top:8px;">' + self.labelled(
                t["iin"], self.chips((self.name("industry", n), self.usd(f)) for n, f, _, _ in d["industries_in"])) + "</div>")
        if d["industries_out"]:
            parts.append('<div style="margin-top:4px;">' + self.labelled(
                t["iout"], self.chips((self.name("industry", n), self.usd(f)) for n, f, _, _ in d["industries_out"])) + "</div>")
        return "".join(parts)

    def etf(self, d):
        t = self.t
        parts = ['<div>' + self.labelled(t["cats"], self.chips((self.name("category", n), self.usd(f))
                                                              for n, f, _, _ in d["categories"])) + "</div>"]
        if d["etf_in"] or d["etf_out"]:
            parts.append('<div style="margin-top:4px;">'
                         + self.labelled(t["in"], self.chips((s, self.usd(v)) for s, v in d["etf_in"])) + "<br/>"
                         + self.labelled(t["out"], self.chips((s, self.usd(v)) for s, v in d["etf_out"]))
                         + f'<span style="color:{MUTED};">{esc(t["noetf"])}</span></div>')
        rows = [(f"{i}", f"<b>{esc(s)}</b>", self.pct(c), esc(self.money(v)), esc(self.name("category", cat)))
                for i, (s, c, v, cat) in enumerate(d["etf_top"], 1)]
        parts.append(f'<div style="margin-top:8px;font-weight:700;">{esc(t["etop"])}</div>'
                     + self.table(t["eh"], rows, ["left", "left", "right", "right", "right"]))
        return "".join(parts)

    def forecast(self, d):
        t, names = self.t, (FACTOR_ZH if self.locale == "zh" else FACTOR_EN)
        blocks = []
        for item in d["items"]:
            if not item:
                continue
            bt, rec = item.get("backtest") or {}, item.get("record") or {}
            label = item["label"] if self.locale == "zh" else TARGET_EN.get(item["label"], item["label"])
            why = self.comma.join(f"{names.get(k, k)} {v * 100:+.0f}" for k, v in item["parts"][:3])
            trust = []
            if bt.get("signals"):
                trust.append(t["bt"].format(hit=bt["hit_rate"], n=bt["signals"], cov=bt["coverage"],
                                            v=t["sig"] if bt.get("significant") else t["nosig"]))
            if rec.get("evaluated"):
                trust.append(t["live"].format(h=rec["hits"], d=rec["directional"], r=rec["in_range"], e=rec["evaluated"]))
            band = self.paren(f'±{item["band_pct"]:.1f}%{self.comma}{esc(t["conf68"])}')
            score = self.paren(f'{item["score"]:+.0f}')
            blocks.append(
                f'<div style="border:1px solid {LINE};border-radius:8px;padding:10px;margin-top:8px;">'
                f'<b>{esc(label)}</b> {esc(fmt_price(item["price"]))}　'
                f'<span style="color:{self.tone(item["direction"])};font-weight:700;">{esc(t["dir"][item["direction"]])}</span>'
                f'{score}<br/>'
                f'{esc(t["range"])} <b>{esc(fmt_price(item["low"]))} – {esc(fmt_price(item["high"]))}</b>{band}<br/>'
                f'<span style="color:{MUTED};font-size:12px;">{esc(t["why"])}{self.colon}{esc(why)}'
                + (f'<br/>{esc(t["trust"])}{self.colon}{esc("; ".join(trust))}' if trust else "") + "</span></div>")
        blocks.append(f'<div style="margin-top:8px;color:{MUTED};font-size:12px;">{esc(t["disclaim"])}</div>')
        return "".join(blocks)

    # ----- highlights / subject -----

    def highlights(self, digests):
        en, lines = self.locale == "en", []
        c, dv, eq, fc = (digests.get(k) for k in PARTS)
        if c:
            if en:
                taker = f'; 4h taker buy share {c["taker"]["buy_share"]:.0f}%' if c.get("taker") else ""
                lines.append(f'Crypto 24h volume {esc(self.money(c["total_24h"]))} ({self.pct(c["vol_change"])} vs prior 24h); '
                             f'estimated net flow {self.usd(c["flow_24h"])}{taker}.')
            else:
                taker = f'，近 4h 主动买盘占比 {c["taker"]["buy_share"]:.0f}%' if c.get("taker") else ""
                lines.append(f'加密 24h 成交 {esc(self.money(c["total_24h"]))}（较前 24h {self.pct(c["vol_change"])}），'
                             f'资金估算 {self.usd(c["flow_24h"])}{taker}。')
        if dv and dv["perps"]:
            p = dv["perps"][0]
            oi = self.paren(("24h " if not en else "") + self.pct(p["oi_chg24"], 1) + (" 24h" if en else "")) \
                if p["oi_chg24"] is not None else ""
            lines.append(f'{esc(p["asset"])} perp funding {self.pct(p["funding_ann"], 1)} annualized; open interest '
                         f'{esc(self.money(p["oi_usd"]))}{oi}.' if en else
                         f'{esc(p["asset"])} 永续资金费率年化 {self.pct(p["funding_ann"], 1)}，持仓 {esc(self.money(p["oi_usd"]))}{oi}。')
        if eq:
            best = eq["sectors"][0] if eq["sectors"] else None
            worst = eq["sectors"][-1] if eq["sectors"] else None
            label = re.split(r" \(|（|;|，", self.session_label(eq["session"]))[0]
            if en:
                text = f'US stocks ({esc(label)}): estimated stock flow {self.usd(eq["stocks"]["flow"])}'
                if best and worst and best[0] != worst[0]:
                    text += (f'; {esc(self.name("sector", best[0]))} led ({self.usd(best[1])}), '
                             f'{esc(self.name("sector", worst[0]))} lagged ({self.usd(worst[1])})')
            else:
                text = f'美股（{esc(label)}）个股资金估算 {self.usd(eq["stocks"]["flow"])}'
                if best and worst and best[0] != worst[0]:
                    text += f'，{esc(best[0])}领先（{self.usd(best[1])}），{esc(worst[0])}垫底（{self.usd(worst[1])}）'
            lines.append(text + ("." if en else "。"))
        majors = [a for part in (c, dv, eq) if part for a in part.get("anomalies", []) if a["major"]]
        if majors:
            sample = "; ".join(esc(self.alert_text(a)) for a in majors[:3])
            lines.append(f'{len(majors)} major alert(s): {sample}.' if en else f'重大异动 {len(majors)} 条：{sample}。')
        if dv and dv.get("macro"):
            picks = [m for m in dv["macro"] if m["label"] in ("美股500", "黄金", "原油")]
            if picks:
                body = self.comma.join(f'{esc(m.get("label_en", m["label"]) if en else m["label"])} {self.pct(m["chg24"])}'
                                       for m in picks)
                lines.append(f"Cross-asset: {body}." if en else f"跨资产：{body}。")
        if fc:
            first = next((i for i in fc["items"] if i), None)
            if first:
                tilt = self.t["dir"][first["direction"]]
                rng = f'{esc(fmt_price(first["low"]))}–{esc(fmt_price(first["high"]))}'
                label = first["label"] if not en else TARGET_EN.get(first["label"], first["label"])
                lines.append(f'Outlook: {esc(label)} {esc(tilt.lower())} ({first["score"]:+.0f}), 24h range {rng} '
                             f'(statistical signal, not advice).' if en else
                             f'研判：{esc(label)} {esc(tilt)}（{first["score"]:+.0f}），24h 区间 {rng}（统计信号，仅供参考）。')
        return lines

    def subject(self, snapshot, digests):
        en = self.locale == "en"
        bits = [f'{self.t["brand"]} {self.when(snapshot["taken_at"], "%b %d %H:%M UTC" if en else "%m-%d %H:%M")}']
        c = digests.get("crypto")
        if c and c["top"]:
            sym, price, change, _ = c["top"][0]
            bits.append(f"{sym} {fmt_price(price)} {fmt_pct(change, 1)}")
        eq = digests.get("equity")
        if eq:
            flow = eq["stocks"]["flow"]
            word = self.t["inflow"] if flow >= 0 else self.t["outflow"]
            bits.append(f"US stocks {word} {self.money(abs(flow))}" if en else f"美股资金{word} {self.money(abs(flow))}")
        return (" | " if en else "｜").join(bits)

    # ----- whole message -----

    def render(self, snapshot):
        t, metrics, status = self.t, snapshot["metrics"], snapshot["status"]
        digests = {k: (metrics.get(k) or {}).get("digest") for k in PARTS}

        def note(key):
            if str(status.get(key, "")).startswith("reused"):
                stamp = (metrics.get(key) or {}).get("reused_from") or snapshot["taken_at"]
                return esc(t["reused"].format(t=self.when(stamp, "%H:%M")))
            return None

        def missing(title, key):
            return self.card(title, f'<span style="color:{MUTED};">{esc(t["na"])}{self.colon}{esc(status.get(key, "-"))}</span>')

        cards = [self.card(t["c_crypto"], self.crypto(digests["crypto"]), note("crypto"))
                 if digests["crypto"] else missing(t["c_crypto"], "crypto")]
        if digests["deriv"]:
            cards.append(self.card(t["c_deriv"], self.deriv(digests["deriv"]), note("deriv")))
        alerts = (digests["crypto"] or {}).get("anomalies", []) + (digests["deriv"] or {}).get("anomalies", [])
        cards.append(self.card(t["c_calerts"], self.anomaly_list(alerts)))
        if digests["equity"]:
            cards += [self.card(t["c_equity"], self.equity(digests["equity"]), note("equity")),
                      self.card(t["c_sectors"], self.sectors(digests["equity"])),
                      self.card(t["c_etf"], self.etf(digests["equity"])),
                      self.card(t["c_ealerts"], self.anomaly_list(digests["equity"]["anomalies"]))]
        else:
            cards.append(missing(t["c_equity"], "equity"))
        if digests["deriv"]:
            cards.append(self.card(t["c_macro"], self.macro(digests["deriv"])))
        cards.append(self.card(t["c_forecast"], self.forecast(digests["forecast"]), note("forecast"))
                     if digests["forecast"] else missing(t["c_forecast"], "forecast"))
        highlights = self.highlights(digests)
        summary = "".join(f'<li style="margin:0 0 6px 0;">{line}</li>' for line in highlights)
        preheader = esc(plain(highlights[0]) if highlights else "")[:140]
        when = f'{self.when(snapshot["taken_at"], "%Y-%m-%d %H:%M")} {t["tz"]}'
        subject = self.subject(snapshot, digests)
        lang = "zh-CN" if self.locale == "zh" else "en"
        body = f"""<!DOCTYPE html><html lang="{lang}"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/><meta name="color-scheme" content="light only"/>
<title>{esc(subject)}</title></head>
<body style="margin:0;padding:0;background:{BG};font-family:{FONT};color:{INK};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BG};"><tr><td align="center" style="padding:16px 10px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;">
<tr><td style="background:{DARK};border-radius:10px 10px 0 0;padding:16px 18px;">
  <div style="color:#ffffff;font-size:18px;font-weight:800;letter-spacing:0.5px;">PureGamma <span style="color:#98a2b3;font-weight:400;">· {esc(t["brand"])}</span></div>
  <div style="color:#98a2b3;font-size:12px;margin-top:4px;">{esc(when)} · {esc(t["scope"])}</div>
</td></tr>
<tr><td style="background:{CARD};border:1px solid {LINE};border-top:0;border-radius:0 0 10px 10px;padding:14px 16px;">
  <div style="font-size:15px;font-weight:700;margin-bottom:6px;">{esc(t["highlights"])}</div>
  <ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.7;">{summary}</ul>
</td></tr>
<tr><td style="height:14px;line-height:14px;font-size:0;">&nbsp;</td></tr>
{''.join(cards)}
<tr><td style="padding:4px 6px 20px 6px;color:{MUTED};font-size:11px;line-height:1.7;">{esc(t["footer"])}<br/>{esc(t["unsubscribe"])}</td></tr>
</table></td></tr></table></body></html>"""
        text = (f"PureGamma {t['brand']} · {when}\n\n{t['highlights']}:\n"
                + "\n".join(f"- {plain(line)}" for line in highlights) + f"\n\n{t['footer']}\n{t['unsubscribe']}")
        return subject, body, text


# ---------- recipients and schedule ----------

def recipient_hash(email):
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:24]


def load_recipients(path=None, fallback=(), default_locale="zh", default_time="08:30"):
    """Recipients from a JSON file ([{email, locale, timezone, local_time, enabled}]) plus fallback addresses.

    Invalid rows are skipped; duplicate addresses keep the first entry. Never logs addresses.
    """
    rows = []
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read().strip()
            rows = json.loads(raw) if raw else []
        except (OSError, ValueError):
            rows = []
    rows = list(rows if isinstance(rows, list) else []) + [{"email": e} for e in fallback]
    out, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or not row.get("enabled", True):
            continue
        email = str(row.get("email") or "").strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or email.lower() in seen:
            continue
        seen.add(email.lower())
        locale = "en" if str(row.get("locale") or default_locale).lower().startswith("en") else "zh"
        timezone = str(row.get("timezone") or DEFAULT_TIMEZONE[locale])
        try:
            ZoneInfo(timezone)
        except Exception:
            timezone = DEFAULT_TIMEZONE[locale]
        local_time = str(row.get("local_time") or default_time)
        # due_today() feeds these into datetime.replace(); "24:00" or "9:75" would raise there every tick.
        clock = re.fullmatch(r"(\d{1,2}):(\d{2})", local_time)
        if not clock or int(clock.group(1)) > 23 or int(clock.group(2)) > 59:
            local_time = default_time
        out.append({"email": email, "locale": locale, "timezone": timezone, "local_time": local_time})
    return out


def due_today(recipient, now, grace):
    """(key, local_date, due_at) when this recipient's local delivery time passed less than `grace` ago, else None."""
    zone = ZoneInfo(recipient["timezone"])
    local = dt.datetime.fromtimestamp(int(now), zone)
    hour, minute = (int(x) for x in recipient["local_time"].split(":"))
    due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local < due or (local - due).total_seconds() > grace:
        return None
    date = due.date().isoformat()
    return f"{recipient_hash(recipient['email'])}:{date}", date, int(due.timestamp())


# ---------- SMTP ----------

def smtp_sender(host, port, user, password, sender, security="auto", reply_to=None, timeout=20, sleep=time.sleep,
                smtp_factory=None):
    """Return send(to, subject, html_body, text_body): one multipart message to exactly one recipient."""
    mode = security if security != "auto" else ("ssl" if int(port) == 465 else "starttls")

    def connect():
        if smtp_factory:
            return smtp_factory(mode)
        context = ssl.create_default_context()
        if mode == "ssl":
            return smtplib.SMTP_SSL(host, int(port), timeout=timeout, context=context)
        client = smtplib.SMTP(host, int(port), timeout=timeout)
        try:
            client.ehlo()
            if mode == "starttls":
                client.starttls(context=context)
                client.ehlo()
        except BaseException:
            client.close()
            raise
        return client

    def send(to, subject, html_body, text_body):
        message = EmailMessage()
        message["Subject"], message["From"], message["To"] = subject, sender, to
        if reply_to:
            message["Reply-To"] = reply_to
        message["Date"] = formatdate(localtime=False)
        message["Message-ID"] = make_msgid(domain=(sender.rsplit("@", 1)[-1].strip("> ") or "localhost"))
        message.set_content(text_body, charset="utf-8")
        message.add_alternative(html_body, subtype="html", charset="utf-8")
        last = "unknown"
        for attempt in range(1, 4):
            accepted = False
            try:
                with connect() as client:
                    if user:
                        client.login(user, password)
                    client.send_message(message)
                    accepted = True
                return
            except smtplib.SMTPAuthenticationError:
                raise EmailError("SMTP 认证失败（检查账号或授权码）") from None
            except smtplib.SMTPRecipientsRefused:
                raise EmailError("收件人被拒绝") from None
            except (smtplib.SMTPException, OSError) as ex:
                if accepted:
                    # The server took the message; only QUIT/close failed (SMTP.__exit__ raises on a
                    # non-221 reply). Retrying would deliver a duplicate.
                    LOG.warning("SMTP session ended uncleanly after delivery (%s)", type(ex).__name__)
                    return
                last = type(ex).__name__
                if attempt < 3:
                    sleep(5 * attempt)
        raise EmailError(f"SMTP 发送失败（{last}）")
    return send
