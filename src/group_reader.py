"""Authorized read-only MTProto WatcherGuruChat -> internal RSS feed.

Requires the account owner to log in on the VM and join the target group.
No messages sent to Telegram; this process only reads messages.
"""
import argparse
import asyncio
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
import os
from pathlib import Path
import re
import threading

from group_feed import atomic_json, read_json, make_rss

LOG = logging.getLogger('group-reader')
STATE = Path('/state')
FEED = STATE / 'feed.json'
OFFSET = STATE / 'offset.json'
SESSION = str(STATE / 'watcherguru.session')
GROUP = 'WatcherGuruChat'
# Avoid filling 20 GiB with high-volume unverified group chat.
TOPICS = re.compile(r'(?i)\b(btc|bitcoin|eth|ethereum|binance|coinbase|sec|cftc|fomc|fed|cpi|etf|treasury|inflation|liquidation|hack|exploit|stablecoin|blackrock|tariff|nasdaq|ipo|interest rates?)\b|比特币|以太坊|美联储|降息|加息|清算|交易所|监管|利率|关税')


def credentials():
    try:
        api_id = int(Path('/run/secrets/telegram_api_id').read_text().strip())
        api_hash = Path('/run/secrets/telegram_api_hash').read_text().strip()
    except Exception as exc:
        raise RuntimeError('missing Telegram API_ID / API_HASH secrets; run scripts/enable_group.sh on VM') from exc
    if not api_hash:
        raise RuntimeError('empty Telegram API_HASH')
    return api_id, api_hash


def http_start():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/healthz':
                payload = b'{"ok":true}'
                mime = 'application/json'
            elif self.path == '/rss':
                payload = make_rss(read_json(FEED, []))
                mime = 'application/rss+xml; charset=utf-8'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *args):
            return
    server = ThreadingHTTPServer(('0.0.0.0',8766), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def keep_message(m):
    text = getattr(m, 'raw_text', None) or ''
    text = ' '.join(text.split())
    if not (24 <= len(text) <= 2000 and TOPICS.search(text)):
        return None
    when = m.date or dt.datetime.now(dt.timezone.utc)
    return {'id':int(m.id), 'text':text[:900], 'date':int(when.timestamp())}


async def login():
    from telethon import TelegramClient
    api_id, api_hash = credentials()
    STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    async with TelegramClient(SESSION,api_id,api_hash) as client:
        if not await client.is_user_authorized():
            await client.start()
        who = await client.get_me()
        if not who:
            raise RuntimeError('Telegram login failed')
        entity = await client.get_entity(GROUP)
        # Verify access before enabling the collector source.
        await client.get_messages(entity, limit=1)
        LOG.info('Telegram session authorized and group readable; session kept only on VM')


async def poll():
    from telethon import TelegramClient
    api_id, api_hash = credentials()
    STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE,0o700)
    http_start()
    async with TelegramClient(SESSION,api_id,api_hash) as client:
        if not await client.is_user_authorized():
            raise RuntimeError('Telegram session not authorized; run group login first')
        group = await client.get_entity(GROUP)
        last_id = int(read_json(OFFSET, {}).get('last_id',0))
        if not last_id:
            messages = await client.get_messages(group, limit=1)
            last_id = int(messages[0].id) if messages else 0
            atomic_json(OFFSET,{'last_id':last_id})
            # Baseline only. Don't send the historical chat backlog.
            LOG.info('group baseline set; latest=%d (historical messages skipped)',last_id)
        while True:
            try:
                incoming=[]
                async for msg in client.iter_messages(group,min_id=last_id,reverse=True,limit=400):
                    incoming.append(msg)
                if incoming:
                    old = read_json(FEED, [])
                    ids = {int(e['id']) for e in old}
                    accepted=0
                    for msg in incoming:
                        itm=keep_message(msg)
                        if itm and itm['id'] not in ids:
                            old.append(itm)
                            ids.add(itm['id'])
                            accepted+=1
                    # Feed persistence must happen before advancing offset.
                    atomic_json(FEED, sorted(old,key=lambda i:i['id'])[-250:])
                    last_id = max(int(m.id) for m in incoming)
                    atomic_json(OFFSET,{'last_id':last_id})
                    LOG.info('group read messages=%d retained_new=%d latest_id=%d',len(incoming),accepted,last_id)
                    if len(incoming)>=400:
                        LOG.warning('group backlog exceeded 400 messages, some messages may not have been seen')
            except Exception as e:
                LOG.warning('group reading interrupted: %s',type(e).__name__)
            await asyncio.sleep(20)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--login',action='store_true')
    args=p.parse_args()
    logging.basicConfig(level='INFO',format='%(asctime)sZ %(levelname)s %(message)s')
    asyncio.run(login() if args.login else poll())

if __name__=='__main__':
    main()
