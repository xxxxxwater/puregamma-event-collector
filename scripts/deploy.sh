#!/bin/sh
# Run from project root on the intended Instabox host.
set -eu
cd "$(dirname "$0")/.."
command -v docker >/dev/null || { echo 'ERROR: Docker not installed'; exit 1; }
docker compose version >/dev/null || { echo 'ERROR: Docker Compose plugin missing'; exit 1; }
if [ ! -f .env ]; then cp .env.example .env; chmod 600 .env; fi
umask 077
mkdir -p secrets data
if [ ! -s secrets/telegram_bot_token ]; then
  printf 'Telegram Bot Token (input hidden): '
  stty -echo
  IFS= read -r bot_token
  stty echo
  printf '\n'
  test -n "$bot_token" || { echo 'ERROR: bot token empty'; exit 1; }
  printf '%s' "$bot_token" > secrets/telegram_bot_token
  unset bot_token
fi
chmod 600 secrets/telegram_bot_token
printf '\nDisk before deploy:\n'
df -h .
printf '\nValidating compose configuration...\n'
docker compose config -q
printf '\nStarting stack...\n'
docker compose up -d
printf '\nContainer status:\n'
docker compose ps
printf '\nLocal health endpoint (may take a minute to become ready):\n'
for count in 1 2 3 4 5 6; do
  if curl -fsS --max-time 3 http://127.0.0.1:8765/status; then printf '\n'; break; fi
  sleep 5
done
printf '\nRecent logs (secrets redacted by application):\n'
docker compose logs --tail=20 collector
