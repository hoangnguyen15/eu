# Production Deployment

Topology: **Linux server** (Docker Compose) for the central stack, **Windows Server 2022** machines for FlowAgent workers. Workers only need outbound HTTPS to the gateway (`:8443`) and to object storage.

## 1. Central server (Linux)

### Prerequisites
* Docker Engine 24+ with Compose v2, 2 vCPU / 4 GB RAM minimum for ~10 workers.
* DNS names: `api.example.com` (mobile API) and `agents.example.com` (agent gateway).
* A TLS reverse proxy / load balancer in front of the API (`:8000`) — e.g. Caddy, Traefik, nginx, or a cloud LB. The agent gateway (`:8443`) terminates mTLS itself and must **not** be behind a TLS-terminating proxy (use TCP passthrough if a LB is needed).

### Configuration
```bash
git clone <repo> && cd <repo>
cp .env.example .env && chmod 600 .env
```
Set at minimum:

| Variable | Notes |
|---|---|
| `ENVIRONMENT=production` | disables `/docs` |
| `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `MINIO_ROOT_PASSWORD` | random, ≥ 32 chars |
| `JWT_SECRET` | random ≥ 32 chars; rotating it logs everyone out |
| `GATEWAY_SHARED_SECRET` | random ≥ 24 chars; shared only by nginx and backend |
| `GATEWAY_HOSTNAME=agents.example.com` | goes into the gateway certificate SAN |
| `CORS_ALLOW_ORIGINS` | empty for native mobile apps; list web origins explicitly otherwise |
| `TRUSTED_PROXY_COUNT=1` | when exactly one reverse proxy sets `X-Forwarded-For` |
| `S3_*` | see storage below |

Prefer a secret manager (Docker secrets, Vault, cloud KMS-backed env) over a plain `.env` file in production; nothing secret is committed to Git.

### Object storage
* **Cloudflare R2**: create a private bucket; API token with Object Read & Write on that bucket.
  ```
  S3_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
  S3_PUBLIC_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
  S3_REGION=auto
  S3_ACCESS_KEY_ID=... S3_SECRET_ACCESS_KEY=...
  S3_BUCKET=flow-assets
  S3_CREATE_BUCKET=false
  ```
  Then remove the `minio` service dependency (or keep MinIO for dev only).
* **MinIO** (bundled): set `S3_PUBLIC_ENDPOINT_URL` to the URL workers and phones use to reach MinIO (put TLS in front of it).
* Never enable public bucket policies. Enable versioning/replication on the bucket for backups.

### Start
```bash
docker compose up -d --build --wait
docker compose exec -e ADMIN_PASSWORD='<strong>' backend python -m app.cli create-admin --email ops@example.com
```
`migrate` runs `alembic upgrade head` before `backend`/`scheduler` start, on every deploy. `pki-init` creates the agent CA and gateway certificate once (volume `certs`).

**Protect the CA key** (`certs` volume, `ca.key`): it can mint agent identities. Back it up offline; consider moving signing to an HSM/KMS for larger fleets.

### Upgrades
```bash
git pull && docker compose build && docker compose up -d --wait
```
Migrations are forward-only in production; take a DB backup first (`scripts/backup-db.sh`). Run `alembic downgrade -1` only from a tested backup plan.

### Scaling
* `backend`: stateless (2 uvicorn workers per container by default). To run several replicas, stop publishing a fixed host port and put them behind your load balancer; agent and mobile WebSockets fan out through Redis Pub/Sub, so any replica can serve any client.
* `scheduler`: safe to run multiple replicas (`FOR UPDATE SKIP LOCKED`, partial unique indexes, advisory lock for maintenance).
* Workers: each worker reports `capacity` slots; the scheduler balances by load. Verified with 10 concurrent workers × 2 slots in the test suite.

### Backups
```bash
scripts/backup-db.sh /var/backups/flow       # pg_dump -Fc + sha256, 14-day retention
scripts/restore-db.sh /var/backups/flow/flow-<ts>.dump
```
Schedule daily (cron/systemd timer) and copy off-host. Images live in object storage — back up via bucket replication/versioning. Redis holds only transient queues/events (AOF enabled); the DB is the source of truth.

### Monitoring
* `GET /api/v1/health` (DB/Redis/storage), container healthchecks (scheduler writes a liveness file each tick).
* JSON logs on stdout (ship with your log driver).
* Watch: queued generations age, `/admin/dead-letters`, `/admin/generations?status=REVIEW_REQUIRED`, workstations `OFFLINE`.

## 2. Windows workers (Windows Server 2022)

### Build the package (on a build machine with .NET 8 SDK)
```powershell
cd windows-agent
.\publish.ps1            # runs tests, publishes self-contained win-x64 single file, zips + sha256
```

### Enroll and install (per machine, elevated PowerShell)
1. Create a single-use token: `POST /api/v1/admin/enrollment-tokens {"workstation_name":"WIN-01"}` (valid 24 h).
2. Register/authorize the provider account(s) this machine will host (admin API or `app.cli add-account`). Only an opaque `profile_ref` is configured on the worker; credentials never leave the machine and are never stored centrally.
3. Copy the zip and `ca.crt` (from the `certs` volume: `docker compose exec backend cat /certs/ca.crt`) to the worker, then:
```powershell
Expand-Archive FlowAgent-1.0.0-win-x64.zip C:\Temp\FlowAgent
C:\Temp\FlowAgent\install.ps1 -SourceDir C:\Temp\FlowAgent `
    -ServerBaseUrl https://agents.example.com:8443 -EnrollmentToken <token> `
    -CaCertificate C:\Temp\ca.crt -Capacity 2 -Providers mock `
    -Account "acct@example.com=profile-1=mock"
```
The installer registers service `FlowAgent` (virtual account `NT SERVICE\FlowAgent`, delayed auto-start, restart on failure) and restricts `C:\ProgramData\FlowAgent` (DPAPI-protected identity, SQLite outbox) to SYSTEM, Administrators and the service account. On first start the agent generates its key locally, enrolls, and connects.

Firewall: outbound TCP 8443 to the gateway and 443 to storage; no inbound ports.

### Operate
* Logs: Event Viewer → Application (source `FlowAgent`).
* Revoke a machine: `POST /api/v1/admin/workstations/{id}/disable` (effective immediately, even with a valid certificate).
* Uninstall: `uninstall.ps1 [-PurgeData]`.
* Upgrade: re-run `install.ps1` with the new package (keeps config and identity).

> Not verified in this repository's automated runs: the agent was cross-compiled for win-x64 and exercised end-to-end as a Linux container; Windows-specific paths (Windows Service host, DPAPI, `NT SERVICE` account ACLs, SChannel client certificates) still need a smoke test on a Windows Server 2022 machine before rollout.

## 3. Security checklist
- [ ] All `CHANGE_ME` secrets replaced; `.env` mode 600, not in Git
- [ ] API behind TLS; HSTS at the proxy; `ENVIRONMENT=production`
- [ ] Gateway `:8443` reachable only from worker networks if possible; CA key backed up offline
- [ ] Bucket private; `S3_PUBLIC_ENDPOINT_URL` uses HTTPS
- [ ] `CORS_ALLOW_ORIGINS` minimal; rate limits tuned
- [ ] Daily DB backups tested with a restore
- [ ] Provider accounts registered only with a written authorization (`attestation`), Google Flow connector left disabled unless usage rights and a Google-permitted integration method are confirmed
