"""Static, private internal RSS feed generated from authorized Telegram group messages.

This module has no dependencies; the network-side reader is in group_reader.py.
"""
import datetime as dt
import json
import os
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


def atomic_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.tmp-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fp:
            json.dump(obj, fp, ensure_ascii=False)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path, default):
    try:
        with open(path, encoding='utf-8') as fp:
            return json.load(fp)
    except (OSError, ValueError, TypeError):
        return default


def make_rss(items):
    rss = ET.Element('rss', attrib={'version':'2.0'})
    channel = ET.SubElement(rss, 'channel')
    ET.SubElement(channel, 'title').text = 'WatcherGuruChat authorized public group'
    ET.SubElement(channel, 'link').text = 'https://t.me/WatcherGuruChat'
    ET.SubElement(channel, 'description').text = 'Unverified public discussion, NOT official news'
    for it in reversed(items[-250:]):
        message = ET.SubElement(channel, 'item')
        ET.SubElement(message, 'guid', attrib={'isPermaLink':'false'}).text = f"watcherguruchat:{it['id']}"
        ET.SubElement(message, 'title').text = it['text'][:280].replace('\n', ' ')
        ET.SubElement(message, 'link').text = f"https://t.me/WatcherGuruChat/{it['id']}"
        ET.SubElement(message, 'description').text = it['text'][:800]
        ET.SubElement(message, 'pubDate').text = dt.datetime.fromtimestamp(it['date'], tz=dt.timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
    return ET.tostring(rss, encoding='utf-8', xml_declaration=True)
