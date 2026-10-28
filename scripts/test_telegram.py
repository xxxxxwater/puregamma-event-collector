#!/usr/bin/env python3
"""Send one explicit test message from server using Docker secret, no token printing."""
import os, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))
from main import telegram_send

base = pathlib.Path(__file__).resolve().parents[1]
chat_id = os.environ.get('TELEGRAM_CHAT_ID')
if not chat_id:
    for line in (base / '.env').read_text().splitlines():
        if line.startswith('TELEGRAM_CHAT_ID='):
            chat_id = line.split('=', 1)[1].strip()
if not chat_id:
    raise SystemExit('Missing TELEGRAM_CHAT_ID')
token = (base / 'secrets' / 'telegram_bot_token').read_text().strip()
ok, mid, wait, err = telegram_send(token, chat_id, '✅ PureGamma RSS Event Collector: Telegram 投递测试成功。')
if ok:
    print('Telegram message delivered; message_id:', mid)
else:
    raise SystemExit('Telegram test failed: %s, retry_after=%s' % (err, wait))
