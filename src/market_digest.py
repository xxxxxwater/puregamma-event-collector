"""Structured, channel-neutral digests of each snapshot part (stored in snapshot metrics).

Telegram uses pre-rendered sections; e-mail (market_email) and any future channel render
from these compact JSON digests instead. Numbers are rounded to keep snapshots small.
"""
import market_render as render


def _r(value, digits=2):
    return None if value is None else round(float(value), digits)


def _anomalies(items, limit=20):
    ranked = sorted(items, key=lambda a: -a["severity"])[:limit]
    return [{"text": a["text"], "text_en": a.get("text_en") or a["text"], "major": bool(a.get("major")),
             "kind": a["kind"], "symbol": a["symbol"]} for a in ranked]


def crypto(overview, window, taker, books, anomalies, now):
    f = overview["flow"]
    return {
        "at": int(now), "total_24h": _r(f["total"], 0), "vol_change": _r(f["vol_change"]), "flow_24h": _r(f["flow"], 0),
        "advancers": f["advancers"], "decliners": f["decliners"], "major_share": _r(overview["major_share"], 1),
        "stables": [[s["symbol"], _r(s["volume"], 0)] for s in overview["stables"][:3]],
        "top": [[a["symbol"], _r(a["price"], 8), _r(a["change"]), _r(a["volume"], 0)] for a in overview["top"]],
        "taker": None if not taker or taker["buy"] + taker["sell"] <= 0 else {
            "buy": _r(taker["buy"], 0), "sell": _r(taker["sell"], 0), "net": _r(taker["net"], 0),
            "buy_share": _r(taker["buy_share"], 1), "coverage": _r(taker["coverage"], 3),
            "rows": [[r["symbol"], _r(r["buy_share"], 1)] for r in taker["rows"][:6] if r["buy_share"] is not None]},
        "window": None if not window else {
            "hours": window["hours"], "end": window["end"], "total": _r(window["total"], 0), "net": _r(window["net"], 0),
            "vs_prior": _r(window["vs_prior"]), "top_in": [[a["symbol"], _r(a["net"], 0)] for a in window["top_in"]],
            "top_out": [[a["symbol"], _r(a["net"], 0)] for a in window["top_out"]]},
        "books": [[sym, _r(b["ratio"])] for sym, b in (books or {}).items() if b and b.get("ratio")][:5],
        "anomalies": _anomalies(anomalies),
    }


def deriv(perps, term, total_oi, total_chg24, macro, anomalies, now):
    return {
        "at": int(now), "total_oi": _r(total_oi, 0), "total_chg24": _r(total_chg24, 1),
        "perps": [{"asset": p["asset"], "funding_ann": _r(p["funding_ann"], 1), "oi_usd": _r(p["oi_usd"], 0),
                   "oi_chg4": _r(p["oi_chg4"], 1), "oi_chg24": _r(p["oi_chg24"], 1), "premium": _r(p["premium"], 3)}
                  for p in perps[:4]],
        "term": {root: [[r["expiry"], _r(r["annualized"], 1)] for r in rows] for root, rows in term.items()},
        "macro": [{"label": m["label"], "label_en": m.get("label_en", m["label"]), "kind": m["kind"],
                   "price": _r(m["price"], 4), "chg24": _r(m["chg24"])} for m in macro],
        "anomalies": _anomalies(anomalies),
    }


def equity(overview, session, anomalies, data_at):
    def summary(s):
        return {k: (_r(v, 0) if isinstance(v, float) and k != "wchange" else _r(v) if k == "wchange" else v)
                for k, v in s.items()}

    def groups(items, n=None):
        chosen = items if n is None else items[:n]
        return [[g["name"], _r(g["flow"], 0), _r(g["change"], 2), g["count"]] for g in chosen]

    inds = overview["industries"]
    return {
        "at": int(data_at), "session": dict(session, label=render.session_label(session)),
        "stocks": summary(overview["stocks"]), "etfs": summary(overview["etfs"]),
        "top": [[r["symbol"], _r(r["price"], 4), _r(r["change"]), _r(r["turnover"], 0), r["sector"]] for r in overview["top"]],
        "inflow": [[r["symbol"], _r(r["flow"], 0), _r(r["change"])] for r in overview["inflow"]],
        "outflow": [[r["symbol"], _r(r["flow"], 0), _r(r["change"])] for r in overview["outflow"]],
        "gainers": [[r["symbol"], _r(r["change"])] for r in overview["gainers"]],
        "losers": [[r["symbol"], _r(r["change"])] for r in overview["losers"]],
        "sectors": groups(overview["sectors"]),
        "industries_in": groups([g for g in inds if g["flow"] > 0], 5),
        "industries_out": groups([g for g in reversed(inds) if g["flow"] < 0], 5),
        "categories": groups(overview["categories"]),
        "etf_in": [[r["symbol"], _r(r["flow"], 0)] for r in overview["etf_in"]],
        "etf_out": [[r["symbol"], _r(r["flow"], 0)] for r in overview["etf_out"]],
        "etf_top": [[r["symbol"], _r(r["change"]), _r(r["turnover"], 0), r["category"]] for r in overview["etf_top"]],
        "anomalies": _anomalies(anomalies),
    }


def forecast(labels, outlooks, records, backtests, at):
    items = []
    for label in labels:
        o = outlooks.get(label)
        items.append(None if not o else {
            "label": label, "price": _r(o["price"], 6), "score": _r(o["score"], 1), "direction": o["direction"],
            "low": _r(o["low"], 6), "high": _r(o["high"], 6), "band_pct": _r(o["band_pct"], 2),
            "parts": [[k, _r(v, 3)] for k, v in o["parts"][:5]], "record": records.get(label) or {},
            "backtest": backtests.get(label) or {}})
    return {"at": int(at), "labels": labels, "items": items}
