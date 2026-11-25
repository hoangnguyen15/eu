#!/usr/bin/env bash
# Logical PostgreSQL backup (custom format, compressed) from the compose stack.
#   scripts/backup-db.sh [output-dir]     restore: scripts/restore-db.sh <file>
# Schedule it (cron/systemd timer) and ship the files off-host; object storage
# (images) must be backed up separately via bucket replication/versioning.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
OUT="${1:-./backups}"
mkdir -p "$OUT"
FILE="$OUT/flow-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T postgres pg_dump -U "${POSTGRES_USER:-flow}" -d "${POSTGRES_DB:-flow}" -Fc > "$FILE"
sha256sum "$FILE" > "$FILE.sha256"
find "$OUT" -name 'flow-*.dump*' -mtime +"${BACKUP_RETENTION_DAYS:-14}" -delete
echo "backup written: $FILE"
