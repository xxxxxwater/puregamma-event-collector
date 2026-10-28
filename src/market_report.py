"""Coinbase market brief service v2: collection jobs -> snapshot -> briefs, alerts, commands.

Architecture and definitions: market/DESIGN.md. Each loop iteration (60s):
1. run due collection jobs (market_jobs), each on its own cadence with health records;
2. at a REPORT_INTERVAL_HOURS slot, build a fresh snapshot and broadcast it once
   (resumable, placeholders for failed parts, failure notice before the grace ends);
   otherwise rebuild the snapshot every SNAPSHOT_REFRESH_MINUTES for the commands;
3. after each snapshot, record anomalies and push new major ones (deduped, capped,
   silent during quiet hours).
With TELEGRAM_ENABLED=false the service only collects and keeps snapshots fresh; with
BRIEF_HTTP_PORT set it also serves the latest snapshot as the rendered e-mail brief
(GET /brief?locale=zh|en, GET /health) for the PureGamma AI daily brief (DESIGN.md section 10).
Read-only public data only; no Coinbase API key is used.
"""
import argparse
import html
import http.client
import http.server
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import market_alerts as alerts
import market_email as email_channel
import market_jobs as jobs
import market_render as render
import market_snapshot as snapshots
import market_sources as sources
import market_store as store

LOG = logging.getLogger("market-report")
REUSE_MAX_AGE = 3600


class RetryLater(RuntimeError):
    pass


class TelegramError(RuntimeError):
    """Sanitized Telegram failure: never carries the bot-token URL."""


def plain_text(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def telegram_sender(token, chat_id, sleep=time.sleep, attempts=3, opener=urllib.request.urlopen):
    def post(fields):
        req = urllib.request.Request("https://api.telegram.org/bot" + token + "/sendMessage",
                                     data=urllib.parse.urlencode(fields).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        with opener(req, timeout=20) as resp:
            if not json.load(resp).get("ok"):
                raise TelegramError("telegram API returned not-ok")

    def send(text, silent=False):
        fields = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        if silent:
            fields["disable_notification"] = "true"
        for attempt in range(1, attempts + 1):
            try:
                return post(fields)
            except urllib.error.HTTPError as ex:
                code, wait = ex.code, 5 * attempt
                if code == 429:
                    try:
                        wait = int(json.loads(ex.read(4096)).get("parameters", {}).get("retry_after", wait))
                    except (ValueError, TypeError, AttributeError):
                        pass
                elif code == 400 and "parse_mode" in fields:
                    # Malformed HTML must not lose the message: resend once as plain text.
                    LOG.warning("Telegram rejected HTML (400); resending as plain text")
                    fields = {k: v for k, v in fields.items() if k != "parse_mode"}
                    fields["text"] = plain_text(text)
                    continue
                if (code != 429 and code < 500) or attempt == attempts:
                    raise TelegramError("telegram HTTP %d" % code) from None
                sleep(min(max(wait, 1), 60))
            except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
                if attempt == attempts:
                    raise TelegramError("telegram network failure") from None
                sleep(5 * attempt)
        raise TelegramError("telegram send failed")
    return send


class MarketService:
    def __init__(self, con, send, hours=4, grace=45 * 60, refresh=15 * 60, retry=5 * 60, fetch=sources.get_json,
                 sleep=time.sleep, alerts_enabled=True, alerts_per_day=8, quiet=(0, 8),
                 email_send=None, email_recipients=None, email_colors=None):
        self.con, self.send, self.sleep = con, send, sleep
        self.email_send = email_send
        self.email_recipients = email_recipients or (lambda: [])
        self.email_colors = email_colors or {"zh": "cn", "en": "us"}
        self.last_snapshot = None
        self.hours, self.grace, self.refresh, self.retry = hours, grace, refresh, retry
        self.alerts_enabled, self.alerts_per_day, self.quiet = alerts_enabled, alerts_per_day, quiet
        self.collector = jobs.Collector(con, fetch=fetch, sleep=sleep)
        self.builder = snapshots.SnapshotBuilder(con, self.collector)
        self.last_refresh = 0
        self.next_attempt = 0
        store.meta_set(con, "interval_hours", hours)

    # ----- snapshot -----

    def build_snapshot(self, now):
        ctx = self.builder.build(now)
        sections, metrics, status = ctx["sections"], ctx["metrics"], ctx["status"]
        for key, names in snapshots.PART_SECTIONS.items():
            if status.get(key) == "ok":
                continue
            good = store.last_good(self.con, key, now - REUSE_MAX_AGE)
            if good:
                # Last known good data (<= 1h) beats a blank section; say so explicitly.
                for name in names:
                    if good["sections"].get(name):
                        sections[name] = render.reused(good["sections"][name], good["taken_at"], status[key])
                metrics[key] = dict(good["metrics"].get(key) or {}, reused_from=good["taken_at"])
                status[key] = "reused: " + status[key]
            else:
                for name in names:
                    sections[name] = render.unavailable(snapshots.PART_TITLES[name], status[key])
        snapshot_id = store.save_snapshot(self.con, now, sections, metrics, status)
        if ctx["quotes"]:
            store.replace_quotes(self.con, ctx["quotes"], now)
        self.last_refresh = now
        LOG.info("snapshot %d saved: %s", snapshot_id, ", ".join(f"{k}={v[:40]}" for k, v in status.items()))
        snapshot = {"id": snapshot_id, "taken_at": int(now), "sections": sections, "metrics": metrics, "status": status}
        self.last_snapshot = snapshot
        self.push_alerts(snapshots.SnapshotBuilder.anomalies(ctx), now)
        return snapshot

    def push_alerts(self, anomalies, now):
        if self.send is None:
            return
        try:
            push = alerts.plan(self.con, anomalies, now, self.alerts_enabled, self.alerts_per_day, self.quiet)
            if push:
                self.send(push["text"], silent=push["silent"])
                store.mark_pushed(self.con, push["keys"], push["silent"])
                LOG.info("alert pushed (%d anomalies, silent=%s)", len(push["keys"]), push["silent"])
        except Exception as ex:   # an alert problem must never break snapshots or briefs
            LOG.warning("alert push failed: %s", str(ex)[:200])

    # ----- broadcast -----

    def quiet_now(self, now):
        return alerts.in_quiet_hours(now, self.quiet)

    def broadcast(self, slot, now):
        row = store.broadcast_get(self.con, slot)
        if row is None:
            snapshot = self.build_snapshot(now)
            if store.has_data(snapshot["status"]):
                messages, kind = store.brief_messages(snapshot, self.hours), "brief"
            elif now + 2 * self.retry <= slot + self.grace:
                # Keep a full retry interval of margin so the failure notice is never skipped.
                raise RetryLater("all market sources failed")
            else:
                next_slot = slot + self.hours * 3600
                messages, kind = [render.failure_notice(slot, snapshot["status"], next_slot)], "failure"
            store.broadcast_start(self.con, slot, now, snapshot["id"], messages, kind)
            row = store.broadcast_get(self.con, slot)
        silent = self.quiet_now(now)
        for index in range(row["parts_sent"], len(row["messages"])):
            if index:
                self.sleep(1.1)
            self.send(row["messages"][index], silent=silent)
            store.broadcast_progress(self.con, slot, index + 1)
        outcome = "sent" if row["kind"] == "brief" else "failed"
        store.broadcast_finish(self.con, slot, outcome, now)
        LOG.info("slot %d broadcast %s (%d message(s), silent=%s)", slot, outcome, len(row["messages"]), silent)
        return outcome

    # ----- e-mail -----

    def email_tick(self, now):
        """Daily e-mail: each recipient once per local day at their local_time, own message, deduped per recipient."""
        if self.email_send is None:
            return
        due = []
        for recipient in self.email_recipients():
            slot = email_channel.due_today(recipient, now, self.grace)
            if not slot:
                continue
            key, local_date, due_at = slot
            row = store.delivery_get(self.con, key)
            if row and (row["status"] in ("sent", "failed") or (row["next_attempt_at"] or 0) > now):
                continue
            due.append((recipient, key, local_date, due_at))
        if not due:
            return
        snapshot = self.last_snapshot
        if not snapshot or now - snapshot["taken_at"] > self.refresh:
            snapshot = self.build_snapshot(now)
        rendered, sent, failed = {}, 0, 0
        for index, (recipient, key, local_date, due_at) in enumerate(due):
            locale = recipient["locale"]
            last_chance = now + 2 * self.retry > due_at + self.grace
            rhash = email_channel.recipient_hash(recipient["email"])
            try:
                if not store.has_data(snapshot["status"]):
                    raise email_channel.EmailError("所有数据源都失败，本期不发邮件")
                if locale not in rendered:
                    rendered[locale] = email_channel.Renderer(self.email_colors.get(locale), locale).render(snapshot)
                if index:
                    self.sleep(1.1)
                self.email_send(recipient["email"], *rendered[locale])
                store.delivery_record(self.con, key, rhash, local_date, locale, due_at, "sent", now,
                                      snapshot_id=snapshot["id"])
                sent += 1
            except Exception as ex:
                store.delivery_record(self.con, key, rhash, local_date, locale, due_at,
                                      "failed" if last_chance else "retry", now, error=str(ex)[:200],
                                      next_attempt_at=now + self.retry, snapshot_id=snapshot["id"])
                failed += 1
        LOG.info("e-mail briefs: %d sent, %d failed or retrying (of %d due)", sent, failed, len(due))

    def tick(self, now):
        store.meta_set(self.con, "heartbeat", int(now))
        self.collector.run_due(now)
        store.meta_set(self.con, "heartbeat", int(now))
        if self.send is not None:
            self.telegram_tick(now)
        self.email_tick(now)
        if now - self.last_refresh >= self.refresh:
            try:
                self.build_snapshot(now)
            except Exception:
                LOG.exception("snapshot refresh failed")
                self.last_refresh = now

    def telegram_tick(self, now):
        slot = store.slot_start(now, self.hours)
        row = store.broadcast_get(self.con, slot)
        if row is None or row["outcome"] == "sending":
            if now - slot <= self.grace:
                if now >= self.next_attempt:
                    try:
                        self.broadcast(slot, now)
                    except Exception as ex:
                        LOG.warning("slot %d broadcast attempt failed: %s; retry in %ds", slot, str(ex)[:200], self.retry)
                        self.next_attempt = now + self.retry
                return
            if row is not None:
                outcome = "partial" if row["parts_sent"] else "expired"
                store.broadcast_finish(self.con, slot, outcome, now)
                LOG.error("slot %d grace window closed; outcome=%s", slot, outcome)

    def run(self, clock=time.time):
        while True:
            self.tick(int(clock()))
            self.sleep(60)


class BriefServer:
    """Read-only view of the latest snapshot as the rendered e-mail, for the PureGamma AI daily brief.

    brief(locale) -> (200, {ok, locale, snapshot_id, taken_at, age_seconds, subject, html, text, parts}) or
    (503, {ok: false, error}) when there is no usable snapshot; health() reports heartbeat, snapshot age and jobs.
    Nothing about recipients passes through here: callers send only a locale.
    """

    def __init__(self, db_path, colors=None, manage=None, wordmark="PureGamma", clock=time.time):
        self.db_path, self.clock = db_path, clock
        self.colors = colors or {"zh": "cn", "en": "us"}
        self.manage, self.wordmark = manage or {}, wordmark
        self.cache, self.lock = {}, threading.Lock()

    def latest(self):
        if not os.path.exists(self.db_path):
            return None, 0, []
        con = store.open_readonly(self.db_path)
        try:
            return store.latest_snapshot(con), int(store.meta_get(con, "heartbeat", 0) or 0), store.jobs_health(con)
        finally:
            con.close()

    def brief(self, locale):
        if locale not in ("zh", "en"):
            return 400, {"ok": False, "error": "locale must be zh or en"}
        snapshot, _beat, _jobs = self.latest()
        if not snapshot:
            return 503, {"ok": False, "error": "no snapshot yet"}
        if not store.has_data(snapshot["status"]):
            return 503, {"ok": False, "error": "all market sources failed", "taken_at": snapshot["taken_at"]}
        key = (snapshot["id"], locale)
        with self.lock:
            rendered = self.cache.get(key)
        if rendered is None:
            rendered = email_channel.Renderer(self.colors.get(locale), locale, manage=self.manage.get(locale),
                                              wordmark=self.wordmark).render(snapshot)
            with self.lock:
                self.cache = {k: v for k, v in self.cache.items() if k[0] == snapshot["id"]}
                self.cache[key] = rendered
        subject, body, text = rendered
        parts = {k: "ok" if v == "ok" else "reused" if str(v).startswith("reused") else "unavailable"
                 for k, v in snapshot["status"].items()}
        return 200, {"ok": True, "locale": locale, "snapshot_id": snapshot["id"], "taken_at": snapshot["taken_at"],
                     "age_seconds": int(self.clock()) - snapshot["taken_at"], "subject": subject, "html": body,
                     "text": text, "parts": parts}

    def health(self):
        snapshot, beat, jobs_rows = self.latest()
        now = int(self.clock())
        ok = now - beat <= store.HEARTBEAT_MAX_AGE
        failing = sorted(r["name"] for r in jobs_rows if r["last_error"])
        return (200 if ok else 503), {"ok": ok, "heartbeat_age": now - beat,
                                      "snapshot_age": now - snapshot["taken_at"] if snapshot else None,
                                      "jobs_ok": len(jobs_rows) - len(failing), "jobs_total": len(jobs_rows),
                                      "jobs_failing": failing}


def brief_handler(server):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "market-brief/1"

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            try:
                if url.path == "/brief":
                    code, payload = server.brief((urllib.parse.parse_qs(url.query).get("locale") or ["zh"])[0])
                elif url.path == "/health":
                    code, payload = server.health()
                else:
                    code, payload = 404, {"ok": False, "error": "not found"}
            except Exception as ex:      # a bad request must never take the collector down
                LOG.warning("brief http %s failed: %s", url.path, str(ex)[:200])
                code, payload = 500, {"ok": False, "error": type(ex).__name__}
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            LOG.debug("http " + fmt, *args)
    return Handler


def serve_briefs(server, port, host="0.0.0.0"):
    """Serve BriefServer on a daemon thread; returns the HTTP server (shutdown() stops it)."""
    httpd = http.server.ThreadingHTTPServer((host, port), brief_handler(server))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="brief-http", daemon=True).start()
    return httpd


def health(path, now):
    try:
        con = store.open_readonly(path)
        try:
            beat = int(store.meta_get(con, "heartbeat", 0) or 0)
        finally:
            con.close()
    except Exception:
        return 1
    return 0 if now - beat <= store.HEARTBEAT_MAX_AGE else 1


def email_sender_from_env():
    """SMTP sender from env + password file; None (with a warning) when incomplete."""
    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    password = ""
    path = os.environ.get("SMTP_PASSWORD_FILE", "/run/secrets/smtp_password")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            password = f.read().strip()
    if not host or (user and not password):
        LOG.warning("e-mail disabled: SMTP_HOST and the SMTP password file are required")
        return None
    sender = os.environ.get("EMAIL_FROM", "").strip() or f"PureGamma AI <{user}>"
    return email_channel.smtp_sender(host, int(os.environ.get("SMTP_PORT", "587")), user, password, sender,
                                     os.environ.get("SMTP_SECURITY", "auto"), os.environ.get("EMAIL_REPLY_TO", "").strip() or None)


def env_flag(name, default=True):
    return os.environ.get(name, "true" if default else "false").strip().lower() in ("1", "true", "yes", "on")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="collect once, print the brief, send nothing")
    parser.add_argument("--once", action="store_true", help="collect once and send the brief now (manual)")
    parser.add_argument("--health", action="store_true", help="exit 0 when the service heartbeat is fresh")
    parser.add_argument("--email-preview", metavar="PATH", help="render the latest snapshot as the HTML e-mail into PATH")
    parser.add_argument("--email-test", metavar="ADDRESS", help="send the latest snapshot e-mail to ADDRESS now")
    parser.add_argument("--locale", choices=("zh", "en"), default="zh", help="language for --email-preview/--email-test")
    args = parser.parse_args()
    db_path = os.environ.get("REPORT_DB_PATH", store.DEFAULT_PATH)
    if args.health:
        sys.exit(health(db_path, int(time.time())))
    colors = {"zh": os.environ.get("EMAIL_COLORS", "cn"), "en": os.environ.get("EMAIL_COLORS_EN", "us")}
    if args.email_preview:
        snapshot = store.latest_snapshot(store.open_readonly(db_path))
        if not snapshot:
            raise SystemExit("no v2 snapshot yet")
        subject, body, _ = email_channel.Renderer(colors[args.locale], args.locale).render(snapshot)
        with open(args.email_preview, "w", encoding="utf-8") as f:
            f.write(body)
        print(subject)
        return
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)sZ %(levelname)s %(name)s %(message)s")
    hours = int(os.environ.get("REPORT_INTERVAL_HOURS", "4"))
    if hours <= 0 or 24 % hours:
        LOG.warning("REPORT_INTERVAL_HOURS=%d does not divide 24; using 4", hours)
        hours = 4
    send = None
    telegram = env_flag("TELEGRAM_ENABLED")
    if args.once and not telegram:
        raise SystemExit("--once needs Telegram (TELEGRAM_ENABLED=false here)")
    if telegram and not args.dry_run:
        with open(os.environ.get("TELEGRAM_TOKEN_FILE", "/run/secrets/telegram_bot_token"), encoding="utf-8") as f:
            token = f.read().strip()
        if ":" not in token:
            raise SystemExit("telegram bot token missing/invalid")
        send = telegram_sender(token, os.environ["TELEGRAM_CHAT_ID"].strip())
    quiet = alerts.parse_hours(os.environ.get("QUIET_HOURS", "0-8"))
    email_send = email_sender_from_env() if (env_flag("EMAIL_ENABLED", False) or args.email_test) else None
    if args.email_test:
        if email_send is None:
            raise SystemExit("e-mail not configured: set SMTP_HOST, SMTP_USER and the SMTP password file")
        snapshot = store.latest_snapshot(store.open_readonly(db_path))
        if not snapshot:
            raise SystemExit("no v2 snapshot yet")
        subject, body, text = email_channel.Renderer(colors[args.locale], args.locale).render(snapshot)
        email_send(args.email_test, subject, body, text)
        print("test e-mail sent:", subject)
        return
    recipients_file = os.environ.get("EMAIL_RECIPIENTS_FILE", "/run/secrets/email_recipients")
    fallback = [r.strip() for r in os.environ.get("EMAIL_TO", "").split(",") if r.strip()]

    def email_recipients():
        return email_channel.load_recipients(recipients_file, fallback, os.environ.get("EMAIL_DEFAULT_LOCALE", "zh"),
                                             os.environ.get("EMAIL_TIME", "08:30"))
    service = MarketService(store.connect(db_path), send, hours=hours,
                            grace=int(os.environ.get("REPORT_GRACE_MINUTES", "45")) * 60,
                            refresh=int(os.environ.get("SNAPSHOT_REFRESH_MINUTES", "15")) * 60,
                            alerts_enabled=env_flag("ALERTS_ENABLED"),
                            alerts_per_day=int(os.environ.get("ALERTS_MAX_PER_DAY", "8")), quiet=quiet,
                            email_send=email_send, email_recipients=email_recipients, email_colors=colors)
    port = int(os.environ.get("BRIEF_HTTP_PORT", "0") or 0)
    LOG.info("starting v2; telegram=%s; interval=%dh (%s); refresh=%ds; alerts=%s max/day=%d; quiet hours (Beijing) "
             "%02d-%02d; e-mail=%s; brief http=%s; no API key", "on" if telegram else "off", hours,
             store.schedule_label(hours), service.refresh, service.alerts_enabled, service.alerts_per_day, *quiet,
             (f"on, {len(email_recipients())} recipient(s), daily at each recipient's local time") if email_send else "off",
             port or "off")
    if args.dry_run or args.once:
        now = int(time.time())
        service.collector.run_due(now)
        service.send = None            # a manual run never pushes alerts
        snapshot = service.build_snapshot(now)
        messages = store.brief_messages(snapshot, hours)
        if args.dry_run:
            print("\n\n----- 分条 -----\n\n".join(messages))
            return
        for index, text in enumerate(messages):
            if index:
                time.sleep(1.1)
            send(text)
        LOG.info("manual brief sent (%d message(s))", len(messages))
        return
    if port:
        manage = {loc: os.environ.get(f"BRIEF_MANAGE_{loc.upper()}", "").strip() or None for loc in ("zh", "en")}
        serve_briefs(BriefServer(db_path, colors, manage, os.environ.get("BRIEF_WORDMARK", "PureGamma").strip()), port)
    service.run()


if __name__ == "__main__":
    main()
