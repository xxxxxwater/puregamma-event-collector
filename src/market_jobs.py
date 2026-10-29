"""Collection jobs: each fetches one source, persists it, and records its health (DESIGN §4).

Jobs run in list order inside the service loop, each on its own cadence; a failing
job is retried after 5 minutes and never stops the others. Raw payloads that only the
next snapshot needs live in `cache`; history the analytics need goes to SQLite.
"""
import datetime as dt
import json
import logging
import time

import market_analysis as analysis
import market_crypto as crypto
import market_derivs as derivs
import market_equities as equities
import market_forecast as forecast
import market_sources as sources
import market_store as store

LOG = logging.getLogger("market.jobs")
HOUR = 3600
DAY = 86400
TICK_BUDGET = 8 * 60       # seconds of jobs per loop round before the rest wait a tick
TOP_CANDLES = 30
TOP_TRADES = 6
TOP_BOOKS = 5
TRADE_PAGES = {"BTC": 15, "ETH": 10}
DEFAULT_TRADE_PAGES = 5
CANDLE_DAYS = 8
TARGET_DAYS = 60
ETF_SWEEP_SYMBOLS = 800       # 40 watchlist calls per run
BACKFILL_PER_RUN = 20
BACKFILL_MIN_BARS = 15
MIN_TRADABLE = 1000


class Job:
    def __init__(self, name, fn, every):
        self.name, self.fn, self.every = name, fn, every

    def interval(self, now):
        return self.every(now) if callable(self.every) else self.every


class Collector:
    def __init__(self, con, fetch=sources.get_json, sleep=time.sleep):
        self.con, self.fetch, self.sleep = con, fetch, sleep
        self.cache = {}
        self.next_run = {}
        self.jobs = [
            Job("spot", self.spot, 900), Job("futures", self.futures, 900), Job("candles", self.candles, 900),
            Job("trades", self.trades, 900), Job("books", self.books, 900), Job("market_info", self.market_info, 900),
            Job("cb_equity_list", self.cb_equity_list, 6 * HOUR), Job("equity_meta", self.equity_meta, DAY),
            Job("etf_sweep", self.etf_sweep, self._sweep_interval), Job("universe", self.universe, 6 * HOUR),
            Job("equity_live", self.equity_live, self._live_interval),
            Job("equity_backfill", self.equity_backfill, self._backfill_interval),
            Job("forecast", self.forecast, HOUR), Job("backtest", self.backtest, DAY), Job("prune", self.prune, HOUR),
        ]

    # ----- scheduling -----

    def run_due(self, now, budget=TICK_BUDGET, clock=time.monotonic):
        """Run due jobs in order; returns the names of the jobs that ran. After `budget` seconds no
        new job starts: the rest run next tick (those that ran are no longer due), so briefs and
        snapshots never wait behind a slow round."""
        ran, started = [], clock()
        for job in self.jobs:
            if now >= self.next_run.get(job.name, 0):
                if ran and clock() - started > budget:
                    LOG.warning("tick budget spent after %ds; %s and later jobs wait for the next tick",
                                clock() - started, job.name)
                    break
                self.run(job, now)
                ran.append(job.name)
        return ran

    def run(self, job, now):
        started = time.monotonic()
        try:
            detail, ok, error = job.fn(now), True, None
        except sources.SourceError as ex:
            detail, ok, error = None, False, str(ex)
            LOG.warning("job %s failed: %s", job.name, ex)
        except Exception as ex:
            detail, ok, error = None, False, "内部错误 " + type(ex).__name__
            LOG.exception("job %s crashed", job.name)
        store.job_record(self.con, job.name, now, ok, (time.monotonic() - started) * 1000, error, detail)
        interval = job.interval(now)
        self.next_run[job.name] = now + (interval if ok else min(interval, 300))
        return ok

    def _sweep_interval(self, now):
        if store.meta_get(self.con, "etf_sweep_cursor") is not None:
            return 900
        done = int(store.meta_get(self.con, "etf_sweep_done", 0) or 0)
        return max(900, done + 7 * DAY - now)

    def _live_interval(self, now):
        return 900 if equities.extended_hours(self.cache.get("market_info"), now) else 2 * HOUR

    def _backfill_interval(self, now):
        return 900 if int(store.meta_get(self.con, "backfill_pending", 1) or 0) else 6 * HOUR

    def quality(self, name, result):
        checks = store.meta_json(self.con, "quality_checks", {}) or {}
        checks[name] = result
        store.meta_set(self.con, "quality_checks", json.dumps(checks, ensure_ascii=False))

    def _overview(self):
        overview = self.cache.get("overview")
        if not overview:
            raise sources.SourceError("等待现货数据")
        return overview

    # ----- crypto -----

    def spot(self, now):
        overview = analysis.crypto_overview(list(sources.coinbase_products("SPOT", fetch=self.fetch)))
        if overview["flow"]["total"] <= 0 or not overview["top"]:
            raise sources.SourceError("Coinbase spot data has no USD volume")
        self.cache.update(overview=overview, spot_at=now)
        return f"{overview['count']} assets"

    def futures(self, now):
        contracts = derivs.parse_contracts(sources.coinbase_futures(fetch=self.fetch))
        if not contracts:
            raise sources.SourceError("Coinbase futures list is empty")
        store.insert_deriv_ticks(self.con, now, contracts)
        self.cache.update(contracts=contracts, futures_at=now)
        return f"{len(contracts)} contracts"

    def forecast_targets(self):
        targets = [("BTC", "BTC-USD", "BTC"), ("ETH", "ETH-USD", "ETH")]
        us5 = derivs.find_perp(self.cache.get("contracts") or [], "US5")
        if us5:
            targets.append(("美股500", us5["product_id"], "US5"))
        return targets

    def candles(self, now):
        overview = self._overview()
        end = now - now % HOUR
        wanted = {a["product_id"]: CANDLE_DAYS for a in overview["risk"][:TOP_CANDLES] if a.get("product_id")}
        for _, pid, _ in self.forecast_targets():
            wanted[pid] = TARGET_DAYS
        fetched = 0
        for pid, days in wanted.items():
            first = self.con.execute("SELECT MIN(start) FROM candles WHERE product_id=? AND granularity=3600",
                                     (pid,)).fetchone()[0]
            want_from = end - days * DAY
            if (first is None or first > want_from + 2 * HOUR) and \
                    int(store.meta_get(self.con, "candles_backfilled:" + pid, 0) or 0) < days:
                rows = sources.coinbase_candles(pid, "ONE_HOUR", want_from, first or end, fetch=self.fetch)
                store.upsert_candles(self.con, pid, 3600, rows)
                store.meta_set(self.con, "candles_backfilled:" + pid, days)
                fetched += len(rows)
            last = store.last_candle_start(self.con, pid, 3600)
            if last is not None and last + HOUR < end:
                rows = sources.coinbase_candles(pid, "ONE_HOUR", last + HOUR, end, fetch=self.fetch)
                store.upsert_candles(self.con, pid, 3600, rows)
                fetched += len(rows)
        return f"{len(wanted)} products, {fetched} new candles"

    def trades(self, now):
        overview = self._overview()
        agree = samples = 0
        coverage = []
        for asset in overview["risk"][:TOP_TRADES]:
            pid, symbol = asset["product_id"], asset["symbol"]
            cursor = float(store.meta_get(self.con, "trades_cursor:" + pid, 0) or 0)
            start, end = (max(cursor, now - HOUR) if cursor else now - 900), int(now)
            if end - start < 30:
                continue
            trades, covered_from = sources.coinbase_trades(pid, start, end, TRADE_PAGES.get(symbol, DEFAULT_TRADE_PAGES),
                                                           fetch=self.fetch)
            store.add_flow(self.con, symbol, crypto.bucketize(trades, covered_from, end))
            store.meta_set(self.con, "trades_cursor:" + pid, end)
            ratio, n = crypto.maker_side_consistency(trades)
            if n:
                agree, samples = agree + ratio * n, samples + n
            coverage.append((end - covered_from) / (end - start))
        if samples >= 50:
            self.quality("逐笔方向", {"ok": agree / samples > 0.5, "ratio": round(agree / samples, 3),
                                   "samples": samples, "at": int(now)})
        return f"coverage {min(coverage) * 100:.0f}%" if coverage else "no window"

    def books(self, now):
        books = {}
        for asset in self._overview()["risk"][:TOP_BOOKS]:
            books[asset["symbol"]] = crypto.book_depth(sources.coinbase_book(asset["product_id"], 500, fetch=self.fetch))
        self.cache.update(books=books, books_at=now)
        return f"{len(books)} books"

    # ----- equities -----

    def market_info(self, now):
        self.cache["market_info"] = sources.nasdaq_market_info(fetch=self.fetch)
        return self.cache["market_info"].get("mrktStatus") or "ok"

    def cb_equity_list(self, now):
        seen = store.merge_tickers(self.con, sources.coinbase_products("EQUITY", fetch=self.fetch), now)
        return f"{seen} products seen, {len(store.tradable_tickers(self.con, now))} tradable"

    def equity_meta(self, now):
        subtypes = store.tradable_subtypes(self.con, now)
        if len(subtypes) < MIN_TRADABLE:
            raise sources.SourceError("Coinbase 股票清单不足（%d），等待刷新" % len(subtypes))
        rows = []
        for q in sources.nasdaq_quotes(fetch=self.fetch):
            ticker = (q.get("symbol") or "").strip().upper().replace("/", ".")
            if equities.kind_of(subtypes.get(ticker)) != "stock":
                continue
            price, volume = analysis.money(q.get("lastsale")), analysis.money(q.get("volume"))
            rows.append({"ticker": ticker, "name": q.get("name"), "kind": "stock", "subtype": subtypes[ticker],
                         "sector": q.get("sector") or None, "industry": q.get("industry") or None, "category": None,
                         "market_cap": analysis.money(q.get("marketCap")),
                         "ref_turnover": price * volume if price and volume else None})
        named = set()
        for e in sources.nasdaq_etfs(fetch=self.fetch):
            ticker = (e.get("symbol") or "").strip().upper()
            if equities.kind_of(subtypes.get(ticker)) != "etf":
                continue
            named.add(ticker)
            rows.append(self._etf_row(ticker, e.get("companyName"), subtypes[ticker]))
        rows += [self._etf_row(t, None, subtypes[t]) for t in equities.CORE_ETF_LIST
                 if t in subtypes and t not in named and equities.kind_of(subtypes[t]) == "etf"]
        store.upsert_equity_meta(self.con, rows, now)
        return f"{sum(r['kind'] == 'stock' for r in rows)} stocks, {sum(r['kind'] == 'etf' for r in rows)} ETFs"

    @staticmethod
    def _etf_row(ticker, name, subtype):
        return {"ticker": ticker, "name": name, "kind": "etf", "subtype": subtype, "sector": None, "industry": None,
                "category": equities.etf_category(ticker, name), "market_cap": None, "ref_turnover": None}

    def etf_sweep(self, now):
        """Rank every Coinbase ETF by turnover, 800 symbols per run (weekly, DESIGN C5)."""
        cursor = store.meta_get(self.con, "etf_sweep_cursor")
        if cursor is None:
            if now - int(store.meta_get(self.con, "etf_sweep_done", 0) or 0) < 7 * DAY:
                return "not due"
            cursor = 0
        etfs = sorted(t for t, m in store.equity_meta(self.con).items() if m["kind"] == "etf")
        if not etfs:
            raise sources.SourceError("ETF 资料为空，等待行业资料作业")
        cursor = int(cursor)
        batch = etfs[cursor:cursor + ETF_SWEEP_SYMBOLS]
        rows = sources.nasdaq_watchlist([(t, "etf") for t in batch], fetch=self.fetch, sleep=self.sleep)
        values = {}
        for ticker, row in rows.items():
            tick = equities.parse_tick(row)
            view = equities.session_view(tick, "validated") if tick else None
            if view and view["turnover"] > 0:
                values[ticker] = view["turnover"]
        store.set_ref_turnover(self.con, values, now)
        cursor += len(batch)
        if cursor >= len(etfs):
            store.meta_del(self.con, "etf_sweep_cursor")
            store.meta_set(self.con, "etf_sweep_done", int(now))
            self.next_run["universe"] = 0          # re-rank the universe with the new ETF turnover
        else:
            store.meta_set(self.con, "etf_sweep_cursor", cursor)
        return f"{min(cursor, len(etfs))}/{len(etfs)} ETFs ranked"

    def universe(self, now):
        rows = equities.select_universe(store.equity_meta(self.con), store.tradable_tickers(self.con, now))
        if len(rows) < 100:
            raise sources.SourceError("股票池不足（%d）" % len(rows))
        store.set_universe(self.con, rows, now)
        return f"{sum(r['kind'] == 'stock' for r in rows)} stocks + {sum(r['kind'] == 'etf' for r in rows)} ETFs"

    def equity_live(self, now):
        members = store.universe(self.con)
        if not members:
            raise sources.SourceError("股票池为空")
        rows = sources.nasdaq_watchlist([(u["ticker"], "etf" if u["kind"] == "etf" else "stocks") for u in members],
                                        fetch=self.fetch, sleep=self.sleep)
        ticks = [t for t in (equities.parse_tick(r) for r in rows.values()) if t]
        if len(ticks) < len(members) * 0.5:
            raise sources.SourceError("只拿到 %d/%d 只行情" % (len(ticks), len(members)))
        store.insert_equity_ticks(self.con, now, ticks)
        by_ticker = {t["ticker"]: t for t in ticks}
        self.cache.update(equity_ticks=by_ticker, equity_at=now)
        semantics = store.meta_get(self.con, "after_semantics", "unverified")
        if semantics == "unverified" and any(t["status"] == "after" for t in by_ticker.values()):
            last_open = store.last_open_ticks(self.con, now - 10 * HOUR, now)
            after = equities.after_hours_sample(by_ticker, last_open,
                                                equities.session_close(self.cache.get("market_info")))
            result = equities.after_hours_check(after, last_open) if after else None
            if result:
                semantics = "validated" if result["ok"] else "failed"
                store.meta_set(self.con, "after_semantics", semantics)
                self.quality("盘后字段", dict(result, at=int(now)))
        session_date = equities.completed_session_date(self.cache.get("market_info"), now)
        if session_date:
            bars = [b for b in (equities.daily_bar(k, t, session_date, semantics) for k, t in by_ticker.items()) if b]
            if bars:
                store.upsert_daily(self.con, bars)
        return f"{len(ticks)}/{len(members)} quotes"

    def equity_backfill(self, now):
        """Seed 20-session volume baselines from Nasdaq daily history, 20 tickers per run (DESIGN C6)."""
        counts = store.daily_counts(self.con)
        attempted = store.meta_json(self.con, "backfill_attempted", {}) or {}
        pending = [u for u in store.universe(self.con) if counts.get(u["ticker"], 0) < BACKFILL_MIN_BARS
                   and now - attempted.get(u["ticker"], 0) > 7 * DAY]
        today = dt.datetime.fromtimestamp(now, analysis.ET).date()
        failures = 0
        for u in pending[:BACKFILL_PER_RUN]:
            try:
                rows = sources.nasdaq_history(u["ticker"], "etf" if u["kind"] == "etf" else "stocks",
                                              (today - dt.timedelta(days=45)).isoformat(), today.isoformat(),
                                              limit=40, fetch=self.fetch)
            except sources.SourceError:
                failures += 1
                if failures > 5:
                    raise
                continue
            store.upsert_daily(self.con, equities.history_bars(u["ticker"], rows))
            attempted[u["ticker"]] = int(now)
            self.sleep(0.3)
        store.meta_set(self.con, "backfill_attempted", json.dumps(attempted))
        left = max(0, len(pending) - BACKFILL_PER_RUN)
        store.meta_set(self.con, "backfill_pending", left)
        return f"{min(len(pending), BACKFILL_PER_RUN)} filled, {left} pending"

    # ----- outlook -----

    def live_factors(self, symbol, now):
        live = {}
        if symbol in ("BTC", "ETH"):
            flow = store.flow_window(self.con, now - 4 * HOUR, now + 1).get(symbol)
            summary = crypto.taker_summary({symbol: flow}, 4 * HOUR) if flow else None
            if summary and summary["coverage"] and summary["coverage"] >= 0.3:
                live["taker_share"] = summary["buy_share"]
            book = (self.cache.get("books") or {}).get(symbol)
            if book:
                live["book_ratio"] = book.get("ratio")
            overview = self.cache.get("overview")
            if overview:
                risk = overview["risk"][:30]
                live["breadth"] = sum(1 for a in risk if (a["change"] or 0) > 0) / len(risk) * 100 if risk else None
                asset = next((a for a in risk if a["symbol"] == symbol), None)
                ret_24h = asset["change"] if asset else None
            else:
                ret_24h = None
            contracts = self.cache.get("contracts") or []
            perps = derivs.crypto_perps(contracts, past_24h=store.deriv_near(self.con, now - 24 * HOUR))
            perp = next((p for p in perps if p["asset"] == symbol), None)
            if perp:
                live.update(funding_ann=perp["funding_ann"], oi_chg24=perp["oi_chg24"], ret_24h=ret_24h)
            us5 = next((m for m in derivs.macro_board(contracts) if m["code"] == "US5"), None)
            if us5:
                live["macro_ret"] = us5["chg24"]
        # Index perps (US500): funding reflects carry/basis rather than crowding, so price factors only.
        return forecast.live_factors(**live)

    def forecast(self, now):
        hour = now - now % HOUR
        outlooks = {}
        for label, pid, symbol in self.forecast_targets():
            closes = [r["close"] for r in store.candles(self.con, pid, 3600, since=now - 70 * DAY)]
            o = forecast.outlook(closes, self.live_factors(symbol, now)) if len(closes) >= 200 else None
            outlooks[label] = o
            if o:
                store.add_forecast(self.con, {"created_at": hour, "target": label, "horizon_h": forecast.HORIZON_H,
                                              "price": o["price"], "score": o["score"], "direction": o["direction"],
                                              "sigma": o["sigma"], "low": o["low"], "high": o["high"],
                                              "factors": o["factors"]})
        self.cache.update(outlooks=outlooks, outlooks_at=now)
        evaluated = 0
        pids = {label: pid for label, pid, _ in self.forecast_targets()}
        for f in store.due_forecasts(self.con, now):
            target_hour = f["created_at"] + f["horizon_h"] * HOUR
            bars = [b for b in store.candles(self.con, pids.get(f["target"], ""), 3600, since=f["created_at"])
                    if b["start"] <= target_hour - HOUR]
            if bars:
                realized, hit, in_range = forecast.evaluate(f, bars[-1]["close"])
                store.finish_forecast(self.con, f["id"], now, realized, hit, in_range)
                evaluated += 1
            elif now - target_hour > DAY:
                store.finish_forecast(self.con, f["id"], now, None, None, None)
        return f"{sum(1 for o in outlooks.values() if o)} outlooks, {evaluated} evaluated"

    def backtest(self, now):
        results = {}
        for label, pid, _ in self.forecast_targets():
            closes = [r["close"] for r in store.candles(self.con, pid, 3600, since=now - 70 * DAY)]
            if len(closes) >= 400:
                results[label] = forecast.backtest(closes)
                store.meta_set(self.con, "backtest:" + label, json.dumps(results[label]))
        return ", ".join(f"{k} {v['hit_rate'] or 0:.0f}%/{v['signals']}" for k, v in results.items()) or "not enough history"

    def prune(self, now):
        return f"{store.prune(self.con, now)} rows removed"
