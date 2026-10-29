#!/usr/bin/env python3
"""PureGamma light-weight RSS/Atom ingest, conservative event dedupe, Telegram outbox.

Only stdlib dependencies. Secrets are read from a Docker secret, never logged.
"""
import argparse
import concurrent.futures
import datetime as dt
import email.utils
import hashlib
import html
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import random
import re
import signal
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from translation import Translator, TranslationError
from bot_commands import run_command_poller

LOG = logging.getLogger("collector")
APP_UA = "PureGammaEventCollector/1.0 (+rss-push; contact administrator)"
UTC = dt.timezone.utc
STOP = threading.Event()
PRIORITIES = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
STOPlist = {"the", "a", "an", "of", "on", "at", "for", "to", "is", "has", "with", "by", "from", "and", "in", "its", "be", "will", "as", "that", "after", "new"}
TITLE_VERBS = {"approve", "approves", "approved", "propose", "proposes", "proposed", "deny", "denies", "denied", "launch", "launches", "launched", "halt", "halts", "halted", "suspend", "suspends", "suspended", "resume", "resumes", "resumed", "list", "lists", "listed", "delist", "delists", "delisted"}


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def strip_markup(text):
    parser = PlainText()
    try:
        parser.feed(text or "")
        text = " ".join(parser.parts)
    except Exception:
        pass
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def text_of(element, tag):
    e = element.find(tag)
    return ("".join(e.itertext()).strip() if e is not None else "")


def child_of(element, local_name):
    for e in element:
        if e.tag.rsplit("}", 1)[-1] == local_name:
            return e
    return None


def parse_date(value):
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (ValueError, TypeError):
        try:
            d = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return int(d.timestamp())


def parse_feed(raw):
    """Strictly handles RSS 2.0 and Atom; no external entity network loading."""
    root = ET.fromstring(raw)
    tag = root.tag.rsplit("}", 1)[-1].lower()
    entries = []
    if tag == "rss" or tag == "rdf":
        channel = root.find("channel")
        if channel is None:
            channel = root
        items = channel.findall("item")
        # RSS 1.0 puts <item> and its children in the purl.org/rss/1.0 namespace.
        q = (lambda name: "{*}" + name) if tag == "rdf" else (lambda name: name)
        if tag == "rdf":
            items = root.findall("{*}item")
        for item in items:
            title = strip_markup(text_of(item, q("title")))
            link = text_of(item, q("link"))
            guid = text_of(item, q("guid"))
            body = strip_markup(text_of(item, q("description")))
            pub = parse_date(text_of(item, "pubDate") or text_of(item, "{*}date"))
            if title:
                entries.append({"title": title, "link": link, "guid": guid, "summary": body[:800], "published": pub})
    elif tag == "feed":
        for item in root.findall("{*}entry"):
            title = strip_markup(text_of(item, "{*}title"))
            guid = text_of(item, "{*}id")
            body = strip_markup(text_of(item, "{*}summary") or text_of(item, "{*}content"))
            link = ""
            for a in item.findall("{*}link"):
                if a.get("rel", "alternate") == "alternate" and a.get("href"):
                    link = a.attrib["href"]
                    break
            pub = parse_date(text_of(item, "{*}published") or text_of(item, "{*}updated"))
            if title:
                entries.append({"title": title, "link": link, "guid": guid, "summary": body[:800], "published": pub})
    else:
        raise ValueError("unsupported RSS/Atom root")
    return entries


def canonical_url(url):
    if not url:
        return ""
    try:
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ("http", "https"):
            return ""
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
             if not (k.lower().startswith("utm_") or k.lower() in {"fbclid", "gclid", "ref", "igshid"})]
        netloc = p.netloc.lower().removesuffix(":443").removesuffix(":80")
        path = p.path.rstrip("/") or "/"
        return urllib.parse.urlunsplit((p.scheme.lower(), netloc, path, urllib.parse.urlencode(sorted(q)), ""))
    except ValueError:
        return ""


def normalized_title(title):
    s = unicodedata.normalize("NFKC", strip_markup(title)).lower()
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", s)
    return " ".join(s.split())[:300]


def title_tokens(value):
    return {w for w in normalized_title(value).split() if w not in STOPlist}


def same_event(a, b):
    na, nb = normalized_title(a), normalized_title(b)
    if na == nb and len(na) >= 16:
        return True
    ta, tb = title_tokens(a), title_tokens(b)
    if min(len(ta), len(tb)) < 5:
        return False
    if {x for x in ta if x.isdecimal()} != {x for x in tb if x.isdecimal()}:
        return False
    if {x for x in ta if x in TITLE_VERBS} != {x for x in tb if x in TITLE_VERBS}:
        return False
    return len(ta & tb) / max(len(ta | tb), 1) >= 0.88


def classify(title, source):
    t = normalized_title(title)
    category = "general"
    priority = source.get("default_priority", "P3")
    trust = source.get("trust", "C")
    def apply(cat, level):
        nonlocal priority, category
        category = cat
        if PRIORITIES[level] < PRIORITIES[priority]:
            priority = level

    # CJK characters are all \w, so \b never fires between adjacent Chinese
    # characters: Chinese keywords must stay outside the \b(...)\b group.
    if re.search(r"\b(fomc|federal reserve|fed|interest rate|rate decision|powell)\b|美联储|降息|加息", t):
        apply("macro.monetary", "P1")
    if re.search(r"\b(cpi|inflation|nonfarm|non farm|payrolls|pce|unemployment)\b|通胀|非农", t):
        apply("macro.data", "P1")
    if re.search(r"\b(sec|cftc|etf|regulation|regulator|custody)\b|批准|监管", t):
        apply("regulation", "P1")
    if re.search(r"\b(btc|bitcoin|eth|ethereum|crypto)\b|币安|比特币|以太坊|币圈", t):
        if category == "general":
            category = "crypto.market"
    if re.search(r"\b(hack|hacked|exploit|breach|bankrupt|insolvent|outage|security incident)\b|遭攻击|盗币|破产", t):
        apply("crypto.security", "P0")
    if re.search(r"(\bdelist(?:s|ed|ing)?\b|\bsuspend(?:s|ed)? trading\b|\bhalts? trading\b|下架|暂停交易)", t):
        apply("exchange.delisting", "P0")
    elif re.search(r"\b(listing|new listing|listed on|new token)\b|上线", t):
        apply("exchange.listing", "P1")
    if re.search(r"\b(fomc statement|rate decision|cut rates|hike rates|cuts rates|raises rates)\b|利率决议", t):
        apply("macro.policy_decision", "P0")
    if re.search(r"\b(earnings|revenue|guidance)\b|财报", t):
        apply("equities.earnings", "P1")
    # Do not elevate unverified discussion-group posts to P0/P1.
    if trust == "C":
        # Unverified group messages can be included in digest, never P0/P1.
        priority = "P2" if PRIORITIES[priority] <= PRIORITIES["P2"] else "P3"
    elif trust == "B" and priority == "P0":
        priority = "P1"
    return category, priority


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utc_now():
    return int(time.time())


def next_utc_hour(now):
    return (int(now) // 3600 + 1) * 3600


def db_connect(path):
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def init_db(con, sources):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS sources (
            id TEXT PRIMARY KEY, seeded INTEGER NOT NULL DEFAULT 0,
            etag TEXT, modified TEXT, last_ok INTEGER, last_check INTEGER,
            last_error TEXT, consecutive_errors INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, canonical_title TEXT NOT NULL,
            source TEXT NOT NULL, link TEXT, category TEXT NOT NULL,
            priority TEXT NOT NULL, trust TEXT NOT NULL,
            published INTEGER, discovered INTEGER NOT NULL, last_seen INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_discovered ON events(discovered);
        CREATE TABLE IF NOT EXISTS articles (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, event_id TEXT NOT NULL,
            title TEXT NOT NULL, link TEXT, published INTEGER, discovered INTEGER NOT NULL,
            FOREIGN KEY(event_id) REFERENCES events(id)
        );
        CREATE INDEX IF NOT EXISTS idx_articles_event ON articles(event_id);
        CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
            priority TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            due_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT, telegram_message_id INTEGER,
            sent_at INTEGER,
            FOREIGN KEY(event_id) REFERENCES events(id)
        );
        CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(status,due_at);
    """)
    for src in sources:
        con.execute("INSERT OR IGNORE INTO sources(id) VALUES (?)", (src["id"],))
    con.commit()


def validate_config(config):
    ids = set()
    for s in config.get("sources", []):
        if not s.get("id") or not s.get("url") or not s.get("name"):
            raise ValueError("source missing id/name/url")
        if s["id"] in ids:
            raise ValueError("duplicated source id")
        ids.add(s["id"])
        if not (s["url"].startswith("https://") or s["url"].startswith("http://rsshub:1200/") or s["url"].startswith("http://group-reader:8766/")):
            raise ValueError("invalid source URL; use HTTPS or internal RSSHub")
        if s.get("trust", "C") not in {"A", "B", "C"}:
            raise ValueError("trust must be A/B/C")
        if s.get("default_priority", "P3") not in PRIORITIES:
            raise ValueError("priority must be P0/P1/P2/P3")
        if int(s.get("interval_seconds", 300)) < 30:
            raise ValueError("poll minimum is 30 seconds")
    return config


def fetch(source, etag, modified, config):
    headers = {"User-Agent": APP_UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"}
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    req = urllib.request.Request(source["url"], headers=headers)
    timeout = int(config["http_timeout_seconds"])
    # urlopen's timeout applies per socket operation; a server dribbling bytes
    # could otherwise hold a worker, and the whole poll loop, indefinitely.
    deadline = time.monotonic() + float(config.get("http_total_timeout_seconds", timeout * 3))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            length = int(response.headers.get("Content-Length") or 0)
            limit = int(config["max_feed_bytes"])
            if length > limit:
                raise ValueError("feed exceeds configured max length")
            chunks, size = [], 0
            while size <= limit:
                chunk = response.read1(min(65536, limit + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if time.monotonic() > deadline:
                    raise TimeoutError("feed download exceeded total timeout")
            body = b"".join(chunks)
            if len(body) > limit:
                raise ValueError("feed exceeds configured max length")
            entries = parse_feed(body)
            if source.get("experimental") and not entries:
                raise ValueError("public group preview returned no entries; availability not verified")
            # HTML/error pages must not be silently accepted as an empty RSS.
            return entries, response.headers.get("ETag"), response.headers.get("Last-Modified"), False
    except urllib.error.HTTPError as err:
        if err.code == 304:
            return [], etag, modified, True
        raise RuntimeError("HTTP status %d" % err.code) from None


def add_entries(con, source, entries, now, seeded, max_age_h=24, p2_age_h=6):
    added, events_added, enqueued = 0, 0, 0
    # Process chronological so official earliest record gets canonical event owner.
    for item in sorted(entries, key=lambda x: (x.get("published") or 0, x["title"])):
        clean_link = canonical_url(item.get("link", ""))
        unique = item.get("guid") or clean_link or normalized_title(item["title"])
        article_id = sha(source["id"] + "|" + unique)
        if con.execute("SELECT 1 FROM articles WHERE id=?", (article_id,)).fetchone():
            continue
        matches = con.execute("SELECT id,title,priority,trust FROM events WHERE discovered>=? ORDER BY discovered DESC LIMIT 600", (now - 21600,)).fetchall()
        existing = next((r for r in matches if same_event(r["title"], item["title"])), None)
        category, priority = classify(item["title"], source)
        age = now - (item.get("published") or now)
        if existing:
            event_id = existing["id"]
            old_trust = existing["trust"]
            more_trusted = "ABC".index(source.get("trust", "C")) < "ABC".index(old_trust)
            higher_priority = PRIORITIES[priority] < PRIORITIES[existing["priority"]]
            if more_trusted and higher_priority:
                # Official confirmation can promote a rumor to an alert even when
                # the article belongs to the same canonical event.
                con.execute("UPDATE events SET title=?,canonical_title=?,source=?,link=?,category=?,priority=?,trust=?,published=?,last_seen=? WHERE id=?",
                            (item["title"][:700], normalized_title(item["title"]), source["name"], clean_link,
                             category, priority, source.get("trust", "C"), item.get("published"), now, event_id))
                if seeded and priority != "P3" and age <= (p2_age_h if priority == "P2" else max_age_h) * 3600:
                    due = next_utc_hour(now) if priority == "P2" else now
                    exists_outbox = con.execute("SELECT id FROM outbox WHERE event_id=?", (event_id,)).fetchone()
                    if exists_outbox:
                        # Escalation by a more trusted source is a meaningful new alert.
                        con.execute("UPDATE outbox SET status='pending', attempts=0, priority=?,due_at=?,sent_at=NULL,telegram_message_id=NULL,last_error=NULL WHERE event_id=?", (priority, due, event_id))
                    else:
                        con.execute("INSERT INTO outbox(event_id,priority,due_at) VALUES (?,?,?)", (event_id, priority, due))
                    enqueued += 1
            else:
                con.execute("UPDATE events SET last_seen=? WHERE id=?", (now, event_id))
        else:
            event_id = "evt_" + uuid.uuid4().hex[:20]
            con.execute("INSERT INTO events(id,title,canonical_title,source,link,category,priority,trust,published,discovered,last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (event_id, item["title"][:700], normalized_title(item["title"]), source["name"], clean_link, category, priority, source.get("trust", "C"), item.get("published"), now, now))
            events_added += 1
            if seeded and age <= (p2_age_h if priority == "P2" else max_age_h) * 3600 and priority != "P3":
                due = next_utc_hour(now) if priority == "P2" else now
                con.execute("INSERT OR IGNORE INTO outbox(event_id,priority,due_at) VALUES (?,?,?)", (event_id, priority, due))
                enqueued += 1
        con.execute("INSERT INTO articles(id,source,event_id,title,link,published,discovered) VALUES(?,?,?,?,?,?,?)",
                    (article_id, source["id"], event_id, item["title"][:700], clean_link, item.get("published"), now))
        added += 1
    return added, events_added, enqueued


def ingest_result(con, source, result, now, config):
    row = con.execute("SELECT seeded,consecutive_errors FROM sources WHERE id=?", (source["id"],)).fetchone()
    seeded = bool(row["seeded"])
    interval = int(source.get("interval_seconds", 300))
    if isinstance(result, Exception):
        errors = row["consecutive_errors"] + 1
        err = str(result)[:250]
        # Do not include HTTP request URL here: Telegram tokens must never appear in logs.
        con.execute("UPDATE sources SET last_check=?, last_error=?, consecutive_errors=?, next_at=? WHERE id=?",
                    (now, err, errors, now + min(3600, interval * (2 ** min(errors, 4))) + random.randint(0, 5), source["id"]))
        con.commit()
        LOG.warning("source=%s failure=%d reason=%s", source["id"], errors, err)
        return
    entries, etag, modified, not_changed = result
    if not_changed and not seeded:
        # Unexpected 304 during first fetch; force full fetch next time.
        etag, modified = None, None
    added, events_added, enqueued = (0, 0, 0) if not_changed else add_entries(
        con, source, entries, now, seeded, int(config["max_notification_age_hours"]), int(config["max_p2_age_hours"]))
    # An empty first response (RSSHub hiccup, upstream truncation) is not a
    # baseline: seeding on it would push the whole backlog on the next fetch.
    now_seeded = seeded or (not not_changed and bool(entries))
    con.execute("UPDATE sources SET seeded=?, etag=?, modified=?, last_ok=?, last_check=?, last_error=NULL, consecutive_errors=0, next_at=? WHERE id=?",
                (int(now_seeded), etag if now_seeded else None, modified if now_seeded else None,
                 now, now, now + interval + random.randint(0, 8), source["id"]))
    con.commit()
    if added or enqueued or now_seeded != seeded:
        LOG.info("source=%s seed=%s entries=%d new_events=%d enqueued=%d", source["id"], "complete" if not seeded else "done", added, events_added, enqueued)


CATEGORY_ZH = {
    "general": "综合资讯", "macro.monetary": "货币政策", "macro.data": "宏观数据",
    "macro.policy_decision": "利率决议", "regulation": "金融监管",
    "crypto.market": "加密市场", "crypto.security": "安全事件",
    "exchange.delisting": "交易所下架", "exchange.listing": "交易所上线",
    "equities.earnings": "公司财报",
}


def telegram_message(event, title_zh=None):
    label = {"P0": "🚨", "P1": "⚠️", "P2": "📰"}.get(event["priority"], "📎")
    original = (event["title"] or "")[:400]
    chinese = (title_zh or original)[:500]
    cat = CATEGORY_ZH.get(event["category"], event["category"])
    s = (f"{label} <b>{event['priority']} | {html.escape(cat)}</b>\n"
         f"<b>{html.escape(chinese)}</b>\n"
         f"英文原标题：{html.escape(original)}\n"
         f"来源：{html.escape(event['source'])} · 可信度 {html.escape(event['trust'])}\n")
    if event["published"]:
        stamp = dt.datetime.fromtimestamp(event["published"], tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
        s += f"发布时间：{stamp}\n"
    if event["link"]:
        s += f'<a href="{html.escape(event["link"], quote=True)}">查看原文</a>'
    return s


def telegram_p2_digest(items, translated):
    message = "📰 <b>P2 中文资讯整点摘要</b>\n\n"
    for event, zh in zip(items, translated):
        label = html.escape((zh or event["title"])[:150])
        orig = html.escape((event["title"] or "")[:105])
        src = html.escape((event["source"] or "")[:55])
        if event["link"]:
            url = html.escape(event["link"], quote=True)
            message += f'• <a href="{url}">{label}</a> — {src}\n  原文：{orig}\n'
        else:
            message += f"• {label} — {src}\n  原文：{orig}\n"
    return message


def telegram_send(token, chat_id, message):
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode("utf-8")
    req = urllib.request.Request("https://api.telegram.org/bot" + token + "/sendMessage", data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.load(resp)
            if payload.get("ok"):
                return True, int(payload["result"]["message_id"]), 0, None
            return False, None, 60, "telegram API returned not-ok"
    except urllib.error.HTTPError as ex:
        retry = 60
        try:
            result = json.loads(ex.read(4096).decode("utf-8"))
            retry = max(1, int(result.get("parameters", {}).get("retry_after", retry)))
        except (ValueError, KeyError, TypeError):
            pass
        return False, None, retry, "HTTP %d" % ex.code
    except Exception:
        # urllib errors may contain the full bot-token URL. Never serialize exception.
        return False, None, 90, "network failure"


def expire_stale_outbox(con, now, max_age_h=24, p2_age_h=6):
    """Drop pending pushes that outlived the enqueue age limits.

    After a long Telegram outage, collector downtime or a message Telegram keeps
    rejecting, a "breaking" alert hours late is noise; the event stays queryable.
    """
    cur = con.execute("""UPDATE outbox SET status='expired', last_error='stale: not delivered in time'
                         WHERE status='pending' AND due_at<=? AND
                         (SELECT last_seen FROM events e WHERE e.id=outbox.event_id)
                             < ? - CASE WHEN priority='P2' THEN ? ELSE ? END""",
                      (now, now, int(p2_age_h) * 3600, int(max_age_h) * 3600))
    if cur.rowcount:
        con.commit()
        LOG.warning("outbox expired stale notifications count=%d", cur.rowcount)
    return cur.rowcount


def process_outbox(con, token, chat_id, now, translator=None, max_age_h=24, p2_age_h=6):
    # P0/P1 immediate; P2 grouped hourly (max 5 items per digest).
    translator = translator or Translator(enabled=False)
    expire_stale_outbox(con, now, max_age_h, p2_age_h)
    rows = con.execute("""SELECT o.id, o.attempts, o.priority, e.* FROM outbox o
                          JOIN events e ON e.id=o.event_id WHERE o.status='pending' AND o.due_at<=?
                          ORDER BY o.due_at ASC, o.id ASC LIMIT 20""", (now,)).fetchall()
    if not rows:
        return 0
    delivered = 0
    i = 0
    while i < len(rows):
        r = rows[i]
        if r["priority"] == "P2":
            batch = [r]
            j = i + 1
            while j < len(rows) and rows[j]["priority"] == "P2" and len(batch) < 5:
                batch.append(rows[j])
                j += 1
            i = j
        else:
            batch = [r]
            i += 1
        try:
            translated = [translator.translate(con, item["title"]) for item in batch]
            if r["priority"] == "P2":
                message = telegram_p2_digest(batch, translated)
            else:
                message = telegram_message(r, translated[0])
            if len(message) > 3900:
                raise TranslationError("formatted notification too long")
        except TranslationError as exc:
            for item in batch:
                delay = min(600, max(90, 2 ** min(item["attempts"] + 1, 9)))
                con.execute("UPDATE outbox SET attempts=attempts+1, due_at=?, last_error=? WHERE id=?",
                            (now + delay, str(exc)[:120], item["id"]))
            con.commit()
            LOG.warning("translation pending: %s; queued=%d", str(exc), len(batch))
            # Translation offline should not trigger a burst of source requests.
            break
        ok, message_id, retry, err = telegram_send(token, chat_id, message)
        if ok:
            for item in batch:
                con.execute("UPDATE outbox SET status='sent', telegram_message_id=?, sent_at=?, attempts=attempts+1, last_error=NULL WHERE id=?",
                            (message_id, now, item["id"]))
            delivered += len(batch)
            LOG.info("telegram delivered count=%d last_id=%s lang=zh", len(batch), message_id)
        else:
            for item in batch:
                delay = min(3600, max(retry, 2 ** min(item["attempts"] + 1, 10)))
                con.execute("UPDATE outbox SET attempts=attempts+1, due_at=?, last_error=? WHERE id=?",
                            (now + delay, err, item["id"]))
            LOG.warning("telegram delivery failed reason=%s", err)
            con.commit()
            break
        con.commit()
        time.sleep(1.1)
    return delivered


def cleanup(con, days):
    cutoff = utc_now() - int(days) * 86400
    con.execute("DELETE FROM outbox WHERE status='sent' AND sent_at<?", (cutoff,))
    con.execute("DELETE FROM outbox WHERE status='expired' AND due_at<?", (cutoff,))
    con.execute("DELETE FROM articles WHERE discovered<? AND event_id NOT IN (SELECT event_id FROM outbox WHERE status='pending')", (cutoff,))
    con.execute("DELETE FROM events WHERE discovered<? AND id NOT IN (SELECT event_id FROM articles) AND id NOT IN (SELECT event_id FROM outbox)", (cutoff,))
    con.commit()
    con.execute("PRAGMA wal_checkpoint(PASSIVE)")


def launch_health_server(db_path, port, sources):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/healthz", "/status"):
                self.send_error(404)
                return
            try:
                with db_connect(db_path) as conn:
                    source_states = [dict(x) for x in conn.execute("SELECT id,seeded,last_ok,last_check,last_error,consecutive_errors FROM sources ORDER BY id")]
                    pending = conn.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0]
                    sent = conn.execute("SELECT COUNT(*) FROM outbox WHERE status='sent'").fetchone()[0]
                    events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                payload = {"ok": True, "events": events, "pending": pending, "sent": sent}
                if self.path == "/status":
                    payload["sources"] = source_states
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_error(503, "storage unavailable")
        def log_message(self, format_string, *args):
            return
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--once", action="store_true", help="poll each source once then drain outbox")
    args = parser.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)sZ %(levelname)s %(message)s")
    with open(os.environ.get("SOURCES_PATH", "/config/sources.json"), encoding="utf-8") as f:
        config = validate_config(json.load(f))
    sources = [s for s in config["sources"] if s.get("enabled", True)]
    if args.check_config:
        print("configuration valid: %s enabled sources" % len(sources))
        return
    token_path = os.environ.get("TELEGRAM_TOKEN_FILE", "/run/secrets/telegram_bot_token")
    with open(token_path, encoding="utf-8") as f:
        token = f.read().strip()
    if not token or ":" not in token:
        raise RuntimeError("telegram bot token missing/invalid")
    chat_id = os.environ["TELEGRAM_CHAT_ID"].strip()
    os.makedirs(os.path.dirname(os.environ.get("DB_PATH", "/data/events.db")), exist_ok=True)
    db_path = os.environ.get("DB_PATH", "/data/events.db")
    con = db_connect(db_path)
    init_db(con, sources)
    translator = Translator()
    translator.init_cache(con)
    LOG.info("Telegram delivery translation: %s", "simplified Chinese" if translator.enabled else "disabled")
    server = launch_health_server(db_path, int(os.environ.get("HEALTH_PORT", "8765")), sources)
    LOG.info("started with %d enabled feeds (tokens redacted)", len(sources))
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    heartbeat = {"started": utc_now(), "last_tick": 0, "loop_count": 0}
    heartbeat_lock = threading.Lock()
    if not args.once:
        threading.Thread(
            target=run_command_poller,
            args=(token, chat_id, db_path, heartbeat, heartbeat_lock, sources, STOP, utc_now),
            name="telegram-commands", daemon=True
        ).start()
    last_cleanup = 0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=int(config["settings"]["max_workers"])) as pool:
            while not STOP.is_set():
                now = utc_now()
                with heartbeat_lock:
                    heartbeat["last_tick"] = now
                    heartbeat["loop_count"] += 1
                due = []
                for s in sources:
                    r = con.execute("SELECT etag,modified,next_at FROM sources WHERE id=?", (s["id"],)).fetchone()
                    if r and r["next_at"] <= now:
                        due.append((s, r["etag"], r["modified"]))
                futures = {pool.submit(fetch, src, etag, mod, config["settings"]): src for src, etag, mod in due}
                for future in concurrent.futures.as_completed(futures):
                    src = futures[future]
                    try:
                        result = future.result()
                    except Exception as err:
                        result = err
                    ingest_result(con, src, result, utc_now(), config["settings"])
                process_outbox(con, token, chat_id, utc_now(), translator,
                               int(config["settings"]["max_notification_age_hours"]),
                               int(config["settings"]["max_p2_age_hours"]))
                if now - last_cleanup >= 86400:
                    cleanup(con, int(config["settings"]["db_retention_days"]))
                    last_cleanup = now
                if args.once:
                    break
                STOP.wait(float(config["settings"].get("poll_tick_seconds", 2)))
    finally:
        server.shutdown()
        con.close()


if __name__ == "__main__":
    main()
