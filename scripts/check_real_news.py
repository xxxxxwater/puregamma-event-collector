#!/usr/bin/env python3
"""Only genuine events ingested from feeds count. Test telegram messages do NOT."""
import datetime as dt
import os
import sqlite3

path=os.environ.get('DB_PATH','data/events.db')
if not os.path.exists(path):
    raise SystemExit('Events database not found: '+path)
with sqlite3.connect('file:'+os.path.abspath(path)+'?mode=ro',uri=True) as con:
    rows=con.execute('''SELECT e.title,e.source,e.priority,e.discovered,o.telegram_message_id,o.sent_at
        FROM outbox o JOIN events e ON e.id=o.event_id
        WHERE o.status='sent' AND o.telegram_message_id IS NOT NULL
        ORDER BY o.sent_at DESC LIMIT 5''').fetchall()
    pending=con.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0]
if rows:
    print('CONFIRMED: real feed events successfully delivered by Telegram Bot API')
    for title,src,pri,seen,msg,when in rows:
        print('id=%s priority=%s source=%s sent_at=%s title=%s' % (
           msg,pri,src,dt.datetime.fromtimestamp(when,dt.timezone.utc).isoformat(),title[:150]))
else:
    print('NOT YET CONFIRMED: no real RSS-derived event has a Telegram delivered message_id')
    print('Current pending=%s; P2 digest sends at the next UTC hour.' % pending)
