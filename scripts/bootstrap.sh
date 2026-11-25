#!/usr/bin/env bash
# Starts the stack and creates the admin account from .env (idempotent).
set -euo pipefail
cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f docker-compose.yml)
for f in ${COMPOSE_FILES:-}; do COMPOSE+=(-f "$f"); done
set -a; source .env; set +a
"${COMPOSE[@]}" up -d --build --wait
# the gateway must reload if its certificate was (re)issued for a new PUBLIC_HOST
"${COMPOSE[@]}" restart agent-gateway >/dev/null
"${COMPOSE[@]}" exec -T -e ADMIN_PASSWORD="$ADMIN_PASSWORD" backend python -m app.cli create-admin --email "$ADMIN_EMAIL"
H="${PUBLIC_HOST:-localhost}"
cat <<MSG

Ready.
  Web client (mobile demo): http://${H}:${API_PORT:-8000}/app/
  Admin console:            http://${H}:${API_PORT:-8000}/admin/    (${ADMIN_EMAIL})
  API docs:                 http://${H}:${API_PORT:-8000}/docs      (ENVIRONMENT=development only)
  Agent gateway (mTLS):     https://${H}:${GATEWAY_PORT:-8443}
MSG
