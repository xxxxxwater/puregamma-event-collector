#!/usr/bin/env python3
"""Smoke test local en->zh translation + Telegram delivery; never inserts fake RSS."""
import os
import sqlite3
import sys

from main import telegram_send
from translation import Translator


def main():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    tr = Translator()
    if not tr.enabled:
        raise SystemExit('TRANSLATE_ENABLED is not true')
    tr.init_cache(db)
    sample = 'System translation test: Hello from the RSS collector.'
    zh = tr.translate(db, sample)
    if not any('\u4e00' <= char <= '\u9fff' for char in zh):
        raise SystemExit('Translate API did not produce Chinese')
    print('English to Chinese translation OK: ' + zh[:160])
    with open(os.environ.get('TELEGRAM_TOKEN_FILE', '/run/secrets/telegram_bot_token'), encoding='utf-8') as fd:
        token = fd.read().strip()
    message = ('🧪 <b>RSS 中文翻译系统测试（非新闻）</b>\n' +
               zh.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;') + '\n' +
               '英文原文：System translation test: Hello from the RSS collector.\n' +
               '中文标题推送链路已通过测试。')
    ok, msgid, delay, error = telegram_send(token, os.environ['TELEGRAM_CHAT_ID'], message)
    if not ok:
        raise SystemExit('Telegram test delivery failed: ' + str(error) + '. Retry after ' + str(delay))
    print('Chinese test notification delivered; Telegram message_id=' + str(msgid))
    print('NOTE: this test is NOT counted as a real RSS news item.')


if __name__ == '__main__':
    main()
