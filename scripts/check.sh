#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
printf 'Health/status:\n'
curl -fsS http://127.0.0.1:8765/status
printf '\nCompose containers:\n'
docker compose ps
printf '\nSource errors (if any):\n'
docker compose logs --tail=150 collector | grep -E 'failure=|telegram delivery failed' || true
