#!/usr/bin/env bash
# Restore a backup created by backup-db.sh into the compose Postgres.
# DESTRUCTIVE: drops existing objects. Stop backend + scheduler first:
#   docker compose stop backend scheduler && scripts/restore-db.sh backups/flow-...dump
set -euo pipefail
cd "$(dirname "$0")/.."
FILE="${1:?usage: restore-db.sh <dump-file>}"
set -a; source .env; set +a
if [ -f "$FILE.sha256" ]; then sha256sum -c "$FILE.sha256"; fi
read -r -p "Restore $FILE into database ${POSTGRES_DB:-flow}? This overwrites data. [y/N] " ok
[ "$ok" = "y" ] || { echo "aborted"; exit 1; }
docker compose exec -T postgres pg_restore -U "${POSTGRES_USER:-flow}" -d "${POSTGRES_DB:-flow}" \
  --clean --if-exists --no-owner < "$FILE"
echo "restore complete; run 'docker compose up -d' to restart services"
