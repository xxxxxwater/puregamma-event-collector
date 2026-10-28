# V4 — Telegram automatic English → Simplified Chinese translation

This is an additive patch to an existing PureGamma RSS & Event Collector on
`instabox-pg-test`; it is NOT a fresh deployment and it does not change feed
sources, article/event deduplication, priorities, Telegram credentials or group
reader authorization. It is compatible with the original V1 and V3 deployments.

## Features

- Every English RSS title sent via the existing Telegram outbox is translated
  locally to Simplified Chinese using LibreTranslate/Argos (`en` → `zh`).
- Telegram message has Chinese headline, English original, original source URL,
  Chinese category label, priority and timestamp. P2 digest titles are also
  translated. Already-Chinese titles are not retranslated.
- Initial historical articles stay silent; old delivered Telegram messages are
  unchanged. Translation is done **after** ingestion, dedupe, classification and
  queuing. Translation results are cached in the same SQLite DB.
- If translation is unavailable, English messages remain pending for retry. A
  system without a ready model cannot claim Chinese delivery success. Chinese
  translation may be imperfect for names/tickers; compare linked original.
- No news data is submitted to third-party hosted translation APIs: the model
  runs in its own container on InstaBox. First start must download the model.

## Resources

- LibreTranslate `v1.9.6`: capped at 1.25 vCPU, 2560 MiB memory.
- Existing RSSHub/collector remain unchanged in capacity.
- Translation service has no host/public port; accessible only over Docker
  network `events`; only English and Chinese models loaded.
- Model storage is a persistent Docker named volume `libretranslate_models`.
  Ensure available disk space for Docker image and translation models.

## One-command in-place upgrade (run on user's Mac)

Unzip the **V4 archive** to Downloads and run:

```sh
cd ~/Downloads/puregamma-event-collector-v4
sh scripts/upgrade_translation_over_ssh.sh
```

Supply `ibssh_...@ssh.instabox.sh` interactively (not stored in project).
The script verifies hostname `instabox-pg-test`, snapshots the SQLite DB online
(`data/zh-preupgrade-backup-<timestamp>.sqlite`), copies only the four source/Compose
translation-related files, and recreates collector while adding LibreTranslate.
It does **not** overwrite secrets, `.env`, `config/sources.json`, existing group
reader files, or clear old history.

It sends one clearly labeled Chinese **translation TEST** message via existing
Bot to confirm the translate → format → Telegram path. This is not counted as
real RSS news. New incoming English RSS events are translated when queued.

## Verify later

```sh
ssh -p 2222 USER@ssh.instabox.sh \
  'cd "$HOME/puregamma-event-collector" && docker compose ps && docker compose logs --tail=30 collector'
```

If the model is still downloading, events are retained in the SQLite outbox
until translation is available. If you want to temporarily disable translation
(for example, under resource pressure), edit `compose.override.yaml` and set
`TRANSLATE_ENABLED: 'false'`; then `docker compose up -d --no-deps collector`.
This allows English notifications again, so the Chinese-only guarantee stops.

**Note:** InstaBox's idle auto-pause must be disabled separately using
`instabox auto-suspend instabox-pg-test 0` from an authenticated InstaBox CLI
with manual-lifecycle permission. A Docker restart policy cannot override
server suspension.
