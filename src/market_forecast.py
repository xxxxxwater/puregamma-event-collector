"""Market outlook model v1 (pure): 24h volatility range + direction tilt, backtest, evaluation.

Honesty rules (DESIGN C7): every live outlook is stored and scored 24h later, the
price-only part is backtested on history, and both hit rates are always shown.
A direction hit rate near 50% means no reliable edge; the range is the sturdier output.
"""
import math

WEIGHTS = {"trend": 0.30, "momentum": 0.20, "taker": 0.15, "book": 0.05, "funding": 0.10, "oi": 0.10,
           "breadth": 0.05, "macro": 0.05}
FACTOR_ZH = {"trend": "趋势", "momentum": "动量", "taker": "主动买卖", "book": "盘口", "funding": "资金费率",
             "oi": "持仓", "breadth": "市场宽度", "macro": "美股"}
THRESHOLD = 20
HORIZON_H = 24
EMA_SPAN = 168
HALFLIFE_H = 24


def clip(value, low=-1.0, high=1.0):
    return max(low, min(high, value))


def series_stats(closes, ema_span=EMA_SPAN, halflife=HALFLIFE_H):
    """Running EMA of price and EWMA volatility of hourly log returns, index-aligned with closes."""
    alpha, lam = 2 / (ema_span + 1), 0.5 ** (1 / halflife)
    ema, sigma, e, var = [], [], None, None
    for i, price in enumerate(closes):
        e = price if e is None else alpha * price + (1 - alpha) * e
        if i and closes[i - 1] > 0 and price > 0:
            r = math.log(price / closes[i - 1])
            var = r * r if var is None else lam * var + (1 - lam) * r * r
        ema.append(e)
        sigma.append(math.sqrt(var) if var else None)
    return ema, sigma


def price_factors(closes, ema, sigma, i):
    """Trend (vs 7-day EMA) and 24h momentum, each scaled so +-2 sigma maps to +-1."""
    if i < EMA_SPAN or not sigma[i] or closes[i] <= 0 or closes[i - 24] <= 0:
        return None
    s = sigma[i]
    return {"trend": clip((closes[i] / ema[i] - 1) / (s * math.sqrt(EMA_SPAN)) / 2),
            "momentum": clip(math.log(closes[i] / closes[i - 24]) / (s * math.sqrt(24)) / 2)}


def live_factors(taker_share=None, book_ratio=None, funding_ann=None, oi_chg24=None, ret_24h=None,
                 breadth=None, macro_ret=None):
    """Flow and positioning factors; None when the input is unavailable."""
    out = {}
    if taker_share is not None:
        out["taker"] = clip((taker_share - 50) / 10)          # 60% taker buys -> +1
    if book_ratio:
        out["book"] = clip(math.log(book_ratio) / math.log(2))  # 2x resting bids -> +1
    if funding_ann is not None:
        out["funding"] = clip(-funding_ann / 40)              # crowded longs (+40%/yr) -> -1 (contrarian)
    if oi_chg24 is not None and ret_24h:
        out["oi"] = clip(math.copysign(1, ret_24h) * oi_chg24 / 10)  # OI growing with the move confirms it
    if breadth is not None:
        out["breadth"] = clip((breadth - 50) / 25)
    if macro_ret is not None:
        out["macro"] = clip(macro_ret / 1.5)
    return out


def score(factors, weights=WEIGHTS):
    """Weighted factor score in [-100, 100]; missing factors are dropped and weights renormalised."""
    used = {k: v for k, v in factors.items() if v is not None and k in weights}
    total = sum(weights[k] for k in used)
    if not total:
        return 0.0, []
    value = sum(weights[k] * v for k, v in used.items()) / total * 100
    parts = sorted(((k, weights[k] * v / total) for k, v in used.items()), key=lambda kv: abs(kv[1]), reverse=True)
    return value, parts


def direction(value, threshold=THRESHOLD):
    return 1 if value >= threshold else -1 if value <= -threshold else 0


def outlook(closes, live=None, horizon=HORIZON_H):
    """Outlook from ascending hourly closes (>= 170) plus optional live factors."""
    ema, sigma = series_stats(closes)
    base = price_factors(closes, ema, sigma, len(closes) - 1)
    if base is None:
        return None
    factors = dict(base, **(live or {}))
    value, parts = score(factors)
    price, s = closes[-1], sigma[-1]
    band = s * math.sqrt(horizon)
    return {"price": price, "score": value, "direction": direction(value), "sigma": s,
            "low": price * math.exp(-band), "high": price * math.exp(band), "band_pct": (math.exp(band) - 1) * 100,
            "factors": factors, "parts": parts}


def backtest(closes, horizon=HORIZON_H, step=6, threshold=THRESHOLD):
    """Price-only walk-forward test: direction hit rate of signals and 1-sigma range coverage.

    Sampled every `step` hours to limit overlap between 24h outcomes.
    """
    ema, sigma = series_stats(closes)
    samples = signals = hits = covered = 0
    for i in range(EMA_SPAN, len(closes) - horizon, step):
        base = price_factors(closes, ema, sigma, i)
        if base is None:
            continue
        value, _ = score(base)
        realized = math.log(closes[i + horizon] / closes[i])
        samples += 1
        covered += abs(realized) <= sigma[i] * math.sqrt(horizon)
        d = direction(value, threshold)
        if d:
            signals += 1
            hits += (realized > 0) == (d > 0)
    hit_rate = hits / signals * 100 if signals else None
    effective = signals * step / horizon            # overlapping outcomes: ~1 independent per horizon
    z = (hit_rate - 50) / (50 / math.sqrt(effective)) if hit_rate is not None and effective >= 1 else None
    return {"samples": samples, "signals": signals, "hit_rate": hit_rate, "effective": round(effective, 1),
            "significant": z is not None and abs(z) >= 2, "coverage": covered / samples * 100 if samples else None,
            "hours": len(closes)}


def evaluate(forecast, price_then):
    """Score a stored outlook against the price at its horizon."""
    realized = (price_then / forecast["price"] - 1) * 100
    hit = None if not forecast["direction"] else int((realized > 0) == (forecast["direction"] > 0))
    return realized, hit, int(forecast["low"] <= price_then <= forecast["high"])
