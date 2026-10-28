"""Crypto microstructure analytics (pure): taker flow from Coinbase trades, order-book
depth, hourly-candle metrics (1h move, relative volume, 7-day range) and anomalies.
"""
import datetime as dt
import math

from market_analysis import fnum

BUCKET = 900


def taker_side(trade):
    """Aggressor side. Coinbase market trades report the MAKER side (DESIGN C3), so invert it."""
    side = str(trade.get("side") or "").upper()
    return "buy" if side == "SELL" else "sell" if side == "BUY" else None


def maker_side_consistency(trades):
    """Share of price-changing trades consistent with `side` = maker side (expect > 0.5).

    A taker buy lifts the ask (uptick) against a resting SELL order, so (SELL, uptick)
    and (BUY, downtick) are consistent. Returns (ratio, samples).
    """
    ordered = sorted(trades, key=lambda t: (t["ts"], str(t.get("trade_id"))))
    agree = total = 0
    for prev, cur in zip(ordered, ordered[1:]):
        p0, p1 = fnum(prev.get("price")), fnum(cur.get("price"))
        if not p0 or not p1 or p0 == p1:
            continue
        total += 1
        side = str(cur.get("side") or "").upper()
        agree += (side == "SELL" and p1 > p0) or (side == "BUY" and p1 < p0)
    return (agree / total if total else None), total


def bucketize(trades, covered_from, covered_to, bucket=BUCKET):
    """{bucket_start: {buy, sell, trades, covered}} in quote currency; coverage in seconds."""
    out = {}

    def slot(ts):
        return int(ts) - int(ts) % bucket

    for t in trades:
        side = taker_side(t)
        price, size = fnum(t.get("price")), fnum(t.get("size"))
        if not side or not price or not size:
            continue
        b = out.setdefault(slot(t["ts"]), {"buy": 0.0, "sell": 0.0, "trades": 0, "covered": 0.0})
        b[side] += price * size
        b["trades"] += 1
    start = slot(covered_from)
    while start < covered_to:
        overlap = min(covered_to, start + bucket) - max(covered_from, start)
        if overlap > 0:
            out.setdefault(start, {"buy": 0.0, "sell": 0.0, "trades": 0, "covered": 0.0})["covered"] += overlap
        start += bucket
    return out


def taker_summary(window, seconds):
    """Aggregate flow_window() rows: per-symbol and total taker buy/sell with coverage."""
    rows = []
    for symbol, v in window.items():
        total = v["buy"] + v["sell"]
        rows.append({"symbol": symbol, "buy": v["buy"], "sell": v["sell"], "net": v["buy"] - v["sell"],
                     "buy_share": v["buy"] / total * 100 if total else None,
                     "coverage": min(1.0, v["covered"] / seconds) if seconds else None})
    rows.sort(key=lambda r: r["buy"] + r["sell"], reverse=True)
    buy, sell = sum(r["buy"] for r in rows), sum(r["sell"] for r in rows)
    coverage = [r["coverage"] for r in rows if r["coverage"] is not None]
    return {"rows": rows, "buy": buy, "sell": sell, "net": buy - sell,
            "buy_share": buy / (buy + sell) * 100 if buy + sell else None,
            "coverage": min(coverage) if coverage else None}


def book_depth(pricebook, band=0.01):
    """USD depth within +-band of mid; ratio > 1 means more resting bids than asks."""
    bids, asks = pricebook.get("bids") or [], pricebook.get("asks") or []
    if not bids or not asks:
        return None
    best_bid, best_ask = fnum(bids[0].get("price")), fnum(asks[0].get("price"))
    if not best_bid or not best_ask:
        return None
    mid = (best_bid + best_ask) / 2

    def depth(levels, keep):
        total = 0.0
        for level in levels:
            price, size = fnum(level.get("price")), fnum(level.get("size"))
            if price and size and keep(price):
                total += price * size
        return total

    bid_usd = depth(bids, lambda p: p >= mid * (1 - band))
    ask_usd = depth(asks, lambda p: p <= mid * (1 + band))
    return {"mid": mid, "bid_usd": bid_usd, "ask_usd": ask_usd, "ratio": bid_usd / ask_usd if ask_usd else None,
            "spread_bps": (best_ask - best_bid) / mid * 10000}


def candle_metrics(candles):
    """Metrics from ascending complete hourly candles (needs >= 25)."""
    if len(candles) < 25:
        return None
    closes = [c["close"] for c in candles]
    turnover = [c["volume"] * (c["high"] + c["low"] + c["close"]) / 3 for c in candles]
    last = candles[-1]
    history = turnover[-169:-1]
    avg = sum(history) / len(history) if history else 0.0
    prior = candles[-169:-1]
    return {"price": last["close"], "ret_1h": (closes[-1] / closes[-2] - 1) * 100,
            "ret_4h": (closes[-1] / closes[-5] - 1) * 100, "ret_24h": (closes[-1] / closes[-25] - 1) * 100,
            "turnover_1h": turnover[-1], "rvol_1h": turnover[-1] / avg if avg else None,
            "high_7d": max(c["high"] for c in prior), "low_7d": min(c["low"] for c in prior),
            "hours": len(candles), "end": last["start"] + 3600}


def ewma_sigma(closes, halflife=24):
    """EWMA volatility of hourly log returns (fraction per hour)."""
    if len(closes) < 3:
        return None
    lam = 0.5 ** (1 / halflife)
    var = None
    for a, b in zip(closes, closes[1:]):
        if a <= 0 or b <= 0:
            continue
        r = math.log(b / a)
        var = r * r if var is None else lam * var + (1 - lam) * r * r
    return math.sqrt(var) if var else None


def crypto_anomalies(metrics, now, major_assets=("BTC", "ETH")):
    """1h moves, hourly volume spikes and 7-day breakouts for the tracked assets."""
    day = dt.datetime.fromtimestamp(now, dt.timezone.utc).date().isoformat()
    found = []

    def add(symbol, typ, direction, severity, major, text, text_en):
        found.append({"key": f"{typ}:{symbol}:{'up' if direction > 0 else 'down'}:{day}", "kind": "crypto",
                      "symbol": symbol, "type": typ, "direction": direction, "severity": severity,
                      "major": major, "text": text, "text_en": text_en})

    for symbol, m in metrics.items():
        if not m:
            continue
        big = symbol in major_assets
        move, rvol = m["ret_1h"], m["rvol_1h"]
        limit = 1.5 if big else 4.0
        if abs(move) >= limit:
            spike = f"，量比 {rvol:.1f}" if rvol else ""
            major = abs(move) >= 2.5 if big else abs(move) >= 8.0 and (rvol or 0) >= 3 and m["turnover_1h"] >= 2e6
            add(symbol, "move1h", move, abs(move) / limit, major, f"{symbol} 1 小时 {move:+.1f}%{spike}",
                f"{symbol} {move:+.1f}% in 1h" + (f", volume {rvol:.1f}x" if rvol else ""))
        if rvol and rvol >= 3 and m["turnover_1h"] >= 2e6:
            add(symbol, "volume1h", move or 1, min(rvol / 3, 3.0), False, f"{symbol} 1 小时放量 {rvol:.1f} 倍（{move:+.1f}%）",
                f"{symbol} 1h volume {rvol:.1f}x normal ({move:+.1f}%)")
        if m["price"] > m["high_7d"]:
            add(symbol, "high7d", 1, 1.0, False, f"{symbol} 突破 7 日高点", f"{symbol} broke above its 7-day high")
        elif m["price"] < m["low_7d"]:
            add(symbol, "low7d", -1, 1.0, False, f"{symbol} 跌破 7 日低点", f"{symbol} fell below its 7-day low")
    return sorted(found, key=lambda a: a["severity"], reverse=True)
