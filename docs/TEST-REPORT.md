# QA / Verification Report

Date: 2026-11-25 (round 1 + round 3) · Environment: Linux container, Docker 29.9, Compose v5.7, Python 3.12, .NET SDK 8.0, PostgreSQL 16, Redis 7, MinIO (pgsty build).

All results below were produced by actually running the commands; nothing is simulated except the image provider itself (`MockImageProvider`, by design).

## 1. Repository state found

The repository initially contained only `eu.txt`; the platform was built from scratch in this branch.

## 2. Results

| Suite | Command | Result |
|---|---|---|
| Migrations | `alembic upgrade head` → `downgrade base` → `upgrade head` → `alembic check` | clean, no drift |
| Backend lint | `ruff check` | pass |
| Backend unit + integration (Docker) | `docker compose --profile test run --rm tests` | **55 passed** (3 consecutive runs stable; also passes on the host) |
| Agent unit tests (.NET) | `dotnet test FlowAgent.Tests` (also runs inside the agent image build) | **10 passed** |
| Agent cross-publish | `dotnet publish -r win-x64 --self-contained -p:PublishSingleFile=true` | `FlowAgent.exe` produced |
| PowerShell scripts | PowerShell 7 parser on `install.ps1`, `uninstall.ps1`, `publish.ps1` | 0 syntax errors |
| End-to-end, real .NET agent, fresh volumes | `scripts/e2e.sh` | **PASS 28/28** (2 images); separate run with 3 images **33/33** |
| Failure injection, real .NET agent | `scripts/resilience.sh` | **PASS 7/7** |
| mTLS negative checks via nginx gateway | curl | rogue CA client cert → 400; spoofed `X-Client-*` headers without cert → 403; backend agent endpoint without gateway secret → 403 |

### End-to-end path verified
Mobile `POST /generations` (Idempotency-Key retry returns the same job) → reserve credit → Redis Stream wake-up → scheduler lease → `TASK_OFFER` over Redis Pub/Sub → **.NET FlowAgent** (enrolled with single-use token + CSR, connected through the nginx mTLS gateway) → `TASK_ACCEPT/STARTED` → MockImageProvider PNGs → `UPLOAD_REQUEST` → PUT to MinIO signed URLs → `UPLOAD_COMPLETED` → server download + MIME/size/SHA-256/decoder verification + WebP thumbnail → `TASK_COMPLETED` → `COMPLETED` + settle → REST result with signed URLs (SHA-256 of downloaded bytes matches) → WebSocket `generation.queued/started/progress/completed` + `credits.updated`. Private bucket (unsigned URL → 403), tenant isolation (404 for another user).

### Integration test coverage (backend/tests)
Registration/login/validation, JWT refresh rotation + reuse detection, logout, create generation, idempotency (sequential, 8 concurrent retries, conflicting body), credit reservation, concurrent over-spend prevention, refund on cancel/failure, REVIEW_REQUIRED keeps credits until admin resolve, enrollment single-use, agent auth (no secret / failed verify / unpinned cert / disabled workstation), register + heartbeat + session status, duplicate event dedup, scheduler dispatch across workers without double dispatch, **10 workers × 40 jobs**, offer expiry requeue to another worker, worker lost mid-task → FAILED `WORKER_LOST` without retry + OFFLINE detection, `NOT_STARTED` requeue, `OUTCOME_UNKNOWN` → review, upload corruption → `ASSET_VERIFICATION_FAILED`, `TASK_COMPLETED` rejected before verification, foreign agent cannot touch another agent's task, cancel of a dispatched offer → `TASK_CANCEL`, provider failure mapping (`failed`, `auth_required`, `provider_limited` with account marked LIMITED and no reassignment, `review_required`), WebSocket auth / ping-pong / replay with `?since=`, user isolation, pagination, admin RBAC + audit log, rate limiter, request size limit, image validation, PKI, Google Flow connector disabled.

## 3. Defects found and fixed during verification

| # | Defect | Found by | Fix |
|---|---|---|---|
| 1 | Agent hung forever on a half-open WebSocket after an abrupt gateway crash (.NET 8 `KeepAliveInterval` sends pings but never times out); in-flight job waited for an ack that could not arrive | `resilience.sh` (gateway `kill`) | Liveness watchdog in `Worker.ReadLoopAsync`: abort + reconnect when the server is silent for 2×heartbeat+5 s |
| 2 | Job cancellation token was tied to the *connection*, so any disconnect cancelled running jobs (→ falsely `OUTCOME_UNKNOWN` after restart) | `resilience.sh` after fix #1 | Jobs use the service lifetime token |
| 3 | First version of the outage test used `docker compose stop` (graceful drain) and therefore did not really test offline buffering | review of agent logs | Test uses `kill` and asserts from the DB that `UPLOAD_REQUEST` arrived after the gateway was restored |
| 4 | Rate limiter docstring claimed fail-open but Redis errors produced 500 | documentation review | Catch `RedisError`, log, allow |
| 5 | Integration tests leaked QUEUED jobs between modules (flaky assertions when run as one suite) | full-suite run in Docker | Autouse isolation fixture + assertions scoped to the test's own jobs |
| 6 | E2E checker bug (async generator in `all()`) | first `e2e.sh` run | fixed |

## 4. Not verified here (needs a real Windows Server 2022 host)
* Running `FlowAgent.exe` as a Windows Service, `install.ps1`/`uninstall.ps1` execution, `NT SERVICE\FlowAgent` ACLs, Event Log source.
* DPAPI-protected identity and SChannel client-certificate authentication (Linux path uses a 0600 PKCS#12 file).
* `ApprovedImageProvider` against a real licensed API (only the disabled/unconfigured path is tested).
* GitHub Actions workflow (`.github/workflows/ci.yml`) has not been executed on GitHub.

## 5. Environment notes
* Official MinIO images/binaries were not obtainable (Docker Hub denied, `dl.min.io` 410); the stack uses `pgsty/minio` pinned by digest (override with `MINIO_IMAGE`).
* Builds in this sandbox used a local compose override (host network + proxy CA as a BuildKit secret); the committed Dockerfiles support the optional `extra_ca` secret for corporate proxies and otherwise build normally.

---

# Round 3 — web UIs, installer, macOS-style setup, optimisation

All suites re-run on the final code, starting from a **fresh install** with the macOS/LAN configuration (`init-env.sh --host <LAN IP>` → `PUBLIC_HOST=<LAN IP>`, `AGENT_NETWORK_MODE=bridge`, all URLs/certificates via the LAN IP).

| Suite | Result |
|---|---|
| Backend unit + integration (Docker `--profile test`) | **59 passed** (+4 admin console / web UI tests) |
| Agent xUnit (inside `package-agent.sh` and the agent image build) | **14 passed** |
| `scripts/e2e.sh` (LAN IP, agent in bridge network) | **PASS 28/28** |
| `scripts/resilience.sh` | **PASS 7/7** |
| `scripts/ui-check.js` (Chromium, iPhone 13 viewport + desktop admin) | **PASS 16/16**: register/login, realtime, job completes, thumbnails + full image via signed URLs, failure + refund, 5-job load test from the phone UI, admin overview/workstations/sessions/session-check/jobs/provisioning, no CSP violations, no console errors |
| Installer package | `dist/FlowAgent-1.1.0-win-x64.zip` (31 MB: self-contained `FlowAgent.exe` PE, `Install-FlowAgent.cmd` CRLF, scripts, Vietnamese README) |
| `install.ps1 -DryRun` with the config downloaded from the admin console | valid; an invalid config (`http://`) is rejected with a clear message |
| Admin-console provisioning → real agent | a 2nd .NET agent started from the downloaded `flowagent-config.json` enrolled and went ONLINE |
| Gateway certificate | re-issued for the LAN IP (SAN `IP:192.0.2.2`) with the CA unchanged |

## Performance (Mock provider = 0.8 s fixed latency, 1 image/job)

Measured with `backend/scripts/bench.py` (WebSocket timestamps, client side).

| Scenario | Before | After |
|---|---|---|
| Single job, end-to-end | 1.16–1.25 s | **1.00–1.07 s** (≈0.2 s overhead) |
| 20 jobs burst, 1 agent × 2 slots: throughput | 0.96 jobs/s | **1.97 jobs/s** (79 % of the 2.5/s ceiling) |
| same: p95 end-to-end | 19.8 s | **10.0 s** |
| same: `POST /generations` p50 under burst | 533 ms | **196 ms** |
| 28 jobs, 2 agents × 2 slots (account cap raised to 4 for the measurement) | – | **3.7 jobs/s** (near-linear scaling) |

Root causes found from per-stage DB timestamps and fixed:
1. ~1.0 s idle gap per slot between jobs → the scheduler is now woken through the Redis stream whenever a lease ends or a worker registers.
2. Provider completion detected ~250 ms late → agent polling 50 ms for the first 2 s, then backs off to 1 s.
3. Uploads and server-side verification were sequential per image → now parallel.
4. Credit reservation used SELECT … FOR UPDATE + UPDATE → a single conditional UPDATE (shorter lock hold under bursts).

With one shared provider account, throughput stays at ~2 jobs/s because of `MAX_CONCURRENT_JOBS_PER_ACCOUNT=2`, which is intended: provider limits are respected rather than worked around. Scale by adding authorized accounts/workstations.

## Defects found and fixed in round 3
| # | Defect | Fix |
|---|---|---|
| 7 | `install.ps1 -DryRun` failed off-Windows (`Join-Path` on `C:\`) | `[IO.Path]::Combine` |
| 8 | Config file missing a property → confusing StrictMode error instead of a validation message | safe property access, explicit validation |
| 9 | Admin "recent jobs" table squeezed to 1-character columns; long e-mail broke the mobile top bar | layout/ellipsis fixes (verified on screenshots) |
| 10 | UI check assumed the first workstation row is online | checks any row |

## Environment-specific observation (not a product defect)
Chromium in this sandbox cannot open WebSockets to the LAN IP (HTTP 502 from the sandbox network layer; the backend never receives the request), while a direct client (`websockets`) to `ws://<LAN IP>:8000` connects fine and Chromium works via `localhost`. The web client correctly fell back to REST polling ("Mất kết nối (REST)") in that situation. UI checks therefore ran against `localhost`, with images still loaded from the LAN-IP storage URLs.

## Still not verified
* Windows Server 2022 runtime (service, DPAPI, SChannel client cert, real `install.ps1` execution).
* A physical Mac (the macOS procedure was exercised with the same LAN-IP/bridge configuration on Linux; all images used are multi-arch incl. `linux/arm64`).
* Cloudflare R2 and a real licensed provider API.
