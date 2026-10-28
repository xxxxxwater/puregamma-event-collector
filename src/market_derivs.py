"""Coinbase Derivatives analytics (pure): perps funding / open interest / premium,
dated-futures basis, and the cross-asset board (index perps, metals, energy).

Funding is per `funding_interval` (3600s on CDE); annualized = rate x intervals per year.
Open interest is in contracts; notional = contracts x contract_size x price.
"""
import datetime as dt

from market_analysis import fnum

YEAR_SECONDS = 365 * 86400
MACRO = (  # (contract code, Chinese label, kind, English label) -- nearest active contract per code
    ("US5", "美股500", "index", "US 500"), ("TEK", "科技100", "index", "Tech 100"),
    ("AIP", "AI 指数", "index", "AI Index"), ("CHN", "中国指数", "index", "China Index"),
    ("DEF", "国防指数", "index", "Defense Index"), ("MC", "七巨头+加密", "index", "Mag7 + Crypto"),
    ("GOL", "黄金", "commodity", "Gold"), ("SLR", "白银", "commodity", "Silver"), ("PT", "铂金", "commodity", "Platinum"),
    ("CU", "铜", "commodity", "Copper"), ("NOL", "原油", "commodity", "Crude Oil"), ("NGS", "天然气", "commodity", "Natural Gas"),
)
CRYPTO_PERPS = ("BTC", "ETH", "SOL", "XRP")


def parse_contract(product):
    d = product.get("future_product_details") or {}
    price = fnum(product.get("price")) or fnum(product.get("mid_market_price"))
    if not price:
        return None
    size = fnum(d.get("contract_size")) or 0.0
    oi = fnum(d.get("open_interest")) or 0.0
    interval = str(d.get("funding_interval") or "")
    interval_s = fnum(interval[:-1]) if interval.endswith("s") else None
    perp = "Perpetual" in (d.get("display_name") or "") or (d.get("funding_rate") not in (None, "") and bool(interval_s))
    try:
        expiry = int(dt.datetime.fromisoformat(str(d.get("contract_expiry")).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        expiry = None
    asset_type = str(d.get("futures_asset_type") or "").replace("FUTURES_ASSET_TYPE_", "").lower() or "crypto"
    return {"product_id": product.get("product_id"), "code": d.get("contract_code") or "",
            "root": d.get("contract_root_unit") or "", "asset_type": asset_type, "perp": perp,
            "name": d.get("display_name") or product.get("product_id"), "price": price,
            "index_price": fnum(d.get("index_price")), "funding": fnum(d.get("funding_rate")),
            "funding_interval": interval_s or 3600.0, "oi": oi, "oi_usd": oi * size * price,
            "chg24": fnum(product.get("price_percentage_change_24h")),
            "volume24": fnum(product.get("approximate_quote_24h_volume")) or 0.0, "expiry": expiry}


def parse_contracts(products):
    return [c for c in (parse_contract(p) for p in products) if c]


def annualized_funding(contract):
    if contract["funding"] is None:
        return None
    return contract["funding"] * YEAR_SECONDS / contract["funding_interval"] * 100


def premium(contract):
    idx = contract["index_price"]
    return (contract["price"] / idx - 1) * 100 if idx else None


def change_pct(now_value, then_value):
    return (now_value / then_value - 1) * 100 if now_value and then_value else None


def crypto_perps(contracts, past_4h=None, past_24h=None):
    """Per-asset perp metrics for crypto perps, largest open interest first."""
    past_4h, past_24h = past_4h or {}, past_24h or {}
    rows = []
    for c in contracts:
        if not c["perp"] or c["asset_type"] != "crypto":
            continue
        then4, then24 = past_4h.get(c["product_id"]), past_24h.get(c["product_id"])
        rows.append({"asset": c["root"], "product_id": c["product_id"], "price": c["price"], "oi_usd": c["oi_usd"],
                     "funding_ann": annualized_funding(c), "premium": premium(c), "chg24": c["chg24"],
                     "oi_chg4": change_pct(c["oi"], then4["oi_contracts"]) if then4 else None,
                     "oi_chg24": change_pct(c["oi"], then24["oi_contracts"]) if then24 else None})
    return sorted(rows, key=lambda r: r["oi_usd"], reverse=True)


def term_structure(contracts, spot_prices, now, roots=("BTC", "ETH")):
    """Annualized basis of dated (non-perp) crypto futures with open interest vs spot."""
    out = {}
    for c in contracts:
        if c["perp"] or c["asset_type"] != "crypto" or c["root"] not in roots or not c["oi"] or not c["expiry"]:
            continue
        spot = spot_prices.get(c["root"])
        days = (c["expiry"] - now) / 86400
        if not spot or days < 7:          # annualising a few days of basis turns noise into huge numbers
            continue
        basis = (c["price"] / spot - 1) * 100
        out.setdefault(c["root"], []).append({"product_id": c["product_id"], "expiry": c["expiry"], "days": days,
                                              "basis": basis, "annualized": basis * 365 / days, "oi": c["oi"]})
    return {root: sorted(rows, key=lambda r: r["expiry"])[:2] for root, rows in out.items()}


def macro_board(contracts):
    """Nearest active contract per macro code: index perps, metals, energy."""
    by_code = {}
    for c in contracts:
        if c["asset_type"] == "crypto" or not c["price"]:
            continue
        by_code.setdefault(c["code"], []).append(c)
    board = []
    for code, label, kind, label_en in MACRO:
        options = by_code.get(code) or []
        if not options:
            continue
        best = max(options, key=lambda c: (c["perp"], c["oi"] > 0, -(c["expiry"] or 0)))
        board.append({"code": code, "label": label, "label_en": label_en, "kind": kind, "price": best["price"],
                      "chg24": best["chg24"],
                      "funding_ann": annualized_funding(best) if best["perp"] else None, "oi_usd": best["oi_usd"],
                      "product_id": best["product_id"], "perp": best["perp"]})
    return board


def find_perp(contracts, code):
    return next((c for c in contracts if c["perp"] and c["code"] == code), None)


def deriv_anomalies(perps, macro, now):
    day = dt.datetime.fromtimestamp(now, dt.timezone.utc).date().isoformat()
    found = []

    def add(symbol, typ, direction, severity, major, text, text_en):
        found.append({"key": f"{typ}:{symbol}:{'up' if direction > 0 else 'down'}:{day}", "kind": "deriv",
                      "symbol": symbol, "type": typ, "direction": direction, "severity": severity,
                      "major": major, "text": text, "text_en": text_en})

    for p in perps:
        if p["oi_usd"] < 5e6:
            continue
        f = p["funding_ann"]
        if f is not None and (f >= 30 or f <= -20):
            add(p["asset"], "funding", f, abs(f) / 30, abs(f) >= 60 and p["asset"] in ("BTC", "ETH"),
                f"{p['asset']} 永续资金费率年化 {f:+.0f}%（{'多头' if f > 0 else '空头'}拥挤）",
                f"{p['asset']} perp funding {f:+.0f}% annualized ({'crowded longs' if f > 0 else 'crowded shorts'})")
        if p["oi_chg4"] is not None and p["oi_usd"] >= 1e7 and abs(p["oi_chg4"]) >= 15:
            add(p["asset"], "oi", p["oi_chg4"], abs(p["oi_chg4"]) / 15, abs(p["oi_chg4"]) >= 30,
                f"{p['asset']} 永续持仓 4 小时 {p['oi_chg4']:+.0f}%", f"{p['asset']} perp open interest {p['oi_chg4']:+.0f}% in 4h")
        if p["premium"] is not None and abs(p["premium"]) >= 0.5:
            add(p["asset"], "premium", p["premium"], abs(p["premium"]) / 0.5, abs(p["premium"]) >= 1.5,
                f"{p['asset']} 永续{'溢价' if p['premium'] > 0 else '折价'} {p['premium']:+.2f}%",
                f"{p['asset']} perp {'premium' if p['premium'] > 0 else 'discount'} {p['premium']:+.2f}%")
    for m in macro:
        if m["chg24"] is None:
            continue
        limit = 3.0 if m["kind"] == "index" else 4.0
        if abs(m["chg24"]) >= limit:
            add(m["code"], "macro", m["chg24"], abs(m["chg24"]) / limit, abs(m["chg24"]) >= limit * 1.7,
                f"{m['label']} 24 小时 {m['chg24']:+.1f}%", f"{m.get('label_en', m['label'])} {m['chg24']:+.1f}% in 24h")
    return sorted(found, key=lambda a: a["severity"], reverse=True)
