"""Anomaly push policy: record every anomaly once, push only new major ones.

Dedupe: one key per symbol/type/direction/day (keys built by the analytics modules). A key first
seen as minor is pushed once if it later becomes major (escalation), never twice.
Rate limit: at most `max_per_day` push messages per Beijing day; anomalies found in the
same refresh are batched into one message. Quiet hours (Beijing) deliver silently.
"""
import datetime as dt

import market_render as render
import market_store as store

MAX_PER_MESSAGE = 6


def parse_hours(text, default=(0, 8)):
    try:
        start, end = (int(x) for x in str(text).split("-", 1))
        if 0 <= start <= 23 and 0 <= end <= 24:
            return start, end
    except (TypeError, ValueError):
        pass
    return default


def in_quiet_hours(now, quiet):
    hour = dt.datetime.fromtimestamp(int(now), store.CST).hour
    start, end = quiet
    return start <= hour < end if start <= end else hour >= start or hour < end


def beijing_midnight(now):
    local = dt.datetime.fromtimestamp(int(now), store.CST)
    return int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def plan(con, anomalies, now, enabled=True, max_per_day=8, quiet=(0, 8)):
    """Record anomalies; return {'keys', 'text', 'silent'} for a push, or None."""
    fresh = [a for a in anomalies if store.add_alert(con, a, now)]
    major = [a for a in fresh if a.get("major")]
    if not enabled or not major:
        return None
    sent_today = store.pushed_since(con, beijing_midnight(now))
    if sent_today >= max_per_day:
        return None
    lines, seen = [], set()
    for anomaly in sorted(major, key=lambda a: a["severity"], reverse=True):
        if anomaly["symbol"] not in seen and len(lines) < MAX_PER_MESSAGE:
            seen.add(anomaly["symbol"])
            lines.append(anomaly)
    # Keys of same-symbol extras are marked too, so they do not resurface as a later push today.
    keys = [a["key"] for a in major if a["symbol"] in seen]
    return {"keys": keys, "silent": in_quiet_hours(now, quiet),
            "text": render.alert_push(lines, sent_today + 1, max_per_day, now)}
