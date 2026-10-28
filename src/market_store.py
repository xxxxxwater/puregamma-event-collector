"""SQLite contract between market-report (writer) and the collector's commands (reader).

Standard library only: the collector imports this module to answer the market
commands, /q lookups and the /logs status line without any network access.
Snapshots hold pre-rendered Telegram HTML sections plus structured metrics;
time-series tables hold the raw history the analytics need (market/DESIGN.md §4).
"""
import contextlib
import datetime as dt
import json
import sqlite3
import urllib.parse

SCHEMA = 2
DEFAULT_PATH = "/data/market_report.sqlite"
CST = dt.timezone(dt.timedelta(hours=8))
UTC = dt.timezone.utc
BRIEF_GROUPS = (
    ("加密", ("crypto_flow", "crypto_deriv", "crypto_top", "crypto_alerts")),
    ("美股", ("equity_overview", "equity_top", "equity_movers", "sectors", "etf", "equity_alerts")),
    ("宏观与研判", ("macro", "forecast", "note")),
)
SECTIONS = tuple(name for _, names in BRIEF_GROUPS for name in names)
VIEWS = {"market": SECTIONS,
         "crypto": ("crypto_flow", "crypto_deriv", "crypto_top", "crypto_alerts"),
         "deriv": ("crypto_deriv", "macro"),
         "stocks": ("equity_overview", "equity_top", "equity_movers"),
         "sectors": ("equity_overview", "sectors"),
         "etf": ("etf",),
         "alerts": ("crypto_alerts", "equity_alerts"),
         "macro": ("macro",),
         "predict": ("forecast", "note"),
         "flow": ("crypto_flow", "equity_overview", "sectors", "etf")}
MESSAGE_LIMIT = 4000          # Telegram hard limit is 4096 characters
STALE_AFTER = 3600            # commands warn when the snapshot is older than this
HEARTBEAT_MAX_AGE = 600
KEEP_SNAPSHOT_DAYS = 7
OUTCOME_ZH = {"sent": "已发送", "failed": "已发失败通知", "partial": "部分发送", "expired": "超时未发送"}
JOB_ZH = {"spot": "现货", "candles": "K线", "trades": "逐笔", "books": "盘口", "futures": "期货",
          "market_info": "美股时段", "cb_equity_list": "股票清单", "equity_meta": "行业资料", "etf_sweep": "ETF扫描",
          "universe": "股票池", "equity_live": "美股行情", "equity_backfill": "日线回填", "forecast": "研判",
          "backtest": "回测", "prune": "清理"}
RETENTION_DAYS = {"equity_ticks": 5, "crypto_flow": 8, "deriv_ticks": 8, "alerts": 30,
                  "forecasts": 150, "equity_daily": 150, "email_deliveries": 90}
HOURLY_CANDLE_DAYS = 70

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS cb_equities (ticker TEXT PRIMARY KEY, last_seen INTEGER);
CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, taken_at INTEGER NOT NULL,
    schema INTEGER NOT NULL, sections TEXT NOT NULL, metrics TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS broadcasts (slot INTEGER PRIMARY KEY, created_at INTEGER, snapshot_id INTEGER,
    kind TEXT NOT NULL DEFAULT 'brief', messages TEXT NOT NULL, parts_sent INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL, finished_at INTEGER);
CREATE TABLE IF NOT EXISTS jobs (name TEXT PRIMARY KEY, last_run INTEGER, last_ok INTEGER, last_error TEXT,
    duration_ms INTEGER, runs INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, detail TEXT);
CREATE TABLE IF NOT EXISTS candles (product_id TEXT, granularity INTEGER, start INTEGER, open REAL, high REAL,
    low REAL, close REAL, volume REAL, PRIMARY KEY (product_id, granularity, start));
CREATE TABLE IF NOT EXISTS crypto_flow (symbol TEXT, bucket INTEGER, buy_usd REAL, sell_usd REAL, trades INTEGER,
    covered_s REAL, PRIMARY KEY (symbol, bucket));
CREATE TABLE IF NOT EXISTS deriv_ticks (ts INTEGER, product_id TEXT, root TEXT, asset_type TEXT, perp INTEGER,
    price REAL, index_price REAL, funding REAL, oi_contracts REAL, oi_usd REAL, chg24 REAL, volume24 REAL,
    expiry INTEGER, PRIMARY KEY (product_id, ts));
CREATE TABLE IF NOT EXISTS equity_meta (ticker TEXT PRIMARY KEY, name TEXT, kind TEXT, subtype TEXT, sector TEXT,
    industry TEXT, category TEXT, market_cap REAL, ref_turnover REAL, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS equity_universe (ticker TEXT PRIMARY KEY, kind TEXT, rank INTEGER, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS equity_ticks (ts INTEGER, ticker TEXT, status TEXT, last REAL, chg_abs REAL,
    chg_pct REAL, volume REAL, prev_close REAL, open REAL, trade_ts INTEGER, PRIMARY KEY (ticker, ts));
CREATE INDEX IF NOT EXISTS idx_equity_ticks_ts ON equity_ticks(ts);
CREATE TABLE IF NOT EXISTS equity_daily (ticker TEXT, date TEXT, close REAL, change REAL, volume REAL,
    turnover REAL, source TEXT, PRIMARY KEY (ticker, date));
CREATE TABLE IF NOT EXISTS forecasts (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER, target TEXT,
    horizon_h INTEGER, price REAL, score REAL, direction INTEGER, sigma REAL, low REAL, high REAL, factors TEXT,
    evaluated_at INTEGER, realized REAL, hit INTEGER, in_range INTEGER, UNIQUE (target, horizon_h, created_at));
CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, key TEXT UNIQUE, kind TEXT,
    symbol TEXT, severity REAL, major INTEGER, text TEXT, pushed INTEGER NOT NULL DEFAULT 0,
    silent INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS quotes (symbol TEXT, kind TEXT, updated_at INTEGER, text TEXT, PRIMARY KEY (symbol, kind));
CREATE TABLE IF NOT EXISTS email_deliveries (key TEXT PRIMARY KEY, recipient_hash TEXT NOT NULL, local_date TEXT NOT NULL,
    locale TEXT, due_at INTEGER, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
    next_attempt_at INTEGER, sent_at INTEGER, snapshot_id INTEGER);
"""


# ---------- time helpers ----------

def bj(ts, fmt="%m-%d %H:%M"):
    return dt.datetime.fromtimestamp(int(ts), CST).strftime(fmt)


def age_label(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分钟前"


def slot_start(now, hours):
    return int(now) - int(now) % (hours * 3600)


def schedule_label(hours):
    slots = sorted({(k * hours + 8) % 24 for k in range(24 // hours)})
    return "北京 " + "/".join(f"{h:02d}" for h in slots) + " 点"


def bj_day(ts):
    return bj(ts, "%Y-%m-%d")


# ---------- connections ----------

def _columns(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def connect(path=DEFAULT_PATH):
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=15000")
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA_SQL)
    for column in ("subtype", "exchange"):
        if column not in _columns(con, "cb_equities"):
            con.execute(f"ALTER TABLE cb_equities ADD COLUMN {column} TEXT")
    # v1 kept sent slots in `runs`; carry them over so a restart never re-sends a slot.
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
        con.execute("""INSERT OR IGNORE INTO broadcasts(slot,created_at,snapshot_id,kind,messages,parts_sent,outcome,finished_at)
                       SELECT slot,sent_at,NULL,'brief','[]',0,'sent',sent_at FROM runs""")
        con.execute("DROP TABLE runs")
    con.commit()
    return con


def open_readonly(path=DEFAULT_PATH):
    con = sqlite3.connect("file:" + urllib.parse.quote(path, safe="/") + "?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def meta_get(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(con, key, value):
    con.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    con.commit()


def meta_del(con, key):
    con.execute("DELETE FROM meta WHERE key=?", (key,))
    con.commit()


def meta_json(con, key, default=None):
    raw = meta_get(con, key)
    try:
        return json.loads(raw) if raw else default
    except ValueError:
        return default


def prune(con, now):
    """Apply retention to every time-series table (DESIGN §7)."""
    removed = 0
    for table, days in RETENTION_DAYS.items():
        column = {"crypto_flow": "bucket", "forecasts": "created_at", "equity_daily": "date",
                  "email_deliveries": "due_at"}.get(table, "ts")
        cutoff = int(now) - days * 86400
        value = bj_day(cutoff) if column == "date" else cutoff
        removed += con.execute(f"DELETE FROM {table} WHERE {column}<?", (value,)).rowcount
    removed += con.execute("DELETE FROM candles WHERE granularity=3600 AND start<?",
                           (int(now) - HOURLY_CANDLE_DAYS * 86400,)).rowcount
    con.commit()
    return removed


# ---------- jobs (source health) ----------

def job_record(con, name, now, ok, duration_ms, error=None, detail=None):
    con.execute("""INSERT INTO jobs(name,last_run,last_ok,last_error,duration_ms,runs,failures,detail)
                   VALUES(?,?,?,?,?,1,?,?) ON CONFLICT(name) DO UPDATE SET last_run=excluded.last_run,
                   last_ok=COALESCE(excluded.last_ok, jobs.last_ok), last_error=excluded.last_error,
                   duration_ms=excluded.duration_ms, runs=jobs.runs+1, failures=jobs.failures+excluded.failures,
                   detail=COALESCE(excluded.detail, jobs.detail)""",
                (name, int(now), int(now) if ok else None, None if ok else (error or "error")[:300],
                 int(duration_ms), 0 if ok else 1, detail))
    con.commit()


def jobs_health(con):
    return [dict(r) for r in con.execute("SELECT * FROM jobs ORDER BY name")]


# ---------- snapshots ----------

def save_snapshot(con, taken_at, sections, metrics, status):
    cur = con.execute("INSERT INTO snapshots(taken_at,schema,sections,metrics,status) VALUES(?,?,?,?,?)",
                      (int(taken_at), SCHEMA, json.dumps(sections, ensure_ascii=False),
                       json.dumps(metrics, ensure_ascii=False), json.dumps(status, ensure_ascii=False)))
    con.execute("DELETE FROM snapshots WHERE taken_at<? AND id NOT IN (SELECT snapshot_id FROM broadcasts "
                "WHERE snapshot_id IS NOT NULL AND outcome='sending')",
                (int(taken_at) - KEEP_SNAPSHOT_DAYS * 86400,))
    con.commit()
    return cur.lastrowid


def _snapshot(row):
    if row is None or row["schema"] != SCHEMA:
        return None
    return {"id": row["id"], "taken_at": row["taken_at"], "sections": json.loads(row["sections"]),
            "metrics": json.loads(row["metrics"]), "status": json.loads(row["status"])}


def latest_snapshot(con):
    return _snapshot(con.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone())


def last_good(con, key, since):
    """Most recent snapshot (taken_at >= since) whose `key` source succeeded, else None."""
    for row in con.execute("SELECT * FROM snapshots WHERE taken_at>=? ORDER BY id DESC", (int(since),)):
        snapshot = _snapshot(row)
        if snapshot and snapshot["status"].get(key) == "ok":
            return snapshot
    return None


def has_data(status):
    return any(v == "ok" or str(v).startswith("reused") for v in status.values())


# ---------- Coinbase tradable equity cache (DESIGN C3 of v1: unstable pagination) ----------

def merge_tickers(con, products, now):
    """Merge one (possibly partial) pass over Coinbase equities; returns tickers seen.

    Pages arrive from a generator that may raise midway: whatever arrived is kept.
    """
    seen = 0
    try:
        for p in products:
            d = p.get("equity_product_details") or {}
            ticker = (d.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            seen += 1
            if d.get("trading_halted") or d.get("liquidate_only"):
                con.execute("DELETE FROM cb_equities WHERE ticker=?", (ticker,))
            else:
                con.execute("INSERT INTO cb_equities(ticker,last_seen,subtype,exchange) VALUES(?,?,?,?) "
                            "ON CONFLICT(ticker) DO UPDATE SET last_seen=excluded.last_seen, "
                            "subtype=COALESCE(excluded.subtype, cb_equities.subtype), "
                            "exchange=COALESCE(excluded.exchange, cb_equities.exchange)",
                            (ticker, int(now), d.get("equity_subtype"), d.get("opol") or None))
    finally:
        con.commit()
    return seen


def tradable_tickers(con, now, keep_days=7):
    return {r[0] for r in con.execute("SELECT ticker FROM cb_equities WHERE last_seen>=?",
                                      (int(now) - keep_days * 86400,))}


def tradable_subtypes(con, now, keep_days=7):
    return {r[0]: r[1] for r in con.execute("SELECT ticker, subtype FROM cb_equities WHERE last_seen>=?",
                                            (int(now) - keep_days * 86400,))}


# ---------- equities ----------

def upsert_equity_meta(con, rows, now):
    con.executemany("""INSERT INTO equity_meta(ticker,name,kind,subtype,sector,industry,category,market_cap,ref_turnover,updated_at)
        VALUES(:ticker,:name,:kind,:subtype,:sector,:industry,:category,:market_cap,:ref_turnover,:now)
        ON CONFLICT(ticker) DO UPDATE SET name=COALESCE(excluded.name, equity_meta.name), kind=excluded.kind,
        subtype=excluded.subtype, sector=COALESCE(excluded.sector, equity_meta.sector),
        industry=COALESCE(excluded.industry, equity_meta.industry),
        category=COALESCE(excluded.category, equity_meta.category),
        market_cap=COALESCE(excluded.market_cap, equity_meta.market_cap),
        ref_turnover=COALESCE(excluded.ref_turnover, equity_meta.ref_turnover), updated_at=excluded.updated_at""",
                    [dict(r, now=int(now)) for r in rows])
    con.commit()


def set_ref_turnover(con, values, now):
    con.executemany("UPDATE equity_meta SET ref_turnover=?, updated_at=? WHERE ticker=?",
                    [(v, int(now), t) for t, v in values.items()])
    con.commit()


def equity_meta(con):
    return {r["ticker"]: dict(r) for r in con.execute("SELECT * FROM equity_meta")}


def set_universe(con, rows, now):
    con.execute("DELETE FROM equity_universe")
    con.executemany("INSERT INTO equity_universe VALUES(?,?,?,?)",
                    [(r["ticker"], r["kind"], i, int(now)) for i, r in enumerate(rows, 1)])
    con.commit()


def universe(con):
    return [dict(r) for r in con.execute("SELECT ticker, kind, rank FROM equity_universe ORDER BY rank")]


def insert_equity_ticks(con, ts, ticks):
    con.executemany("INSERT OR REPLACE INTO equity_ticks VALUES(?,?,?,?,?,?,?,?,?,?)",
                    [(int(ts), t["ticker"], t["status"], t["last"], t["chg_abs"], t["chg_pct"], t["volume"],
                      t["prev_close"], t["open"], t["trade_ts"]) for t in ticks])
    con.commit()


def ticks_at(con, ts):
    return {r["ticker"]: dict(r) for r in con.execute("SELECT * FROM equity_ticks WHERE ts=?", (int(ts),))}


def tick_times(con, since):
    return [r[0] for r in con.execute("SELECT DISTINCT ts FROM equity_ticks WHERE ts>=? ORDER BY ts", (int(since),))]


def last_open_ticks(con, since, until):
    """Per ticker, the latest tick with status 'open' in [since, until]."""
    rows = con.execute("""SELECT t.* FROM equity_ticks t JOIN (SELECT ticker, MAX(ts) AS ts FROM equity_ticks
                          WHERE status='open' AND ts BETWEEN ? AND ? GROUP BY ticker) m
                          ON t.ticker=m.ticker AND t.ts=m.ts""", (int(since), int(until)))
    return {r["ticker"]: dict(r) for r in rows}


def upsert_daily(con, rows):
    con.executemany("""INSERT INTO equity_daily VALUES(:ticker,:date,:close,:change,:volume,:turnover,:source)
                       ON CONFLICT(ticker,date) DO UPDATE SET close=excluded.close, change=excluded.change,
                       volume=excluded.volume, turnover=excluded.turnover, source=excluded.source""", rows)
    con.commit()


def daily_counts(con):
    return {r[0]: r[1] for r in con.execute("SELECT ticker, COUNT(*) FROM equity_daily GROUP BY ticker")}


def volume_baselines(con, before_date, sessions=20, minimum=10):
    """{ticker: average daily volume over the last `sessions` bars strictly before before_date}."""
    rows = con.execute("""SELECT ticker, volume FROM (SELECT ticker, volume, ROW_NUMBER() OVER
                          (PARTITION BY ticker ORDER BY date DESC) AS n FROM equity_daily
                          WHERE date<? AND volume>0) WHERE n<=?""", (before_date, sessions))
    totals = {}
    for ticker, volume in rows:
        count, total = totals.get(ticker, (0, 0.0))
        totals[ticker] = (count + 1, total + volume)
    return {t: total / count for t, (count, total) in totals.items() if count >= minimum}


# ---------- crypto time series ----------

def upsert_candles(con, product_id, granularity, candles):
    con.executemany("INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?,?,?,?)",
                    [(product_id, granularity, int(c["start"]), float(c["open"]), float(c["high"]), float(c["low"]),
                      float(c["close"]), float(c["volume"])) for c in candles])
    con.commit()


def candles(con, product_id, granularity, since=0):
    return [dict(r) for r in con.execute("SELECT start,open,high,low,close,volume FROM candles WHERE product_id=? "
                                         "AND granularity=? AND start>=? ORDER BY start",
                                         (product_id, granularity, int(since)))]


def last_candle_start(con, product_id, granularity):
    row = con.execute("SELECT MAX(start) FROM candles WHERE product_id=? AND granularity=?",
                      (product_id, granularity)).fetchone()
    return row[0] if row and row[0] is not None else None


def add_flow(con, symbol, buckets):
    """Accumulate taker buckets {bucket: {buy, sell, trades, covered}} for a symbol."""
    con.executemany("""INSERT INTO crypto_flow VALUES(?,?,?,?,?,?) ON CONFLICT(symbol,bucket) DO UPDATE SET
                       buy_usd=buy_usd+excluded.buy_usd, sell_usd=sell_usd+excluded.sell_usd,
                       trades=trades+excluded.trades, covered_s=MIN(900, covered_s+excluded.covered_s)""",
                    [(symbol, b, v["buy"], v["sell"], v["trades"], v["covered"]) for b, v in buckets.items()])
    con.commit()


def flow_window(con, since, until):
    rows = con.execute("""SELECT symbol, SUM(buy_usd), SUM(sell_usd), SUM(trades), SUM(covered_s) FROM crypto_flow
                          WHERE bucket>=? AND bucket<? GROUP BY symbol""", (int(since), int(until)))
    return {r[0]: {"buy": r[1] or 0.0, "sell": r[2] or 0.0, "trades": r[3] or 0, "covered": r[4] or 0.0}
            for r in rows}


def insert_deriv_ticks(con, ts, contracts):
    con.executemany("INSERT OR REPLACE INTO deriv_ticks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(int(ts), c["product_id"], c["root"], c["asset_type"], int(c["perp"]), c["price"],
                      c["index_price"], c["funding"], c["oi"], c["oi_usd"], c["chg24"], c["volume24"], c["expiry"])
                     for c in contracts])
    con.commit()


def deriv_near(con, target_ts, tolerance=1800):
    """{product_id: row} from the deriv snapshot closest to target_ts (within tolerance)."""
    row = con.execute("SELECT ts FROM deriv_ticks WHERE ts BETWEEN ? AND ? ORDER BY ABS(ts-?) LIMIT 1",
                      (int(target_ts) - tolerance, int(target_ts) + tolerance, int(target_ts))).fetchone()
    if not row:
        return {}
    return {r["product_id"]: dict(r) for r in con.execute("SELECT * FROM deriv_ticks WHERE ts=?", (row[0],))}


# ---------- forecasts ----------

def add_forecast(con, row):
    cur = con.execute("""INSERT OR IGNORE INTO forecasts(created_at,target,horizon_h,price,score,direction,sigma,low,high,factors)
                         VALUES(:created_at,:target,:horizon_h,:price,:score,:direction,:sigma,:low,:high,:factors)""",
                      dict(row, factors=json.dumps(row.get("factors") or {}, ensure_ascii=False)))
    con.commit()
    return cur.rowcount == 1


def due_forecasts(con, now):
    return [dict(r) for r in con.execute("SELECT * FROM forecasts WHERE evaluated_at IS NULL AND "
                                         "created_at + horizon_h*3600 <= ?", (int(now),))]


def finish_forecast(con, forecast_id, now, realized, hit, in_range):
    con.execute("UPDATE forecasts SET evaluated_at=?, realized=?, hit=?, in_range=? WHERE id=?",
                (int(now), realized, hit, in_range, forecast_id))
    con.commit()


def forecast_record(con, target, since):
    row = con.execute("""SELECT COUNT(*), SUM(hit IS NOT NULL), SUM(hit=1), SUM(in_range=1) FROM forecasts
                         WHERE target=? AND evaluated_at IS NOT NULL AND created_at>=?""",
                      (target, int(since))).fetchone()
    return {"evaluated": row[0] or 0, "directional": row[1] or 0, "hits": row[2] or 0, "in_range": row[3] or 0}


# ---------- alerts ----------

def add_alert(con, alert, now):
    """Insert an anomaly once per key; returns True when it is new."""
    cur = con.execute("INSERT OR IGNORE INTO alerts(ts,key,kind,symbol,severity,major,text) VALUES(?,?,?,?,?,?,?)",
                      (int(now), alert["key"], alert["kind"], alert["symbol"], alert["severity"],
                       1 if alert.get("major") else 0, alert["text"]))
    con.commit()
    return cur.rowcount == 1


def mark_pushed(con, keys, silent):
    con.executemany("UPDATE alerts SET pushed=1, silent=? WHERE key=?", [(1 if silent else 0, k) for k in keys])
    con.commit()


def pushed_since(con, since):
    return con.execute("SELECT COUNT(DISTINCT ts) FROM alerts WHERE pushed=1 AND ts>=?", (int(since),)).fetchone()[0]


# ---------- quotes (single-symbol lookups for /q) ----------

def replace_quotes(con, rows, now):
    con.execute("DELETE FROM quotes WHERE updated_at<?", (int(now) - 7 * 86400,))
    con.executemany("INSERT OR REPLACE INTO quotes VALUES(?,?,?,?)",
                    [(r["symbol"].upper(), r["kind"], int(now), r["text"]) for r in rows])
    con.commit()


# ---------- e-mail deliveries (one per recipient per local day; addresses stored only as hashes) ----------

def delivery_get(con, key):
    row = con.execute("SELECT * FROM email_deliveries WHERE key=?", (key,)).fetchone()
    return dict(row) if row else None


def delivery_record(con, key, recipient_hash, local_date, locale, due_at, status, now, error=None,
                    next_attempt_at=None, snapshot_id=None):
    con.execute("""INSERT INTO email_deliveries(key,recipient_hash,local_date,locale,due_at,status,attempts,error,
                   next_attempt_at,sent_at,snapshot_id) VALUES(?,?,?,?,?,?,1,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET status=excluded.status, attempts=email_deliveries.attempts+1,
                   error=excluded.error, next_attempt_at=excluded.next_attempt_at,
                   sent_at=COALESCE(excluded.sent_at, email_deliveries.sent_at),
                   snapshot_id=COALESCE(excluded.snapshot_id, email_deliveries.snapshot_id)""",
                (key, recipient_hash, local_date, locale, int(due_at), status, error, next_attempt_at,
                 int(now) if status == "sent" else None, snapshot_id))
    con.commit()


def email_stats(con, since):
    rows = con.execute("SELECT status, COUNT(*) FROM email_deliveries WHERE due_at>=? GROUP BY status", (int(since),))
    return {status: count for status, count in rows}


# ---------- broadcasts (one per slot, resumable) ----------

def broadcast_get(con, slot):
    row = con.execute("SELECT * FROM broadcasts WHERE slot=?", (int(slot),)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["messages"] = json.loads(item["messages"] or "[]")
    return item


def broadcast_start(con, slot, now, snapshot_id, messages, kind="brief"):
    con.execute("INSERT INTO broadcasts(slot,created_at,snapshot_id,kind,messages,parts_sent,outcome) "
                "VALUES(?,?,?,?,?,0,'sending')",
                (int(slot), int(now), snapshot_id, kind, json.dumps(messages, ensure_ascii=False)))
    con.commit()


def broadcast_progress(con, slot, parts_sent):
    con.execute("UPDATE broadcasts SET parts_sent=? WHERE slot=?", (parts_sent, int(slot)))
    con.commit()


def broadcast_finish(con, slot, outcome, now):
    con.execute("UPDATE broadcasts SET outcome=?, finished_at=? WHERE slot=?", (outcome, int(now), int(slot)))
    con.commit()


# ---------- message composition (shared by broadcasts and commands) ----------

def compose(header, sections, limit=MESSAGE_LIMIT):
    """Join sections under a header, splitting only at section boundaries."""
    messages, current = [], header
    for section in sections:
        if not section:
            continue
        if len(section) > limit - 100:
            section = "（该版块内容过长，已省略）"
        candidate = current + "\n\n" + section if current else section
        if len(candidate) <= limit:
            current = candidate
        else:
            messages.append(current)
            current = section
    if current:
        messages.append(current)
    return messages


def brief_header(taken_at, hours, part, parts, title):
    return (f"📊 <b>Coinbase 市场简报 {part}/{parts} · {title}</b>\n"
            f"{bj(taken_at, '%Y-%m-%d %H:%M')} 北京时间 · "
            f"{dt.datetime.fromtimestamp(int(taken_at), UTC):%H:%M} UTC · 每 {hours} 小时")


def snapshot_header(taken_at, now):
    header = (f"📊 <b>Coinbase 市场快照</b>\n"
              f"数据时间 {bj(taken_at, '%m-%d %H:%M')} 北京时间（{age_label(now - taken_at)}）")
    if now - taken_at > STALE_AFTER:
        header += "\n⚠️ 快照超过 1 小时未更新，数据可能过期，请用 /logs 查看服务状态"
    return header


def brief_messages(snapshot, hours):
    messages = []
    for part, (title, names) in enumerate(BRIEF_GROUPS, 1):
        sections = [snapshot["sections"].get(k) for k in names]
        if any(sections):
            messages += compose(brief_header(snapshot["taken_at"], hours, part, len(BRIEF_GROUPS), title), sections)
    return messages


def view_messages(path, view, now):
    """Command replies for one view; never raises for a missing or empty database."""
    try:
        with contextlib.closing(open_readonly(path)) as con:
            snapshot = latest_snapshot(con)
    except sqlite3.Error:
        snapshot = None
    if not snapshot:
        return ["📊 行情服务还没有生成数据，请几分钟后再试。"]
    return compose(snapshot_header(snapshot["taken_at"], now),
                   [snapshot["sections"].get(k) for k in VIEWS[view]])


def quote_messages(path, symbol, now):
    """/q replies: pre-rendered per-symbol cards (stock, ETF and/or crypto)."""
    symbol = (symbol or "").strip().upper().lstrip("$")
    try:
        with contextlib.closing(open_readonly(path)) as con:
            rows = con.execute("SELECT kind, updated_at, text FROM quotes WHERE symbol=? ORDER BY kind",
                               (symbol,)).fetchall()
    except sqlite3.Error:
        rows = []
    if not rows:
        return [f"🔎 没有找到 {symbol} 的数据。股票和 ETF 只覆盖成交额靠前的 Coinbase 可交易标的；"
                "加密只覆盖成交额前 30 的币种。"]
    return [r["text"] + f"\n<i>数据时间 {bj(r['updated_at'])} 北京时间（{age_label(now - r['updated_at'])}）</i>"
            for r in rows]


def status_line(path, now):
    """market-report status lines for /logs (service, snapshot, broadcasts, data sources)."""
    try:
        with contextlib.closing(open_readonly(path)) as con:
            beat = int(meta_get(con, "heartbeat", 0) or 0)
            hours = int(meta_get(con, "interval_hours", 4) or 4)
            snap = con.execute("SELECT taken_at FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
            last = con.execute("SELECT slot,outcome,finished_at FROM broadcasts WHERE outcome!='sending' "
                               "ORDER BY slot DESC LIMIT 1").fetchone()
            jobs = jobs_health(con) if con.execute(
                "SELECT 1 FROM sqlite_master WHERE name='jobs'").fetchone() else []
            checks = meta_json(con, "quality_checks", {}) or {}
            mails = email_stats(con, now - 86400) if con.execute(
                "SELECT 1 FROM sqlite_master WHERE name='email_deliveries'").fetchone() else {}
    except (sqlite3.Error, ValueError):
        return "📊 行情简报：未运行（没有数据库）"
    state = "运行中" if beat and now - beat <= HEARTBEAT_MAX_AGE else "⚠️ 心跳超时"
    parts = [f"📊 行情简报：{state}",
             f"快照 {age_label(now - snap['taken_at'])}" if snap else "暂无快照"]
    if last:
        parts.append(f"最近播报 {bj(last['finished_at'] or last['slot'])} {OUTCOME_ZH.get(last['outcome'], last['outcome'])}")
    parts.append(f"下期 {bj(slot_start(now, hours) + hours * 3600, '%H:%M')}")
    lines = [" · ".join(parts)]
    if jobs:
        failing = [j for j in jobs if j["last_error"]]
        line = f"🧩 数据源 {len(jobs) - len(failing)}/{len(jobs)} 正常"
        if failing:
            line += "；异常：" + "，".join(f"{JOB_ZH.get(j['name'], j['name'])}（{(j['last_error'] or '')[:40]}）"
                                       for j in failing[:4])
        lines.append(line)
    if mails:
        line = f"✉️ 邮件简报：近 24 小时已发 {mails.get('sent', 0)} 封"
        if mails.get("failed") or mails.get("retry"):
            line += f"，失败 {mails.get('failed', 0)} 封，待重试 {mails.get('retry', 0)} 封"
        lines.append(line)
    bad = [name for name, check in checks.items() if not check.get("ok", True)]
    if bad:
        lines.append("🧪 数据自检未通过：" + "，".join(bad))
    return "\n".join(lines)
