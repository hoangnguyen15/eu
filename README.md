# Google Flow Automation Platform

Backend API production-ready cho phép Flutter Mobile App gửi prompt tạo ảnh và nhận kết quả từ các Windows Worker Agent.

```
Flutter Mobile ──REST/WS──► Central API (FastAPI) ──► PostgreSQL (jobs, credits, ledger, audit)
                                  │   ▲                    ▲
                    Redis Streams │   │ Redis Pub/Sub      │
                                  ▼   │                    │
                             Scheduler (leases, recovery, DLQ)
                                  │
                      TASK_OFFER  ▼
          nginx mTLS gateway :8443 ◄──── outbound WSS (client cert) ──── Windows FlowAgent (.NET 8)
                                                                              │ provider adapter
                                                                              │ (Mock / Approved / GoogleFlow=DISABLED)
                                                                              ▼
                                           S3 / MinIO / R2 (private) ◄── PUT signed URL
                                                  │
                         server verifies MIME, size, SHA-256, makes thumbnail ──► COMPLETED + settle credits
                                                  │
                         Mobile: REST result + WebSocket event + signed download URL
```

> **Google Flow**: connector chỉ có interface và luôn ở trạng thái **DISABLED**. Hệ thống không giả định Google Flow có API tự động hoá công khai, không có browser automation, không bypass CAPTCHA/MFA/anti-bot/rate-limit, không lưu mật khẩu Google, không trích xuất/chuyển cookie hay session token giữa các máy. `MockImageProvider` dùng để phát triển và kiểm thử end-to-end; `ApprovedImageProvider` là adapter mẫu cho API ảnh mà bạn có giấy phép sử dụng.

## Thành phần

| Thư mục | Nội dung |
|---|---|
| `backend/app/api/v1` | REST: auth, user/credits, generations, assets, agent enrollment, admin; WS `/ws/v1/events` |
| `backend/app/workers` | Agent gateway WS `/ws/v1/agent`, protocol, event handlers, PKI (CA + ký CSR) |
| `backend/app/scheduler` | Dispatcher (lease, slot, chống trùng), recovery (offline/timeout), dead-letter |
| `backend/app/services` | Credit ledger, generation lifecycle, realtime events, audit |
| `backend/app/storage` | S3 client (signed URL), kiểm tra ảnh (magic bytes + decoder, SHA-256, thumbnail) |
| `backend/app/providers` | `ImageProvider` interface + Mock / Approved / GoogleFlow (disabled) |
| `backend/migrations` | Alembic migrations (UUID PK, FK, index, partial unique, JSONB) |
| `backend/tests` | Unit + integration tests (Postgres/Redis/MinIO thật, API + scheduler chạy thật) |
| `windows-agent/FlowAgent` | .NET 8 Worker Service: mTLS WSS, SQLite outbox, slots, uploader, heartbeat |
| `windows-agent/installer` | `install.ps1`, `uninstall.ps1`; `publish.ps1` build bản win-x64 self-contained |
| `gateway/` | nginx template: mTLS termination cho agent |
| `scripts/` | `e2e.sh`, `resilience.sh`, `backup-db.sh`, `restore-db.sh` |
| `docs/` | [API](docs/API.md), [Deployment](docs/DEPLOYMENT.md), [Test report](docs/TEST-REPORT.md), [OpenAPI](docs/openapi.json) |

## Chạy local

Yêu cầu: Docker + Docker Compose v2.

```bash
cp .env.example .env
# thay toàn bộ CHANGE_ME (vd. openssl rand -hex 32); với local đặt ENVIRONMENT=development để bật /docs
docker compose up -d --build --wait
curl -s localhost:8000/api/v1/health
```

Services: `postgres`, `redis`, `minio`, `pki-init` (tạo CA + cert gateway, 1 lần), `migrate` (Alembic, 1 lần), `backend` (:8000), `scheduler`, `agent-gateway` (:8443, mTLS).

### Chạy end-to-end với agent .NET thật

```bash
./scripts/e2e.sh          # tạo admin, account mock được uỷ quyền, enrollment token, chạy FlowAgent, kiểm tra ~30 điểm (REST, WS, storage, credits)
./scripts/resilience.sh   # mất gateway giữa job, restart agent, SIGKILL agent giữa job
```

`e2e.sh` chạy chính source `windows-agent/FlowAgent` trong container Linux (profile `e2e`); bản production chạy `FlowAgent.exe` dưới dạng Windows Service.

### Tests

```bash
docker compose --profile test run --rm tests            # 55 tests backend (unit + integration)
cd windows-agent && dotnet test FlowAgent.Tests          # 10 tests agent (cũng chạy trong docker build)
```

Chạy test backend trên máy host: cần Python 3.12, `pip install -r backend/requirements-dev.txt`, và Postgres/Redis/S3 — biến `TEST_PG_DSN`, `TEST_REDIS_URL`, `TEST_S3_URL`, `TEST_S3_KEY`, `TEST_S3_SECRET` (xem `backend/tests/conftest.py`).

### Quản trị nhanh

```bash
docker compose exec -e ADMIN_PASSWORD=... backend python -m app.cli create-admin --email admin@example.com
docker compose exec backend python -m app.cli add-account --email acct@example.com --attest "lý do được phép dùng"
docker compose exec backend python -m app.cli enrollment-token --name WIN-WORKER-01
```

Hoặc qua admin API (`/api/v1/admin/...`, role `admin`).

## Luồng một generation

1. Mobile `POST /api/v1/generations` (+ `Idempotency-Key`) → kiểm tra provider/aspect ratio → **reserve credit** (khoá hàng wallet) → `QUEUED` → XADD Redis Stream → WS `generation.queued`.
2. Scheduler chọn worker `ONLINE` còn slot trống, account `AUTHORIZED` + session `ACTIVE`, tạo **task lease** (partial unique index: 1 lease active / slot và / generation) → `DISPATCHED` → `TASK_OFFER`.
3. Agent `TASK_ACCEPT` (chờ ack) → `TASK_STARTED` → provider → lưu ảnh vào đĩa → `UPLOAD_REQUEST` → PUT lên signed URL → `UPLOAD_COMPLETED`.
4. Server tải lại object, kiểm tra MIME thật, kích thước, SHA-256, decode, tạo thumbnail WebP, ghi `assets` → `TASK_COMPLETED` → chỉ khi đủ ảnh đã xác minh mới `COMPLETED` + **settle credit** (đúng 1 lần) → WS `generation.completed`, `credits.updated`.
5. Lỗi: `AUTH_REQUIRED` / `PROVIDER_LIMITED` / `FAILED` hoàn credit; `REVIEW_REQUIRED` giữ credit chờ admin xử lý. Không tự đổi account khi bị giới hạn; không retry khi không biết kết quả phía provider.

## Giả định & giới hạn đã biết

- Mặc định 1 credit / ảnh, tặng 10 credit khi đăng ký (cấu hình qua `.env`).
- MinIO upstream không còn phát hành image cộng đồng; compose dùng `pgsty/minio` (bản build cộng đồng, pin theo digest), đổi bằng `MINIO_IMAGE`. Production nên dùng Cloudflare R2/S3.
- Agent .NET đã được build cho `win-x64` (cross-compile) và chạy E2E trên Linux; **chưa được chạy thực tế trên Windows Server 2022** trong môi trường này (DPAPI, `NT SERVICE\FlowAgent`, SChannel client-cert là các phần riêng Windows chưa được kiểm thử). `install.ps1`/`uninstall.ps1` mới được kiểm tra cú pháp bằng PowerShell parser.
- `ApprovedImageProvider` là template HTTP trung tính; cần map sang API thật của nhà cung cấp trước khi bật.
- Rate limit là fixed-window trên Redis, fail-open (có log cảnh báo) khi Redis không truy cập được.
