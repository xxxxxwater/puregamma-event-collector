"""Optional local English->Simplified Chinese translation for Telegram delivery.

Never translates raw feed data before ingest/classification/deduplication.
Translation failures are fail-closed for Telegram: caller retries queued messages.
No user secrets are sent to the translation endpoint.
"""
import hashlib
import json
import os
import re
import time
import urllib.request


class TranslationError(Exception):
    """Temporary translation problem: never print arbitrary remote response data."""


def english_text(text):
    # Titles with sufficient English words are translated, even if also containing Chinese.
    text = text or ""
    # Chinese stories often contain tickers like BTC/ETF: do not translate
    # a mostly Chinese headline just because it has an English abbreviation.
    if len(re.findall(r"[\u3400-\u9fff]", text)) >= 2:
        return False
    return bool(re.search(r"[A-Za-z]{3,}", text))


class Translator:
    def __init__(self, enabled=None, endpoint=None, timeout=None):
        self.enabled = (enabled if enabled is not None else os.environ.get("TRANSLATE_ENABLED", "false").lower() == "true")
        self.endpoint = endpoint or os.environ.get("TRANSLATE_ENDPOINT", "http://libretranslate:5000/translate")
        self.timeout = int(timeout or os.environ.get("TRANSLATE_TIMEOUT_SECONDS", "25"))

    def init_cache(self, con):
        con.execute("""CREATE TABLE IF NOT EXISTS translation_cache (
            input_hash TEXT PRIMARY KEY, text_zh TEXT NOT NULL,
            translated_at INTEGER NOT NULL
        )""")
        con.commit()

    def translate(self, con, text):
        text = (text or "").strip()
        if not self.enabled or not english_text(text):
            return text
        key = hashlib.sha256(("en-zh-v1|" + text).encode("utf-8")).hexdigest()
        row = con.execute("SELECT text_zh FROM translation_cache WHERE input_hash=?", (key,)).fetchone()
        if row is not None:
            return row["text_zh"]
        payload = json.dumps({"q": text[:700], "source": "en", "target": "zh", "format": "text"}).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=payload,
                 headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                if response.status != 200:
                    raise TranslationError("translation status not 200")
                obj = json.load(response)
            zh = obj.get("translatedText", "")
            if not isinstance(zh, str) or not zh.strip():
                raise TranslationError("empty translation result")
            zh = zh.strip()[:750]
            # A Chinese sentence is required for meaningful English titles, but
            # short ticker-only strings are allowed unchanged.
            if len(text.split()) >= 3 and not re.search(r"[\u3400-\u9fff]", zh):
                raise TranslationError("translation contains no Chinese characters")
        except TranslationError:
            raise
        except Exception:
            # Never log URL, server body, response headers, or secret tokens.
            raise TranslationError("local translation endpoint unavailable") from None
        con.execute("INSERT OR IGNORE INTO translation_cache(input_hash,text_zh,translated_at) VALUES (?,?,?)",
                    (key, zh, int(time.time())))
        con.commit()
        return zh
