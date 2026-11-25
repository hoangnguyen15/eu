#!/usr/bin/env bash
# Full end-to-end run with the real .NET FlowAgent (Linux container build) and
# MockImageProvider:
#   Mobile API -> Scheduler -> Agent (mTLS WSS) -> signed upload -> Storage ->
#   REST result -> WebSocket notification.
# Prerequisites: .env (see .env.example), docker compose v2.
# Extra compose files (e.g. a build override) can be passed via COMPOSE_FILES.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f docker-compose.yml)
for f in ${COMPOSE_FILES:-}; do COMPOSE+=(-f "$f"); done

set -a; source .env; set +a
: "${ADMIN_EMAIL:?ADMIN_EMAIL missing in .env}" "${ADMIN_PASSWORD:?ADMIN_PASSWORD missing in .env}"
ACCOUNT_EMAIL="${AGENT_ACCOUNT_EMAIL:-mock-account@example.com}"

echo "==> starting core stack"
"${COMPOSE[@]}" up -d --build --wait

echo "==> bootstrap admin + authorized mock provider account"
"${COMPOSE[@]}" exec -T -e ADMIN_PASSWORD="$ADMIN_PASSWORD" backend python -m app.cli create-admin --email "$ADMIN_EMAIL"
"${COMPOSE[@]}" exec -T backend python -m app.cli add-account --email "$ACCOUNT_EMAIL" \
  --attest "E2E: MockImageProvider account, no external service involved"

if ! "${COMPOSE[@]}" --profile e2e ps --status running flow-agent 2>/dev/null | grep -q flow-agent; then
  echo "==> enrolling a FlowAgent with a single-use token"
  TOKEN=$("${COMPOSE[@]}" exec -T backend python -m app.cli enrollment-token --name e2e-agent | tr -d '\r')
  AGENT_ENROLLMENT_TOKEN="$TOKEN" "${COMPOSE[@]}" --profile e2e up -d --build flow-agent
fi

echo "==> running end-to-end checks"
docker run --rm --network host flow-backend:latest python scripts/e2e_client.py \
  --api "http://localhost:${API_PORT:-8000}" --admin-email "$ADMIN_EMAIL" --admin-password "$ADMIN_PASSWORD"
