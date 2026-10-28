#!/bin/sh
# Run INSIDE the VM, only after you personally join @WatcherGuruChat.
# API_ID/API_HASH + OTP/2FA are provided on VM TTY, never in ChatGPT or git.
set -eu
cd "$(dirname "$0")/.."
[ -t 0 ] || { echo 'Interactive TTY required; use ssh -tt'; exit 2; }
command -v docker >/dev/null || exit 1
umask 077
mkdir -p secrets data/group
chmod 700 secrets data/group
if [ ! -s secrets/telegram_api_id ]; then
  printf 'Telegram API_ID (from my.telegram.org): '
  IFS= read -r api_id
  case "$api_id" in *[!0-9]*|'') echo 'Invalid API_ID'; exit 2;; esac
  printf %s "$api_id" > secrets/telegram_api_id
fi
if [ ! -s secrets/telegram_api_hash ]; then
  printf 'Telegram API_HASH (hidden): '
  stty -echo
  IFS= read -r api_hash
  stty echo
  printf '\n'
  if [ -z "$api_hash" ]; then echo 'Empty API_HASH'; exit 2; fi
  printf %s "$api_hash" > secrets/telegram_api_hash
fi
chmod 600 secrets/telegram_api_id secrets/telegram_api_hash
printf '\n[1/3] Building isolated read-only group adapter ...\n'
docker compose --profile mtproto build group-reader
printf '\n[2/3] Log in with Telegram phone + OTP + 2FA on THIS VM ...\n'
docker compose --profile mtproto run --rm --no-deps group-reader python /app/group_reader.py --login
printf '\n[3/3] Enable read-only MTProto source ...\n'
docker compose --profile mtproto up -d --no-deps group-reader
# First give reader a chance to baseline group history.
sleep 5
python3 - <<'PY'
import json, os
p='config/sources.json'
with open(p,encoding='utf-8') as f: data=json.load(f)
for source in data['sources']:
    if source['id']=='watcherguru_chat':
        if source['url']!='http://group-reader:8766/rss':
            raise SystemExit('Incorrect group adapter URL; refusing to enable')
        source['enabled']=True
new=p+'.tmp'
with open(new,'w',encoding='utf-8') as f:
    json.dump(data,f,ensure_ascii=False,indent=2)
    f.write('\n')
os.replace(new,p)
PY
docker compose up -d --no-deps --force-recreate collector
printf '\nGroup status (never expose Telegram session contents):\n'
docker compose --profile mtproto ps
printf '\nGroup logs:\n'
docker compose --profile mtproto logs --tail=8 group-reader
printf '\nCollector source status:\n'
curl -fsS --max-time 3 http://127.0.0.1:8765/status || true
printf '\n'
