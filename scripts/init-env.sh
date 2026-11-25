#!/usr/bin/env bash
# Creates .env from .env.example with random secrets and this machine's LAN
# address. Works on macOS (bash 3.2) and Linux.
#   scripts/init-env.sh [--host 192.168.1.10] [--admin-email you@example.com] [--force]
#   scripts/init-env.sh --set-host 192.168.1.20   # only change PUBLIC_HOST, keep all secrets
set -euo pipefail
cd "$(dirname "$0")/.."

HOST=""; ADMIN_EMAIL="admin@example.com"; FORCE=0; SET_HOST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --admin-email) ADMIN_EMAIL="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --set-host) SET_HOST="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

if [ -n "$SET_HOST" ]; then
  [ -f .env ] || { echo ".env not found; run without --set-host first" >&2; exit 1; }
  cp .env ".env.bak.$(date +%Y%m%d%H%M%S)"
  MODE="bridge"; [ "$SET_HOST" = "localhost" ] && MODE="host"
  sed -e "s/^PUBLIC_HOST=.*/PUBLIC_HOST=${SET_HOST}/" -e "s/^AGENT_NETWORK_MODE=.*/AGENT_NETWORK_MODE=${MODE}/" \
      .env > .env.tmp && mv .env.tmp .env && chmod 600 .env
  echo "PUBLIC_HOST=${SET_HOST} (secrets unchanged). Now run scripts/bootstrap.sh to re-issue the gateway certificate."
  exit 0
fi

if [ -f .env ] && [ "$FORCE" -ne 1 ]; then
  echo ".env already exists (use --force to regenerate; the old file is backed up)"; exit 1
fi
[ -f .env ] && cp .env ".env.bak.$(date +%Y%m%d%H%M%S)"

if [ -z "$HOST" ]; then
  if [ "$(uname -s)" = "Darwin" ]; then
    HOST="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
  else
    HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi
  HOST="${HOST:-localhost}"
fi
MODE="bridge"; [ "$HOST" = "localhost" ] && MODE="host"

rand() { openssl rand -hex 24; }
ADMIN_PASSWORD="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)"

sed -e "s/^PUBLIC_HOST=.*/PUBLIC_HOST=${HOST}/" \
    -e "s/^AGENT_NETWORK_MODE=.*/AGENT_NETWORK_MODE=${MODE}/" \
    -e "s/^ADMIN_EMAIL=.*/ADMIN_EMAIL=${ADMIN_EMAIL}/" \
    -e "s/CHANGE_ME_admin_password/${ADMIN_PASSWORD}/" \
    -e "s/CHANGE_ME_postgres_password/$(rand)/" \
    -e "s/CHANGE_ME_redis_password/$(rand)/" \
    -e "s/CHANGE_ME_at_least_32_random_characters_long/$(rand)/" \
    -e "s/CHANGE_ME_at_least_24_random_characters/$(rand)/" \
    -e "s/CHANGE_ME_minio_password/$(rand)/" \
    .env.example > .env
chmod 600 .env
echo "Wrote .env  (PUBLIC_HOST=${HOST}, AGENT_NETWORK_MODE=${MODE})"
echo "Admin login: ${ADMIN_EMAIL} / ${ADMIN_PASSWORD}   (stored in .env as ADMIN_PASSWORD)"
