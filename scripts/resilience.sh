#!/usr/bin/env bash
# Failure-injection checks against the running stack with the real FlowAgent:
#  1. gateway outage while a job runs -> agent keeps the job, buffers events in
#     its SQLite outbox, reconnects, replays, job COMPLETES
#  2. agent restart (graceful) -> reuses its enrolled identity, keeps working
#  3. agent SIGKILL while the provider call is in flight -> on restart it reports
#     OUTCOME_UNKNOWN; job goes to REVIEW_REQUIRED and is NOT retried
# Run scripts/e2e.sh first.
set -euo pipefail
cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f docker-compose.yml)
for f in ${COMPOSE_FILES:-}; do COMPOSE+=(-f "$f"); done
set -a; source .env; set +a
API="http://localhost:${API_PORT:-8000}"
fail=0

json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
pass() { echo "[PASS] $*"; }
bad() { echo "[FAIL] $*"; fail=1; }

EMAIL="res-$RANDOM$RANDOM@example.com"
curl -fsS -XPOST "$API/api/v1/auth/register" -H 'content-type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"resilience-pass-1\"}" >/dev/null
TOKEN=$(curl -fsS -XPOST "$API/api/v1/auth/login" -H 'content-type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"resilience-pass-1\"}" | json 'd["data"]["access_token"]')
AUTH=(-H "Authorization: Bearer $TOKEN")

create() {  # $1 = prompt
  curl -fsS -XPOST "$API/api/v1/generations" "${AUTH[@]}" -H 'content-type: application/json' \
    -d "{\"prompt\":\"$1\",\"aspect_ratio\":\"1:1\",\"number_of_images\":1}" | json 'd["data"]["generation_id"]'
}
status() { curl -fsS "$API/api/v1/generations/$1" "${AUTH[@]}" | json 'd["data"]["status"]+" "+str(d["data"]["error_code"])'; }
wait_status() {  # $1 id, $2 regex, $3 timeout
  local end=$((SECONDS + ${3:-90})) s
  while [ $SECONDS -lt $end ]; do
    s=$(status "$1"); [[ "$s" =~ $2 ]] && { echo "$s"; return 0; }; sleep 1
  done
  echo "$s"; return 1
}
registrations() { "${COMPOSE[@]}" --profile e2e logs flow-agent 2>/dev/null | grep -c "Registered as workstation" || true; }
wait_agent_online() {  # $1 = registration count before the restart
  local end=$((SECONDS + 90))
  while [ $SECONDS -lt $end ]; do [ "$(registrations)" -gt "$1" ] && return 0; sleep 1; done; return 1
}

echo "== 1. gateway outage during a running job"
PSQL=("${COMPOSE[@]}" exec -T postgres psql -U "${POSTGRES_USER:-flow}" -d "${POSTGRES_DB:-flow}" -tAc)
G=$(create "outage test [mock:slow]")
wait_status "$G" '^running' 30 >/dev/null
"${COMPOSE[@]}" kill agent-gateway >/dev/null 2>&1      # hard cut, no graceful drain
sleep 10   # provider (~6s) finishes while offline; events wait in the agent's SQLite outbox
s=$(status "$G"); [[ "$s" =~ ^running ]] && pass "server still sees RUNNING during outage" || bad "during outage: $s"
RESTORED=$("${PSQL[@]}" "select now()")
"${COMPOSE[@]}" start agent-gateway >/dev/null 2>&1
if s=$(wait_status "$G" '^completed' 120); then pass "job completed after reconnect ($s)"; else bad "outage job: $s"; fi
LATE=$("${PSQL[@]}" "select count(*) from worker_events e join task_leases l on l.id::text = e.payload->>'task_id'
  where l.generation_id='$G' and e.event_type='UPLOAD_REQUEST' and e.created_at > '$RESTORED'")
[ "$LATE" = "1" ] && pass "upload request was replayed from the outbox after reconnect" || bad "late uploads=$LATE"

echo "== 2. graceful agent restart reuses enrolled identity"
BEFORE=$(registrations)
"${COMPOSE[@]}" --profile e2e restart flow-agent >/dev/null 2>&1
wait_agent_online "$BEFORE" && pass "agent re-registered with stored certificate" || bad "agent did not come back"
G=$(create "after restart")
if s=$(wait_status "$G" '^completed' 60); then pass "job after restart completed ($s)"; else bad "after restart: $s"; fi

echo "== 3. SIGKILL while the provider call is in flight"
G=$(create "kill test [mock:slow]")
wait_status "$G" '^running' 30 >/dev/null
sleep 1
"${COMPOSE[@]}" --profile e2e kill -s SIGKILL flow-agent >/dev/null 2>&1
"${COMPOSE[@]}" --profile e2e start flow-agent >/dev/null 2>&1
if s=$(wait_status "$G" '^review_required OUTCOME_UNKNOWN' 90); then
  pass "unknown outcome surfaced, not retried ($s)"
else bad "kill test: $s"; fi
OFFERS=$("${PSQL[@]}" "select count(*) from task_leases where generation_id='$G'")
[ "$OFFERS" = "1" ] && pass "exactly one lease for the killed job (no automatic retry)" || bad "leases=$OFFERS"

echo
[ $fail -eq 0 ] && echo "RESILIENCE RESULT: PASS" || echo "RESILIENCE RESULT: FAIL"
exit $fail
