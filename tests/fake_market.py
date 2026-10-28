"""Deterministic stand-in for every public endpoint market-report calls (tests only)."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import market_sources as sources

SLOT = 1_793_160_000          # 2026-10-28 04:00 UTC = 12:00 Beijing, 00:00 ET
INFO = {"openRaw": "2026-10-28T09:30:00", "closeRaw": "2026-10-28T16:00:00", "previousTradeDate": "Oct 27, 2026",
        "pmOpenRaw": "2026-10-28T04:00:00", "ahCloseRaw": "2026-10-28T20:00:00", "mrktStatus": "Closed"}


def _spot(symbol, volume, change):
    return {"product_id": f"{symbol}-USD", "product_type": "SPOT", "base_currency_id": symbol,
            "quote_currency_id": "USD", "status": "online", "alias": "", "price": "100",
            "approximate_quote_24h_volume": str(volume), "price_percentage_change_24h": str(change),
            "volume_percentage_change_24h": "10", "base_name": symbol}


def _future(pid, code, root, kind, price, perp, expiry, funding=""):
    return {"product_id": pid, "price": price, "price_percentage_change_24h": "1.0",
            "approximate_quote_24h_volume": "1000000",
            "future_product_details": {"contract_code": code, "contract_root_unit": root,
                                       "display_name": "X Perpetual" if perp else "X Futures",
                                       "futures_asset_type": "FUTURES_ASSET_TYPE_" + kind, "contract_size": "0.01",
                                       "open_interest": "100000", "funding_rate": funding,
                                       "funding_interval": "3600s" if perp else "", "index_price": price,
                                       "contract_expiry": expiry}}


SPOT = [_spot("BTC", 3e8, 1.5), _spot("ETH", 1.5e8, -0.5)] + [_spot(f"C{i}", 1e8 - i * 1e6, 1 if i % 2 else -1)
                                                              for i in range(12)]
FUTURES = [_future("BIP-20DEC30-CDE", "BIP", "BTC", "CRYPTO", "100", True, "2089-12-30T16:00:00Z", "0.000002"),
           _future("ETP-20DEC30-CDE", "ETP", "ETH", "CRYPTO", "100", True, "2089-12-30T16:00:00Z", "0.00001"),
           _future("BIT-27NOV26-CDE", "BIT", "BTC", "CRYPTO", "101", False, "2026-11-27T16:00:00Z"),
           _future("US5-19DEC30-CDE", "US5", "CDEUS5", "STOCKS", "3100", True, "2030-12-19T16:00:00Z", "-0.00006"),
           _future("GOL-25NOV26-CDE", "GOL", "CDEGLD", "METALS", "4000", False, "2026-11-25T16:00:00Z")]
STOCKS = [f"S{i}" for i in range(1100)]
ETFS = [f"E{i}" for i in range(300)] + ["SPY", "QQQ"]
EQUITY = ([{"equity_product_details": {"ticker": t, "equity_subtype": "EQUITY_PRODUCT_SUBTYPE_COMMON_STOCK"}}
           for t in STOCKS] +
          [{"equity_product_details": {"ticker": t, "equity_subtype": "EQUITY_PRODUCT_SUBTYPE_ETF"}} for t in ETFS])


class FakeMarket:
    def __init__(self, status="Pre Market", change=5.0, volume=1_000_000):
        self.down, self.calls = set(), []
        self.status, self.change, self.volume = status, change, volume

    def __call__(self, url, params=None, headers=None, timeout=30):
        self.calls.append(url)
        for name in self.down:
            if name in url:
                raise sources.SourceError("HTTP 503 from " + name)
        params = params or {}
        if url.endswith("/market/products"):
            products = {"SPOT": SPOT, "FUTURE": FUTURES, "EQUITY": EQUITY}[params["product_type"]]
            return {"products": products, "pagination": {"has_next": False}}
        if url.endswith("/candles"):
            start, end = params["start"], params["end"]
            return {"candles": [{"start": str(t), "open": str(100 + t / 3.6e6), "high": str(101 + t / 3.6e6),
                                 "low": str(99 + t / 3.6e6), "close": str(100.05 + t / 3.6e6), "volume": "10"}
                                for t in range(start - start % 3600, end + 1, 3600)]}
        if url.endswith("/ticker"):
            start, end = params["start"], params["end"]
            times = [start + (end - start) * k / 20 for k in range(20)]
            iso = lambda ts: dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            trades = [{"trade_id": str(int(ts * 1000)), "time": iso(ts), "price": str(100 + (k % 2)), "size": "1",
                       "side": "SELL" if k % 2 else "BUY"} for k, ts in enumerate(times)]
            return {"trades": list(reversed(trades))}
        if url.endswith("/product_book"):
            return {"pricebook": {"bids": [{"price": "99.9", "size": "10"}], "asks": [{"price": "100.1", "size": "5"}]}}
        if url.endswith("/screener/stocks"):
            return {"data": {"rows": [{"symbol": t, "name": f"Stock {t}", "lastsale": "$10", "volume": str(1000 + i),
                                       "marketCap": "1000000", "sector": "Technology" if i % 2 else "Energy",
                                       "industry": "Semiconductors" if i % 2 else "Integrated oil Companies"}
                                      for i, t in enumerate(STOCKS)]}}
        if url.endswith("/screener/etf"):
            names = ETFS + [f"X{i}" for i in range(1000)]
            return {"data": {"data": {"rows": [{"symbol": t, "companyName": f"{t} Treasury Bond ETF"} for t in names]}}}
        if url.endswith("/quote/watchlist"):
            rows = []
            for item in params["symbol"][:20]:
                ticker = item.split("|")[0].upper()
                last = 10.0 * (1 + self.change / 100)
                rows.append({"symbol": ticker, "lastSalePrice": f"${last:.2f}", "netChange": f"{last - 10:+.2f}",
                             "percentageChange": f"{self.change:+.2f}%", "volume": f"{self.volume:,}",
                             "previousClosePrice": 10.0 if self.status == "Market Open" else 9.5,
                             "openingPrice": 10.0, "marketStatus": self.status,
                             "lastTradeTimestampDateTime": "2026-10-28T00:00:00-04:00"})
            return {"data": rows}
        if url.endswith("/historical"):
            return {"data": {"tradesTable": {"rows": [{"date": f"10/{d:02d}/2026", "close": "$10.00",
                                                       "volume": "900,000"} for d in range(1, 28)]}}}
        if url.endswith("/market-info"):
            return {"data": INFO}
        raise AssertionError("unexpected url " + url)
