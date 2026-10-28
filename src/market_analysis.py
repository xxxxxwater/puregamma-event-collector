"""Pure market computations (no I/O): aggregation, flow proxies, rankings, US session.

Every "flow" here is a price/volume proxy, not exchange-reported net inflow.
Definitions: market/DESIGN.md section 5.
"""
import datetime as dt
from zoneinfo import ZoneInfo

USD_QUOTES = ("USD", "USDC", "USDT")
STABLES = {"USDT", "USDC", "DAI", "PYUSD", "EURC", "USDS", "GUSD", "PAX", "USDP", "TUSD",
           "FDUSD", "RLUSD", "USD1", "USDE", "GHO", "LUSD", "FRAX", "BUSD", "USTC"}
ET = ZoneInfo("America/New_York")
SECTOR_ZH = {"Technology": "科技", "Finance": "金融", "Health Care": "医疗", "Consumer Discretionary": "可选消费",
             "Consumer Staples": "必选消费", "Industrials": "工业", "Energy": "能源", "Utilities": "公用事业",
             "Real Estate": "房地产", "Telecommunications": "电信", "Basic Materials": "原材料",
             "Miscellaneous": "其他"}
UNKNOWN_SESSION = {"state": "unknown", "trade_date": None, "as_of": None}


def fnum(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # drop NaN


def money(value):
    """Parse Nasdaq strings such as '$1,234.50', '-2.1%' or '1,000'."""
    return fnum(str(value or "").replace("$", "").replace(",", "").replace("%", "").strip())


def direction(change):
    return 1 if (change or 0) > 0 else -1 if (change or 0) < 0 else 0


def flow_value(turnover, change):
    """Signed flow estimate: full turnover at |change| >= 2%, proportional below (DESIGN §5)."""
    return turnover * max(-1.0, min(1.0, (change or 0.0) / 2.0))


def flow_summary(items):
    """Turnover of advancing vs declining items: the signed-volume flow proxy."""
    up = sum(i["volume"] for i in items if direction(i["change"]) > 0)
    down = sum(i["volume"] for i in items if direction(i["change"]) < 0)
    total = sum(i["volume"] for i in items)
    prev = sum(i.get("prev_volume", i["volume"]) for i in items)
    return {"total": total, "up": up, "down": down, "net": up - down,
            "flow": sum(flow_value(i["volume"], i["change"]) for i in items),
            "up_share": up / total * 100 if total else None,
            "vol_change": (total / prev - 1) * 100 if prev else None,
            "advancers": sum(1 for i in items if direction(i["change"]) > 0),
            "decliners": sum(1 for i in items if direction(i["change"]) < 0)}


# ---------- crypto ----------

def crypto_assets(products):
    """Aggregate USD-like spot turnover per base asset.

    USD and USDC books are unified on Coinbase: the product carrying a non-empty
    `alias` duplicates another product and is skipped (constraint C7).
    """
    assets = {}
    for p in products:
        if p.get("product_type") != "SPOT" or p.get("quote_currency_id") not in USD_QUOTES:
            continue
        if p.get("alias") or p.get("is_disabled") or p.get("trading_disabled") or p.get("status") != "online":
            continue
        volume = fnum(p.get("approximate_quote_24h_volume")) or 0.0
        base = p.get("base_currency_id") or ""
        if not base:
            continue
        a = assets.setdefault(base, {"symbol": base, "name": p.get("base_name") or base, "volume": 0.0,
                                     "prev_volume": 0.0, "price": None, "change": None, "product_id": None,
                                     "_ref": -1.0})
        a["volume"] += volume
        vchg = fnum(p.get("volume_percentage_change_24h"))
        a["prev_volume"] += volume / (1 + vchg / 100) if vchg is not None and vchg > -100 else volume
        # Price, 24h change and the candle product come from the deepest USD-like book.
        if volume > a["_ref"]:
            a.update(_ref=volume, price=fnum(p.get("price")), change=fnum(p.get("price_percentage_change_24h")),
                     product_id=p.get("product_id"))
    for a in assets.values():
        del a["_ref"]
    return assets


def crypto_overview(products, top_n=10):
    assets = crypto_assets(products)
    risk = sorted((a for a in assets.values() if a["symbol"] not in STABLES and a["volume"] > 0),
                  key=lambda a: a["volume"], reverse=True)
    stables = sorted((a for a in assets.values() if a["symbol"] in STABLES and a["volume"] > 0),
                     key=lambda a: a["volume"], reverse=True)
    flow = flow_summary(risk)
    major = sum(a["volume"] for a in risk if a["symbol"] in ("BTC", "ETH"))
    return {"risk": risk, "stables": stables, "flow": flow, "top": risk[:top_n], "count": len(risk),
            "major_share": major / flow["total"] * 100 if flow["total"] else None}


def candle_turnover(candle):
    """Quote turnover of one candle: base volume x typical price (high+low+close)/3."""
    high, low, close, volume = (fnum(candle.get(k)) or 0.0 for k in ("high", "low", "close", "volume"))
    return volume * (high + low + close) / 3


def crypto_window_flow(candles_by_symbol, window_end, hours=4):
    """Flow over the last `hours` complete hourly candles, plus the prior window's turnover.

    A candle closing above its open counts as inflow, below as outflow.
    """
    split = window_end - hours * 3600
    per_asset = []
    for symbol, candles in candles_by_symbol.items():
        current = [c for c in candles if split <= int(c["start"]) < window_end]
        inflow = outflow = turnover = 0.0
        for c in current:
            amount = candle_turnover(c)
            turnover += amount
            move = (fnum(c.get("close")) or 0) - (fnum(c.get("open")) or 0)
            inflow += amount if move > 0 else 0.0
            outflow += amount if move < 0 else 0.0
        prior = sum(candle_turnover(c) for c in candles if int(c["start"]) < split)
        first_open = fnum(current[0].get("open")) if current else None
        change = ((fnum(current[-1].get("close")) or 0) / first_open - 1) * 100 if first_open else None
        per_asset.append({"symbol": symbol, "turnover": turnover, "inflow": inflow, "outflow": outflow,
                          "net": inflow - outflow, "prior": prior, "change": change})
    total = sum(a["turnover"] for a in per_asset)
    prior_total = sum(a["prior"] for a in per_asset)
    inflow = sum(a["inflow"] for a in per_asset)
    outflow = sum(a["outflow"] for a in per_asset)
    ranked = sorted(per_asset, key=lambda a: a["net"], reverse=True)
    return {"hours": hours, "end": window_end, "assets": len(per_asset), "total": total,
            "inflow": inflow, "outflow": outflow, "net": inflow - outflow,
            "vs_prior": (total / prior_total - 1) * 100 if prior_total else None,
            "top_in": [a for a in ranked if a["net"] > 0][:3],
            "top_out": [a for a in reversed(ranked) if a["net"] < 0][:3]}


def us_session(info, now):
    """Which US session the quote data describes (constraint C6).

    open <= now < close -> intraday (today's cumulative volume)
    now >= close        -> that session is complete
    now < open          -> data is the previous trade date's full session
    """
    if not info:
        return dict(UNKNOWN_SESSION)
    try:
        open_at = dt.datetime.fromisoformat(info["openRaw"]).replace(tzinfo=ET)
        close_at = dt.datetime.fromisoformat(info["closeRaw"]).replace(tzinfo=ET)
    except (KeyError, TypeError, ValueError):
        return dict(UNKNOWN_SESSION)
    now_et = dt.datetime.fromtimestamp(now, ET)
    if open_at <= now_et < close_at:
        return {"state": "intraday", "trade_date": open_at.date().isoformat(), "as_of": now_et.strftime("%H:%M")}
    if now_et >= close_at:
        return {"state": "closed", "trade_date": open_at.date().isoformat(), "as_of": None}
    try:
        previous = dt.datetime.strptime(info.get("previousTradeDate") or "", "%b %d, %Y").date().isoformat()
    except ValueError:
        previous = None
    return {"state": "previous", "trade_date": previous, "as_of": None}
