# API Reference

Base URL: `https://<api-host>/api/v1`. Machine-readable schema: [`openapi.json`](openapi.json)
(also served at `/api/v1/openapi.json`; Swagger UI at `/docs` when `ENVIRONMENT != production`).

## Conventions

* Success: `{"success": true, "data": ...}`
* Error: `{"success": false, "error": {"code": "...", "message": "...", "details": ...}}`
* Auth: `Authorization: Bearer <access_token>` (JWT HS256, 15 min). Refresh tokens are opaque, rotated on every use; re-using a rotated token revokes the whole token family (`REFRESH_TOKEN_REUSED`).
* Statuses in responses are lowercase (`queued`, `completed`, …).
* Request body limit: `MAX_REQUEST_BYTES` (default 1 MiB) → `413 PAYLOAD_TOO_LARGE`.
* Rate limits (per minute): auth/enroll per IP, generations per user → `429 RATE_LIMITED` + `Retry-After`.
* Tenant isolation: another user's generation/asset returns `404` (existence is not revealed).

| Code | HTTP | Meaning |
|---|---|---|
| `VALIDATION_ERROR` | 422 | invalid body/params |
| `INVALID_CREDENTIALS`, `TOKEN_EXPIRED`, `INVALID_TOKEN` | 401 | auth failures |
| `FORBIDDEN` | 403 | admin role required |
| `INSUFFICIENT_CREDITS` | 402 | not enough available credits |
| `IDEMPOTENCY_KEY_REUSED` | 409 | same key, different body |
| `INVALID_STATE` | 409 | e.g. cancel a running job |
| `PROVIDER_DISABLED` | 400 | provider not enabled (e.g. `google_flow`) |

## Auth & user

| Method | Path | Body / notes |
|---|---|---|
| POST | `/auth/register` | `{email, password(10-128)}` → 201 profile |
| POST | `/auth/login` | `{email, password}` → `{access_token, refresh_token, token_type, expires_in}` |
| POST | `/auth/refresh` | `{refresh_token}` → new pair (old one revoked) |
| POST | `/auth/logout` | `{refresh_token}` (auth) → revokes the token family |
| GET | `/user/profile` | |
| GET | `/user/credits` | `{balance, reserved_balance, available_balance}` |
| GET | `/credits/transactions?limit=` | ledger: `GRANT`, `RESERVE`, `SETTLE`, `RELEASE`, `ADJUST` |

## Generations

`POST /generations` — header `Idempotency-Key: <8-128 chars>` (strongly recommended for mobile retries)

```json
{
  "prompt": "A cinematic futuristic city",
  "aspect_ratio": "16:9",
  "number_of_images": 1,
  "reference_images": [],
  "metadata": {"source": "mobile", "client_version": "1.0.0"}
}
```

`201` → `{"success": true, "data": {"generation_id": "uuid", "status": "queued", "created_at": "ISO8601"}}`.
Same key + same body → `200` with the same `generation_id` and header `Idempotent-Replayed: true` (charged once).

* `aspect_ratio`: `1:1, 16:9, 9:16, 4:3, 3:4`; `number_of_images`: 1..`MAX_IMAGES_PER_GENERATION` (4)
* `reference_images`: IDs of the caller's own assets (max 4)
* optional `provider` (default `DEFAULT_PROVIDER=mock`)
* cost = `number_of_images × CREDIT_COST_PER_IMAGE`, reserved at creation

| Method | Path | Notes |
|---|---|---|
| GET | `/generations?limit=&cursor=&status=` | keyset pagination, newest first → `{items, next_cursor}` |
| GET | `/generations/{id}` | includes `assets[]` (signed `url`, `thumbnail_url`, `url_expires_at`, `sha256`, size, dims) and `provider_metadata` when completed |
| POST | `/generations/{id}/cancel` | allowed while `queued`, or `dispatched` before the worker accepted; credits released; idempotent |

### Status machine

```
queued ─► dispatched ─► running ─► uploading ─► completed
  │           │            │           │
  │           │            └───────────┴─► failed | auth_required | provider_limited | review_required
  │           └─► (offer expired / rejected / worker offline) ─► queued   (max MAX_DISPATCH_ATTEMPTS, then failed: DEAD_LETTERED)
  └─► cancelled                     queued too long ─► failed: NO_WORKER_AVAILABLE
```

| Terminal status | Credits | Typical `error_code` |
|---|---|---|
| `completed` | settled (charged once) | – |
| `failed` | released (refund) | `PROVIDER_ERROR`, `WORKER_LOST`, `JOB_TIMEOUT`, `ASSET_VERIFICATION_FAILED`, `DEAD_LETTERED`, `NO_WORKER_AVAILABLE` |
| `auth_required` | released | `AUTH_REQUIRED` (session marked, operator must re-authenticate on the machine) |
| `provider_limited` | released | `PROVIDER_LIMITED` (account marked LIMITED until `retry_after`; job is **not** moved to another account) |
| `review_required` | **kept reserved** until an admin resolves (`refund`/`charge`) | `REVIEW_REQUIRED`, `OUTCOME_UNKNOWN` |
| `cancelled` | released | `CANCELLED_BY_USER` |

`COMPLETED` is set only after every image was downloaded from storage by the server and verified (magic bytes + decoder, declared MIME, size, SHA-256) and its thumbnail stored.

## Assets

| Method | Path | Notes |
|---|---|---|
| GET | `/assets/{id}` | metadata + fresh signed URLs (TTL `SIGNED_URL_TTL_SECONDS`) |
| GET | `/assets/{id}/download` | `307` redirect to a signed URL with `Content-Disposition: attachment` |

The bucket is private; unsigned object URLs return 403.

## Realtime: `WS /ws/v1/events`

Auth: `Authorization: Bearer <access>` header **or** `?access_token=`. Close code `4401` = unauthenticated / token expired → refresh and reconnect.

```json
{"event_id": "uuid", "event": "generation.completed", "timestamp": "ISO8601", "cursor": "1764090000000-0",
 "data": {"generation_id": "uuid", "status": "completed", "progress": 100, "error_code": null,
          "error_message": null, "asset_ids": ["uuid", "uuid"]}}
```

Events: `generation.queued`, `generation.started`, `generation.progress`, `generation.completed`, `generation.failed` (any failure status, see `data.status`), `generation.cancelled`, `credits.updated` (`{balance, reserved_balance, available_balance}`).

* Server sends `{"event": "ping"}` every `HEARTBEAT_INTERVAL_SECONDS`; client may send `{"type":"ping"}` → `{"event":"pong"}`.
* Reconnect with `?since=<last cursor>` to replay missed events (last 500 per user).
* REST remains the source of truth: on reconnect, re-fetch `GET /generations/{id}` for in-flight jobs.

## Agent API (Windows workers)

Agents only make **outbound** connections to the mTLS gateway (`:8443`).

1. `POST /api/v1/agent/enroll` `{token, csr_pem, hostname}` → `{workstation_id, certificate_pem, ca_certificate_pem, expires_at}`.
   Token: single-use, from `POST /api/v1/admin/enrollment-tokens` or `python -m app.cli enrollment-token`. The CSR's subject is ignored; the certificate CN is the workstation id and its SHA-256 fingerprint is pinned server-side.
2. `WSS /ws/v1/agent` with the client certificate. The gateway forwards the verified certificate; the backend checks the gateway secret, the fingerprint and that the workstation is not disabled.

Envelope: `{"type", "id", "ts", "payload"}`. State-changing agent events carry `payload.event_id`, are deduplicated in `worker_events` and always answered with `EVENT_ACK {event_id, status: ok|rejected|error, result, duplicate, retryable}`.

| Direction | Type | Payload (main fields) |
|---|---|---|
| A→S | `REGISTER` | `hostname, agent_version, capacity, providers[], active_tasks[]` → ack `{workstation_id, heartbeat_interval_seconds}` |
| A→S | `HEARTBEAT` | `active_tasks[], free_slots` (every 20–30 s) |
| A→S | `SESSION_STATUS` | `sessions[{google_email, profile_ref, provider, status}]` → ack lists `unregistered` accounts |
| S→A | `TASK_OFFER` | `task_id (lease), generation_id, attempt, slot_index, provider, prompt, aspect_ratio, number_of_images, reference_image_urls, profile_ref, offer_expires_at` |
| A→S | `TASK_ACCEPT` / `TASK_REJECT` | `task_id` (+`reason`); agent executes only after `ok` ack (lease extended to `JOB_TIMEOUT_SECONDS`) |
| A→S | `TASK_STARTED` / `TASK_PROGRESS` | `task_id` / `progress 0-100` |
| A→S | `UPLOAD_REQUEST` | `files[{index, mime_type, size, sha256}]` → ack `uploads[{index, storage_key, method: PUT, url, headers}]` |
| A→S | `UPLOAD_COMPLETED` | `files[{…, storage_key}]` → server verifies objects |
| A→S | `TASK_COMPLETED` | `provider, model, provider_metadata` → rejected unless all images verified |
| A→S | `TASK_FAILED` | `error_code, error_message, retry_after_seconds` (`NOT_STARTED` → safe requeue; `OUTCOME_UNKNOWN` → review) |
| S→A | `TASK_CANCEL` | `task_id, reason` (user cancel before accept, or lease expired) |
| S→A | `EVENT_ACK`, `ERROR` | |

## Admin (`role=admin`)

| Method | Path |
|---|---|
| POST | `/admin/enrollment-tokens` `{workstation_name}` |
| GET | `/admin/workstations` |
| POST | `/admin/workstations/{id}/disable` (revokes access immediately, closes the socket) |
| POST/GET/PATCH | `/admin/google-accounts` (`attestation` required: why automated use is authorized) |
| POST | `/admin/credits/grant` `{user_id, amount, note}` |
| GET | `/admin/generations?status=REVIEW_REQUIRED` |
| POST | `/admin/generations/{id}/resolve` `{action: refund|charge}` |
| GET | `/admin/dead-letters`, `/admin/audit-logs?resource_type=` |

## Health

`GET /health` → 200/503 with `checks: {database, redis, storage}` and `providers` state; `GET /health/live` → liveness only.
