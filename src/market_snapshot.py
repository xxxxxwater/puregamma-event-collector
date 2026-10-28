"""Assemble one snapshot from the collector cache and the SQLite history.

Four independent parts (crypto, deriv, equity, forecast): a failure in one never
blocks the others. Besides Telegram sections it yields per-symbol cards for /q and
the anomaly list used by the push policy.
"""
import logging

import market_analysis as analysis
import market_digest as digest
import market_crypto as crypto
import market_derivs as derivs
import market_equities as equities
import market_render as render
import market_sources as sources
import market_store as store

LOG = logging.getLogger("market.snapshot")
HOUR = 3600
FRESH = {"overview": 3600, "contracts": 3600, "equity": 3 * 3600}
PART_SECTIONS = {
    "crypto": ("crypto_flow", "crypto_top", "crypto_alerts"),
    "deriv": ("crypto_deriv", "macro"),
    "equity": ("equity_overview", "equity_top", "equity_movers", "sectors", "etf", "equity_alerts"),
    "forecast": ("forecast",),
}
PART_TITLES = {
    "crypto_flow": render.CRYPTO_FLOW_TITLE, "crypto_top": render.CRYPTO_TOP_TITLE,
    "crypto_alerts": render.CRYPTO_ALERTS_TITLE, "crypto_deriv": render.CRYPTO_DERIV_TITLE,
    "macro": render.MACRO_TITLE, "equity_overview": render.EQUITY_OVERVIEW_TITLE,
    "equity_top": render.EQUITY_TOP_TITLE, "equity_movers": render.EQUITY_MOVERS_TITLE,
    "sectors": render.SECTORS_TITLE, "etf": render.ETF_TITLE, "equity_alerts": render.EQUITY_ALERTS_TITLE,
    "forecast": render.FORECAST_TITLE,
}


class SnapshotBuilder:
    def __init__(self, con, collector):
        self.con, self.collector = con, collector

    def _cached(self, key, stamp, now, limit):
        value = self.collector.cache.get(key)
        if value is None:
            raise sources.SourceError("等待首次采集（%s）" % key)
        if now - self.collector.cache.get(stamp, 0) > limit:
            raise sources.SourceError("%s 数据已超过 %d 分钟未更新" % (key, limit // 60))
        return value

    def build(self, now):
        ctx = {"now": now, "sections": {}, "metrics": {}, "status": {}, "quotes": [], "anomalies": {}}
        for key, part in (("crypto", self._crypto), ("deriv", self._deriv), ("equity", self._equity),
                          ("forecast", self._forecast)):
            try:
                part(ctx)
                ctx["status"][key] = "ok"
            except sources.SourceError as ex:
                LOG.warning("%s part unavailable: %s", key, ex)
                ctx["status"][key] = str(ex)
            except Exception as ex:
                LOG.exception("%s part crashed", key)
                ctx["status"][key] = "内部错误 " + type(ex).__name__
        if ctx["status"]["crypto"] == "ok":
            alerts = ctx["anomalies"].get("crypto", []) + ctx["anomalies"].get("deriv", [])
            ctx["sections"]["crypto_alerts"] = render.alerts_section(render.CRYPTO_ALERTS_TITLE,
                                                                     sorted(alerts, key=lambda a: -a["severity"]))
        self._cards(ctx)
        ctx["sections"]["note"] = render.NOTE
        return ctx

    # ----- parts -----

    def _crypto(self, ctx):
        now = ctx["now"]
        overview = self._cached("overview", "spot_at", now, FRESH["overview"])
        end = now - now % HOUR
        recent, metrics = {}, {}
        for asset in overview["risk"][:30]:
            rows = store.candles(self.con, asset["product_id"], 3600, since=end - 8 * 86400)
            if rows:
                recent[asset["symbol"]] = [r for r in rows if r["start"] >= end - 8 * HOUR]
                metrics[asset["symbol"]] = crypto.candle_metrics([r for r in rows if r["start"] < end])
        window = analysis.crypto_window_flow({k: v for k, v in recent.items() if v}, end, 4) \
            if sum(1 for v in recent.values() if v) >= 10 else None
        taker = crypto.taker_summary(store.flow_window(self.con, now - 4 * HOUR, now + 1), 4 * HOUR)
        books = self.collector.cache.get("books") if now - self.collector.cache.get("books_at", 0) <= HOUR else {}
        ctx["sections"].update(
            crypto_flow=render.crypto_flow(overview, window, None if window else "K 线不足", taker, books),
            crypto_top=render.crypto_top(overview))
        ctx["anomalies"]["crypto"] = crypto.crypto_anomalies(metrics, now)
        ctx.update(overview=overview, candle_metrics=metrics, taker=taker, window=window, books=books or {})
        f = overview["flow"]
        ctx["metrics"]["crypto"] = {"total_24h": f["total"], "flow_24h": f["flow"], "assets": overview["count"],
                                    "taker_net_4h": taker["net"], "taker_coverage": taker["coverage"],
                                    "window_net_4h": window["net"] if window else None,
                                    "digest": digest.crypto(overview, window, taker, books or {},
                                                            ctx["anomalies"]["crypto"], now)}

    def _deriv(self, ctx):
        now = ctx["now"]
        contracts = self._cached("contracts", "futures_at", now, FRESH["contracts"])
        past4, past24 = store.deriv_near(self.con, now - 4 * HOUR), store.deriv_near(self.con, now - 24 * HOUR)
        perps = derivs.crypto_perps(contracts, past4, past24)
        spot = {a["symbol"]: a["price"] for a in (ctx.get("overview") or {}).get("risk", [])[:50]}
        term = derivs.term_structure(contracts, spot, now)
        total = sum(p["oi_usd"] for p in perps)
        then = sum(r["oi_usd"] or 0 for r in past24.values() if r["perp"] and r["asset_type"] == "crypto")
        board = derivs.macro_board(contracts)
        ctx["sections"].update(crypto_deriv=render.crypto_deriv(perps, term, total, derivs.change_pct(total, then)),
                               macro=render.macro(board))
        ctx["anomalies"]["deriv"] = derivs.deriv_anomalies(perps, board, now)
        ctx.update(perps=perps, macro=board)
        ctx["metrics"]["deriv"] = {"perp_oi_usd": total, "perps": {p["asset"]: {k: p[k] for k in (
            "funding_ann", "oi_usd", "oi_chg24", "premium")} for p in perps[:6]},
            "macro": {m["code"]: m["chg24"] for m in board},
            "digest": digest.deriv(perps, term, total, derivs.change_pct(total, then), board,
                                   ctx["anomalies"]["deriv"], now)}

    def _equity(self, ctx):
        ticks = self._cached("equity_ticks", "equity_at", ctx["now"], FRESH["equity"])
        # Label, pace and compare by when the quotes were fetched, not by the snapshot time.
        data_at = self.collector.cache["equity_at"]
        info = self.collector.cache.get("market_info")
        semantics = store.meta_get(self.con, "after_semantics", "unverified")
        session = equities.session_info(info, ticks, data_at, semantics)
        last_open = store.last_open_ticks(self.con, data_at - 10 * HOUR, data_at) \
            if session["phase"] in ("after", "closed") and semantics == "failed" else {}
        baselines = store.volume_baselines(self.con, session["date"]) if session["date"] else {}
        rows = equities.board(ticks, store.equity_meta(self.con), baselines, data_at, session["open_at"], semantics,
                              last_open, session.get("close_at"))
        if len(rows) < 100:
            raise sources.SourceError("只有 %d 只股票/ETF 可用" % len(rows))
        overview = equities.equity_overview(rows)
        past = self._past_prices(data_at) if session["phase"] == "open" else {}
        anomalies = equities.equity_anomalies(rows, data_at, past, session["date"])
        ctx["sections"].update(
            equity_overview=render.equity_overview(overview, session), equity_top=render.equity_top(overview),
            equity_movers=render.equity_movers(overview), sectors=render.sectors(overview), etf=render.etf(overview),
            equity_alerts=render.alerts_section(render.EQUITY_ALERTS_TITLE, anomalies))
        ctx["anomalies"]["equity"] = anomalies
        ctx.update(equity_rows=rows, session=session)
        ctx["metrics"]["equity"] = {"session": session, "stocks": overview["stocks"], "etfs": overview["etfs"],
                                    "baselines": len(baselines),
                                    "sectors": {g["name"]: g["flow"] for g in overview["sectors"]},
                                    "categories": {g["name"]: g["flow"] for g in overview["categories"]},
                                    "digest": digest.equity(overview, session, anomalies, data_at)}

    def _past_prices(self, now):
        """{15: {ticker: price}, 60: {...}} from earlier live ticks for velocity anomalies."""
        times = store.tick_times(self.con, now - 80 * 60)
        past = {}
        for minutes, (low, high) in ((15, (10, 25)), (60, (45, 75))):
            options = [t for t in times if now - high * 60 <= t <= now - low * 60]
            if options:
                ticks = store.ticks_at(self.con, max(options))
                past[minutes] = {k: t["last"] for k, t in ticks.items() if t["status"] == "open"}
        return past

    def _forecast(self, ctx):
        outlooks = self.collector.cache.get("outlooks")
        if not outlooks or not any(outlooks.values()):
            raise sources.SourceError("研判尚未生成（等待 K 线回填）")
        labels = [label for label, _, _ in self.collector.forecast_targets()]
        records = {label: store.forecast_record(self.con, label, ctx["now"] - 30 * 86400) for label in labels}
        backtests = {label: store.meta_json(self.con, "backtest:" + label, {}) for label in labels}
        ctx["sections"]["forecast"] = render.forecast([(label, outlooks.get(label)) for label in labels],
                                                      records, backtests)
        ctx["metrics"]["forecast"] = {label: {k: o[k] for k in ("score", "direction", "low", "high")}
                                      for label, o in outlooks.items() if o}
        ctx["metrics"]["forecast"]["digest"] = digest.forecast(labels, outlooks, records, backtests,
                                                               self.collector.cache.get("outlooks_at") or ctx["now"])

    def _cards(self, ctx):
        for row in ctx.get("equity_rows", []):
            ctx["quotes"].append({"symbol": row["symbol"], "kind": row["kind"],
                                  "text": render.equity_card(row, ctx["session"])})
        overview = ctx.get("overview")
        if overview:
            perps = {p["asset"]: p for p in ctx.get("perps", [])}
            taker = {r["symbol"]: r for r in (ctx.get("taker") or {}).get("rows", [])}
            for asset in overview["risk"][:30]:
                sym = asset["symbol"]
                ctx["quotes"].append({"symbol": sym, "kind": "crypto", "text": render.crypto_card(
                    asset, (ctx.get("candle_metrics") or {}).get(sym), perps.get(sym), taker.get(sym))})

    @staticmethod
    def anomalies(ctx):
        return [a for part in ("crypto", "deriv", "equity") for a in ctx["anomalies"].get(part, [])]
