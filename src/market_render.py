"""Render market metrics as Telegram HTML sections (escaped, bounded length)."""
import html
import math
import unicodedata

from market_forecast import FACTOR_ZH
from market_store import bj

CRYPTO_FLOW_TITLE = "💰 加密资金流向"
CRYPTO_DERIV_TITLE = "🧾 加密衍生品"
CRYPTO_TOP_TITLE = "🪙 成交额 Top 10 加密现货"
CRYPTO_ALERTS_TITLE = "⚡ 加密异动"
EQUITY_OVERVIEW_TITLE = "🏦 美股概览"
EQUITY_TOP_TITLE = "📈 成交额 Top 20 个股"
EQUITY_MOVERS_TITLE = "💹 个股资金与涨跌"
SECTORS_TITLE = "🏭 行业资金"
ETF_TITLE = "🧺 ETF"
EQUITY_ALERTS_TITLE = "⚡ 美股异动"
MACRO_TITLE = "🌐 跨资产"
FORECAST_TITLE = "🧭 市场研判"
NOTE = ("<i>注：资金流向为量价估算（成交额 × 涨跌强度，涨跌 ≥2% 记全额），不是交易所公布的净流入；"
        "主动买卖来自 Coinbase 逐笔成交。加密、衍生品、跨资产：Coinbase；股票清单和类型：Coinbase，"
        "股票和 ETF 行情：Nasdaq。研判是统计信号，不构成投资建议。</i>")
SOURCE_ZH = {"crypto": "加密（Coinbase）", "deriv": "衍生品（Coinbase）",
             "equity": "美股（Coinbase 清单 / Nasdaq 行情）", "forecast": "研判"}
PHASE_ZH = {"open": "盘中", "pre": "盘前", "after": "盘后", "closed": "休市"}


def esc(value):
    return html.escape(str(value), quote=False)


def fmt_usd(value, signed=False):
    if value is None:
        return "-"
    sign = "-" if value < 0 else ("+" if signed and value > 0 else "")
    v = abs(value)
    if v >= 1e11:
        return f"{sign}${v / 1e8:.0f}亿"
    if v >= 1e10:
        return f"{sign}${v / 1e8:.1f}亿"
    if v >= 1e8:
        return f"{sign}${v / 1e8:.2f}亿"
    if v >= 1e7:
        return f"{sign}${v / 1e4:.0f}万"
    if v >= 1e4:
        return f"{sign}${v / 1e4:.1f}万"
    return f"{sign}${v:,.0f}"


def fmt_price(value):
    if value is None:
        return "-"
    if value >= 1000:
        return f"{value:,.0f}"
    if value >= 1:
        return f"{value:,.2f}"
    if value <= 0:
        return "0"
    return f"{value:.{min(10, 2 - math.floor(math.log10(value)))}f}"


def fmt_pct(value, digits=2):
    return "-" if value is None else f"{value:+.{digits}f}%"


def flow_tag(net):
    return "净流入" if net >= 0 else "净流出"


def display_width(text):
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def cell(text, width, left=False):
    pad = " " * max(0, width - display_width(text))
    return text + pad if left else pad + text


def table(header, rows, widths):
    """Monospace table; widths are display cells, so CJK units stay aligned."""
    lines = [" ".join(cell(v, w, left=(i == 1)) for i, (v, w) in enumerate(zip(header, widths)))]
    for row in rows:
        lines.append(" ".join(cell(v, w, left=(i == 1)) for i, (v, w) in enumerate(zip(row, widths))))
    return "<pre>" + esc("\n".join(lines)) + "</pre>"


def joined(items, sep="，"):
    return sep.join(items)


def unavailable(title, reason):
    return f"<b>{title}</b>\n⚠️ 暂不可用：{esc(reason)[:200]}"


def reused(section, taken_at, reason):
    """Mark a section copied from an earlier snapshot after a failed refresh."""
    note = f"<i>⚠️ 本次刷新失败（{esc(reason)[:80]}），以下沿用 {bj(taken_at, '%H:%M')} 的数据</i>"
    if section.startswith("<i>⚠️ 本次刷新失败"):
        section = section.split("\n", 1)[1]
    return note + "\n" + section


# ---------- crypto ----------

def crypto_flow(overview, window, window_error=None, taker=None, books=None):
    f = overview["flow"]
    lines = [f"<b>{CRYPTO_FLOW_TITLE}</b>（Coinbase 现货 · USD/USDC/USDT）"]
    if taker and taker["buy"] + taker["sell"] > 0:
        names = "/".join(esc(r["symbol"]) for r in taker["rows"][:6])
        cover = ""
        if taker["coverage"] is not None:
            cover = f" · 覆盖率 {taker['coverage'] * 100:.0f}%" + ("，数据积累中" if taker["coverage"] < 0.5 else "")
        lines.append(f"<b>近 4 小时主动买卖</b>（逐笔 {names}{cover}）")
        lines.append(f"主动买入 {fmt_usd(taker['buy'])} / 主动卖出 {fmt_usd(taker['sell'])} → "
                     f"净{'买入' if taker['net'] >= 0 else '卖出'} <b>{fmt_usd(abs(taker['net']))}</b>"
                     f"（买盘占比 {taker['buy_share']:.0f}%）")
        tilt = [r for r in taker["rows"] if r["buy_share"] is not None][:6]
        if tilt:
            lines.append("买盘占比：" + joined((f"{esc(r['symbol'])} {r['buy_share']:.0f}%" for r in tilt), " · "))
    if window:
        lines.append(f"<b>近 {window['hours']} 小时量价</b>（成交额前 {window['assets']} 的币种 · 截至 {bj(window['end'], '%H:%M')}）")
        lines.append(f"成交 {fmt_usd(window['total'])}（较前 {window['hours']} 小时 {fmt_pct(window['vs_prior'])}）"
                     f" → 量价{flow_tag(window['net'])} <b>{fmt_usd(abs(window['net']))}</b>")
        if window["top_in"]:
            lines.append("流入居前：" + joined(f"{esc(a['symbol'])} {fmt_usd(a['net'], True)}" for a in window["top_in"]))
        if window["top_out"]:
            lines.append("流出居前：" + joined(f"{esc(a['symbol'])} {fmt_usd(a['net'])}" for a in window["top_out"]))
    else:
        lines.append(f"近 4 小时量价：暂不可用（{esc(window_error or '无 K 线数据')[:120]}）")
    lines.append(f"<b>24 小时</b>：成交 {fmt_usd(f['total'])}（较前 24 小时 {fmt_pct(f['vol_change'])}）"
                 f" · 涨/跌 {f['advancers']}/{f['decliners']} · 资金估算{flow_tag(f['flow'])} <b>{fmt_usd(abs(f['flow']))}</b>")
    extra = []
    if overview["major_share"] is not None:
        extra.append(f"BTC+ETH 占比 {overview['major_share']:.0f}%")
    if overview["stables"]:
        extra.append("稳定币兑换 " + joined(f"{esc(s['symbol'])} {fmt_usd(s['volume'])}" for s in overview["stables"][:2]))
    if extra:
        lines.append(" · ".join(extra))
    depth = [(sym, b) for sym, b in (books or {}).items() if b and b.get("ratio")]
    if depth:
        lines.append("盘口 ±1% 买/卖挂单比：" + joined((f"{esc(sym)} {b['ratio']:.2f}" for sym, b in depth[:5]), " · "))
    return "\n".join(lines)


def crypto_deriv(perps, term, total_oi=None, total_chg24=None):
    lines = [f"<b>{CRYPTO_DERIV_TITLE}</b>（Coinbase 永续/期货）"]
    for p in perps[:4]:
        oi_chg = []
        if p["oi_chg4"] is not None:
            oi_chg.append(f"4h {fmt_pct(p['oi_chg4'], 1)}")
        if p["oi_chg24"] is not None:
            oi_chg.append(f"24h {fmt_pct(p['oi_chg24'], 1)}")
        chg = f"（{'，'.join(oi_chg)}）" if oi_chg else ""
        fund = f"资金费率年化 {fmt_pct(p['funding_ann'], 1)}" if p["funding_ann"] is not None else "资金费率 -"
        prem = f" · 溢价 {fmt_pct(p['premium'], 2)}" if p["premium"] is not None else ""
        lines.append(f"{esc(p['asset'])} 永续：{fund} · 持仓 {fmt_usd(p['oi_usd'])}{chg}{prem}")
    for root, rows in sorted(term.items()):
        if rows:
            lines.append(f"{esc(root)} 期限结构：" + joined(
                f"{bj(r['expiry'], '%m-%d')} 到期年化基差 {fmt_pct(r['annualized'], 1)}" for r in rows))
    if total_oi:
        chg = f"（24h {fmt_pct(total_chg24, 1)}）" if total_chg24 is not None else ""
        lines.append(f"加密永续总持仓 {fmt_usd(total_oi)}{chg}")
    return "\n".join(lines)


def crypto_top(overview):
    rows = [(str(i), a["symbol"][:6], fmt_price(a["price"]), fmt_pct(a["change"]), fmt_usd(a["volume"]))
            for i, a in enumerate(overview["top"], 1)]
    return (f"<b>{CRYPTO_TOP_TITLE}</b>（24 小时）\n"
            + table(("#", "COIN", "PRICE", "24H", "VOL"), rows, (2, 6, 10, 8, 9)))


def alerts_section(title, anomalies, limit=8, empty="暂无异动"):
    if not anomalies:
        return f"<b>{title}</b>\n{empty}"
    lines = [f"<b>{title}</b>（{len(anomalies)} 条）"]
    lines += [("🔴 " if a["major"] else "• ") + esc(a["text"]) for a in anomalies[:limit]]
    if len(anomalies) > limit:
        lines.append(f"…另有 {len(anomalies) - limit} 条")
    return "\n".join(lines)


# ---------- equities ----------

def session_label(session):
    phase, date = session.get("phase"), (session.get("date") or "")[5:]
    if phase == "open":
        label = f"盘中 · {date} 截至 {session.get('as_of')} 美东（当日累计）"
    elif phase == "pre":
        label = f"盘前 · 展示 {date} 收盘数据，盘前涨跌另计"
    elif phase == "after":
        label = f"盘后 · 展示 {date} 收盘数据，盘后涨跌另计"
    elif phase == "closed":
        label = f"休市 · 展示 {date} 收盘数据"
    else:
        label = "最近交易日"
    if session.get("unverified"):
        label += "（盘后字段语义待校验）"
    return label


def _summary_line(name, s):
    return (f"{name} {s['count']} 只：成交 {fmt_usd(s['turnover'])} · 涨/跌 {s['advancers']}/{s['decliners']}"
            f" · 资金估算{flow_tag(s['flow'])} <b>{fmt_usd(abs(s['flow']))}</b>（成交加权 {fmt_pct(s['wchange'])}）")


def equity_overview(ov, session):
    lines = [f"<b>{EQUITY_OVERVIEW_TITLE}</b>（Coinbase 可交易 · Nasdaq 实时行情）", f"时段：{session_label(session)}"]
    if ov["stocks"]["count"]:
        lines.append(_summary_line("个股", ov["stocks"]))
    if ov["etfs"]["count"]:
        lines.append(_summary_line("ETF", ov["etfs"]))
    return "\n".join(lines)


def equity_top(ov):
    rows = [(str(i), r["symbol"][:6], fmt_price(r["price"]), fmt_pct(r["change"]), fmt_usd(r["turnover"]))
            for i, r in enumerate(ov["top"], 1)]
    return (f"<b>{EQUITY_TOP_TITLE}</b>（不含 ETF）\n"
            + table(("#", "TICKER", "PRICE", "CHG", "VOL"), rows, (2, 6, 8, 8, 10)))


def equity_movers(ov):
    lines = [f"<b>{EQUITY_MOVERS_TITLE}</b>"]
    if ov["inflow"]:
        lines.append("资金流入：" + joined(f"{esc(r['symbol'])} {fmt_usd(r['flow'], True)}" for r in ov["inflow"]))
    if ov["outflow"]:
        lines.append("资金流出：" + joined(f"{esc(r['symbol'])} {fmt_usd(r['flow'])}" for r in ov["outflow"]))
    if ov["gainers"]:
        lines.append("领涨（成交≥$5000万）：" + joined(f"{esc(r['symbol'])} {fmt_pct(r['change'], 1)}" for r in ov["gainers"]))
    if ov["losers"]:
        lines.append("领跌（成交≥$5000万）：" + joined(f"{esc(r['symbol'])} {fmt_pct(r['change'], 1)}" for r in ov["losers"]))
    return "\n".join(lines)


def sectors(ov):
    lines = [f"<b>{SECTORS_TITLE}</b>（个股，按资金估算排序）"]
    if ov["sectors"]:
        lines.append("板块：" + joined((f"{esc(g['name'])} {fmt_usd(g['flow'], True)}（{fmt_pct(g['change'], 1)}）"
                                       for g in ov["sectors"]), " · "))
    inds = ov["industries"]
    if inds:
        top = [g for g in inds if g["flow"] > 0][:5]
        bottom = [g for g in reversed(inds) if g["flow"] < 0][:5]
        if top:
            lines.append("细分行业流入：" + joined(f"{esc(g['name'])} {fmt_usd(g['flow'], True)}（{fmt_pct(g['change'], 1)}）" for g in top))
        if bottom:
            lines.append("细分行业流出：" + joined(f"{esc(g['name'])} {fmt_usd(g['flow'])}（{fmt_pct(g['change'], 1)}）" for g in bottom))
    return "\n".join(lines)


def etf(ov):
    lines = [f"<b>{ETF_TITLE}</b>（Coinbase 可交易 {ov['etfs']['count']} 只）"]
    if ov["categories"]:
        lines.append("类别资金：" + joined((f"{esc(g['name'])} {fmt_usd(g['flow'], True)}" for g in ov["categories"]), " · "))
    if ov["etf_in"]:
        lines.append("流入（不含杠杆/波动率）：" + joined(f"{esc(r['symbol'])} {fmt_usd(r['flow'], True)}" for r in ov["etf_in"]))
    if ov["etf_out"]:
        lines.append("流出（不含杠杆/波动率）：" + joined(f"{esc(r['symbol'])} {fmt_usd(r['flow'])}" for r in ov["etf_out"]))
    rows = [(str(i), r["symbol"][:6], fmt_pct(r["change"]), fmt_usd(r["turnover"]), r["category"][:5])
            for i, r in enumerate(ov["etf_top"], 1)]
    if rows:
        lines.append("成交额 Top 10：\n" + table(("#", "ETF", "CHG", "VOL", "TYPE"), rows, (2, 6, 8, 10, 10)))
    return "\n".join(lines)


# ---------- macro & forecast ----------

def macro(board):
    lines = [f"<b>{MACRO_TITLE}</b>（Coinbase 期货 · 24 小时）"]
    for kind, label in (("index", "股指"), ("commodity", "商品")):
        items = [m for m in board if m["kind"] == kind]
        if items:
            lines.append(f"{label}：" + joined((f"{esc(m['label'])} {fmt_price(m['price'])} {fmt_pct(m['chg24'])}"
                                              for m in items), " · "))
    return "\n".join(lines)


def _parts(parts):
    pos = [f"{FACTOR_ZH.get(k, k)} {v * 100:+.0f}" for k, v in parts if v > 0.02][:3]
    neg = [f"{FACTOR_ZH.get(k, k)} {v * 100:+.0f}" for k, v in parts if v < -0.02][:2]
    text = []
    if pos:
        text.append("支撑：" + "、".join(pos))
    if neg:
        text.append("拖累：" + "、".join(neg))
    return "；".join(text)


def forecast(outlooks, records, backtests):
    """outlooks: [(label, outlook)], records/backtests: {label: dict}."""
    lines = [f"<b>{FORECAST_TITLE}</b>（模型 v1 · 未来 24 小时）"]
    for label, o in outlooks:
        if not o:
            lines.append(f"{esc(label)}：数据不足")
            continue
        tilt = {1: "偏多", -1: "偏空", 0: "中性"}[o["direction"]]
        lines.append(f"{esc(label)} {fmt_price(o['price'])}：<b>{tilt}</b>（{o['score']:+.0f}）· 区间 "
                     f"{fmt_price(o['low'])}–{fmt_price(o['high'])}（±{o['band_pct']:.1f}%，约 68%）")
        why = _parts(o["parts"])
        if why:
            lines.append("  " + why)
    lines.append("📏 可信度（方向命中率只统计偏多/偏空的信号）：")
    for label, _ in outlooks:
        bt, rec = backtests.get(label) or {}, records.get(label) or {}
        bits = []
        if bt.get("signals"):
            verdict = "显著" if bt.get("significant") else "与 50% 无显著差异"
            bits.append(f"回测 {bt['hours'] // 24} 天命中 {bt['hit_rate']:.0f}%（{bt['signals']} 次，有效样本约 "
                        f"{bt.get('effective', 0):.0f}，{verdict}）· 区间覆盖 {bt['coverage']:.0f}%")
        if rec.get("evaluated"):
            hit = f"{rec['hits']}/{rec['directional']}" if rec["directional"] else "无方向信号"
            bits.append(f"实盘 {hit} · 区间 {rec['in_range']}/{rec['evaluated']}")
        lines.append(f"  {esc(label)}：" + ("；".join(bits) if bits else "样本积累中"))
    lines.append("⚠️ 统计信号仅供参考，不构成投资建议；命中率接近 50% 说明方向没有稳定预测力，区间更可靠。")
    return "\n".join(lines)


# ---------- cards, pushes, notices ----------

def equity_card(r, session):
    kind = "ETF" if r["kind"] == "etf" else "个股"
    lines = [f"🔎 <b>{esc(r['symbol'])}</b> {esc(r['name'][:40])}（{kind}"
             + (f" · {esc(r['sector'])} / {esc(r['industry'])}" if r["kind"] == "stock" else f" · {esc(r['category'])}") + "）",
             f"价格 {fmt_price(r['price'])} {fmt_pct(r['change'])} · 成交 {fmt_usd(r['turnover'])} · "
             f"资金估算 {fmt_usd(r['flow'], True)}"]
    if r["rvol"]:
        lines.append(f"量比 {r['rvol']:.2f}（相对近 20 日均量）")
    if r["ext"] is not None:
        lines.append(f"盘外 {fmt_price(r['ext_price'])} {fmt_pct(r['ext'])}")
    lines.append(f"时段：{session_label(session)}")
    return "\n".join(lines)


def crypto_card(asset, metrics, perp=None, taker=None):
    lines = [f"🔎 <b>{esc(asset['symbol'])}</b>（加密 · Coinbase）",
             f"价格 {fmt_price(asset['price'])} · 24h {fmt_pct(asset['change'])} · 成交 {fmt_usd(asset['volume'])}"]
    if metrics:
        rvol = f" · 1h 量比 {metrics['rvol_1h']:.1f}" if metrics.get("rvol_1h") else ""
        lines.append(f"1h {fmt_pct(metrics['ret_1h'])} · 4h {fmt_pct(metrics['ret_4h'])}{rvol} · "
                     f"7 日区间 {fmt_price(metrics['low_7d'])}–{fmt_price(metrics['high_7d'])}")
    if taker and taker.get("buy_share") is not None:
        lines.append(f"近 4h 主动买入占比 {taker['buy_share']:.0f}%（净 {fmt_usd(taker['net'], True)}）")
    if perp:
        lines.append(f"永续：资金费率年化 {fmt_pct(perp['funding_ann'], 1)} · 持仓 {fmt_usd(perp['oi_usd'])}")
    return "\n".join(lines)


def alert_push(anomalies, number, cap, now):
    lines = [f"⚡ <b>异动提醒</b>（{bj(now, '%H:%M')} 北京时间）"]
    lines += ["• " + esc(a["text"]) for a in anomalies]
    lines.append(f"<i>今日第 {number}/{cap} 条推送 · 完整列表 /alerts</i>")
    return "\n".join(lines)


def failure_notice(slot, status, next_slot):
    lines = [f"⚠️ <b>Coinbase 市场简报</b>（{bj(slot)} 北京时间）", "本期数据获取失败，没有生成简报："]
    for key, label in SOURCE_ZH.items():
        if key in status:
            lines.append(f"• {label}：{esc(status.get(key, '未知错误'))[:150]}")
    lines.append(f"下期 {bj(next_slot, '%H:%M')} 自动重试；可随时发 /market 查看最新快照。")
    return "\n".join(lines)
