"""Read-only public market data sources: Coinbase market endpoints and Nasdaq.

Network access only (retries, timeouts, rate limits, paging); no analysis here.
No API key is used anywhere. Field semantics verified on 2026-10-28 are noted
inline and in market/DESIGN.md section 3.
"""
import datetime as dt
import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

LOG = logging.getLogger("market.sources")
COINBASE_MARKET = "https://api.coinbase.com/api/v3/brokerage/market"
NASDAQ_API = "https://api.nasdaq.com/api"
COINBASE_UA = "puregamma-market-report/2.0"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
NASDAQ_HEADERS = {"User-Agent": BROWSER_UA}
MIN_NASDAQ_ROWS = 1000
GRANULARITY = {"ONE_MINUTE": 60, "FIVE_MINUTE": 300, "FIFTEEN_MINUTE": 900, "THIRTY_MINUTE": 1800,
               "ONE_HOUR": 3600, "TWO_HOUR": 7200, "SIX_HOUR": 21600, "ONE_DAY": 86400}
MAX_CANDLES = 350
TRADES_PAGE = 1000
WATCHLIST_BATCH = 20          # the endpoint silently answers only the first 20 symbols


class SourceError(RuntimeError):
    """A data source failed after retries; the message never contains secrets."""


def _retry_after(error):
    try:
        return max(1, int(error.headers.get("Retry-After", "")))
    except (TypeError, ValueError, AttributeError):
        return None


def get_json(url, params=None, headers=None, timeout=30, attempts=3, sleep=time.sleep):
    """GET JSON with bounded retries on 429/5xx/network errors (list params repeat the key)."""
    full = url + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    request_headers = {"Accept": "application/json", "User-Agent": COINBASE_UA}
    request_headers.update(headers or {})
    where = urllib.parse.urlsplit(url).netloc + urllib.parse.urlsplit(url).path
    delay = 2
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(full, headers=request_headers), timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as ex:
            retryable, wait, reason = ex.code == 429 or ex.code >= 500, _retry_after(ex) or delay, "HTTP %d" % ex.code
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as ex:
            # OSError covers timeouts/resets; ValueError covers truncated or non-JSON bodies.
            retryable, wait, reason = True, delay, type(ex).__name__
        if not retryable or attempt == attempts:
            raise SourceError(f"{reason} from {where}")
        LOG.info("retrying %s after %s (attempt %d/%d)", where, reason, attempt, attempts)
        sleep(min(wait, 30))
        delay *= 2


def iso_ts(value):
    """'2026-10-28T08:25:56.401154Z' -> epoch seconds (float)."""
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


# ---------- Coinbase ----------

def coinbase_products(product_type, max_pages=60, fetch=get_json):
    """Yield Coinbase products page by page; pages are large, so never hold them all."""
    cursor = ""
    for _ in range(max_pages):
        params = {"product_type": product_type, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = fetch(COINBASE_MARKET + "/products", params)
        yield from data.get("products") or []
        page = data.get("pagination") or {}
        cursor = page.get("next_cursor") or ""
        if not page.get("has_next") or not cursor:
            return


def coinbase_candles(product_id, granularity, start, end, fetch=get_json):
    """Candles with start <= candle start < end, ascending; pages through the 350-candle cap."""
    step = GRANULARITY[granularity]
    start, end = int(start), int(end)
    found, cursor = {}, start
    while cursor < end:
        chunk_end = min(end, cursor + MAX_CANDLES * step)
        data = fetch(COINBASE_MARKET + "/products/" + urllib.parse.quote(product_id) + "/candles",
                     {"granularity": granularity, "start": cursor, "end": chunk_end})
        for candle in data.get("candles") or []:
            begin = int(candle.get("start") or 0)
            if start <= begin < end:
                found[begin] = candle
        cursor = chunk_end
    return [found[t] for t in sorted(found)]


def coinbase_hourly_candles(product_id, start, end, fetch=get_json):
    return coinbase_candles(product_id, "ONE_HOUR", start, end, fetch=fetch)


def coinbase_trades(product_id, start, end, max_pages=10, fetch=get_json):
    """Market trades with start <= time < end, walking back from `end` in pages of 1000.

    Returns (trades, covered_from): each trade gains a float `ts`. covered_from > start
    means the page cap stopped the walk and [start, covered_from) was not covered.
    NOTE: `side` is the MAKER order side (tick test, 2026-10-28): the taker bought when
    side == 'SELL'. market_crypto converts this; do not use `side` as aggressor directly.
    """
    seen, upper, covered_from = {}, int(end), float(end)
    for _ in range(max_pages):
        data = fetch(COINBASE_MARKET + "/products/" + urllib.parse.quote(product_id) + "/ticker",
                     {"limit": TRADES_PAGE, "start": int(start), "end": upper})
        page = data.get("trades") or []
        oldest, fresh = None, 0
        for trade in page:
            try:
                ts = iso_ts(trade["time"])
            except (KeyError, ValueError):
                continue
            if ts < start or ts >= end or trade.get("trade_id") in seen:
                continue
            seen[trade.get("trade_id")] = dict(trade, ts=ts)
            fresh += 1
            oldest = ts if oldest is None else min(oldest, ts)
        if len(page) < TRADES_PAGE:
            return list(seen.values()), float(start)
        if oldest is not None:
            covered_from = min(covered_from, oldest)
        # Same-second trades can straddle pages: re-include that second, dedupe by trade_id.
        upper = int(oldest) + 1 if fresh and oldest is not None and int(oldest) + 1 < upper else upper - 1
        if upper <= start:
            return list(seen.values()), float(start)
    return list(seen.values()), covered_from


def coinbase_book(product_id, limit=500, fetch=get_json):
    return (fetch(COINBASE_MARKET + "/product_book", {"product_id": product_id, "limit": limit}) or {}).get("pricebook") or {}


def coinbase_futures(fetch=get_json):
    return list(coinbase_products("FUTURE", max_pages=5, fetch=fetch))


# ---------- Nasdaq ----------

def nasdaq_quotes(fetch=get_json):
    """Stock screener: sector/industry/market cap for every listing. Prices lag ~1 session (C1)."""
    data = fetch(NASDAQ_API + "/screener/stocks", {"tableonly": "true", "limit": 10000, "download": "true"},
                 headers=NASDAQ_HEADERS, timeout=45)
    rows = (data.get("data") or {}).get("rows") or []
    if len(rows) < MIN_NASDAQ_ROWS:
        raise SourceError("Nasdaq screener returned only %d rows" % len(rows))
    return rows


def nasdaq_etfs(fetch=get_json):
    """ETF screener: names for classification (no volume in this feed)."""
    data = fetch(NASDAQ_API + "/screener/etf", {"tableonly": "true", "limit": 10000, "download": "true"},
                 headers=NASDAQ_HEADERS, timeout=45)
    rows = ((data.get("data") or {}).get("data") or {}).get("rows") or []
    if len(rows) < MIN_NASDAQ_ROWS:
        raise SourceError("Nasdaq ETF screener returned only %d rows" % len(rows))
    return rows


def nasdaq_watchlist(symbols, fetch=get_json, sleep=time.sleep, pause=0.25, max_failures=None):
    """Live quotes for [(ticker, 'stocks'|'etf'), ...] in batches of 20 -> {TICKER: row}.

    Unknown symbols are skipped by the endpoint. A few failed batches are tolerated;
    more than max_failures (default: a quarter of batches, at least 2) raises.
    """
    batches = [symbols[i:i + WATCHLIST_BATCH] for i in range(0, len(symbols), WATCHLIST_BATCH)]
    limit = max_failures if max_failures is not None else max(2, len(batches) // 4)
    rows, failures = {}, 0
    for index, batch in enumerate(batches):
        try:
            data = fetch(NASDAQ_API + "/quote/watchlist", {"symbol": [f"{t.lower()}|{c}" for t, c in batch]},
                         headers=NASDAQ_HEADERS, timeout=30)
        except SourceError:
            failures += 1
            if failures > limit:
                raise
            continue
        for row in data.get("data") or []:
            ticker = (row.get("symbol") or "").strip().upper()
            if ticker:
                rows[ticker] = row
        if pause and index + 1 < len(batches):
            sleep(pause)
    return rows


def nasdaq_history(symbol, assetclass, fromdate, todate, limit=60, fetch=get_json):
    """Daily bars, newest first: [{'date': 'MM/DD/YYYY', 'close': '$1.00', 'volume': '1,000', ...}]."""
    data = fetch(NASDAQ_API + "/quote/" + urllib.parse.quote(symbol) + "/historical",
                 {"assetclass": assetclass, "fromdate": fromdate, "todate": todate, "limit": limit},
                 headers=NASDAQ_HEADERS, timeout=30)
    return (((data.get("data") or {}).get("tradesTable") or {}).get("rows")) or []


def nasdaq_market_info(fetch=get_json):
    data = fetch(NASDAQ_API + "/market-info", headers=NASDAQ_HEADERS, timeout=20)
    info = data.get("data") or {}
    if not info.get("openRaw") or not info.get("closeRaw"):
        raise SourceError("Nasdaq market-info missing session times")
    return info
