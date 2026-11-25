# Đối chiếu yêu cầu

Ký hiệu: ✅ đã làm và đã chạy kiểm chứng · 🟡 đã làm nhưng chưa kiểm chứng được trong môi trường này · ⛔ ngoài phạm vi hoặc cố ý không làm.

## A. Master prompt (lượt 1)

| # | Yêu cầu | Trạng thái | Nơi thực hiện / bằng chứng |
|---|---|---|---|
| 1 | Mobile gửi prompt → backend tạo job → phân phối → worker → ảnh + metadata → lưu → trả kết quả (REST + WS) | ✅ | `scripts/e2e.sh` 28/28 với agent .NET thật; `test_end_to_end_mock_pipeline` |
| 1 | Nhiều tài khoản, nhiều worker, nhiều job đồng thời, ≥10 workers | ✅ | `test_fleet_of_ten_workers` (10 worker × 40 job); bench 2 agent |
| 1 | Hệ thống credit | ✅ | `credit_service.py`; tests reserve/refund/over-spend/idempotent |
| 2 | Stack: Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic, PostgreSQL 16, Redis 7 (Streams + Pub/Sub), S3/MinIO/R2, .NET 8 Worker, WSS, SQLite outbox, Docker Compose, Pytest/HTTPX | ✅ | `backend/requirements.txt`, `windows-agent/FlowAgent.csproj`, `docker-compose.yml` |
| 2 | Cloudflare R2 | 🟡 | Hỗ trợ qua cấu hình S3 endpoint (`docs/DEPLOYMENT.md`); chưa chạy với R2 thật |
| 2 | Windows Server 2022 cho worker | 🟡 | Build `win-x64` thành công, chạy E2E dưới dạng container Linux; chưa chạy trên Windows thật |
| 3 | Cấu trúc thư mục | ✅ | Repo root = `google-flow-platform/` (backend/, windows-agent/, docs/, compose, .env.example, README) |
| 4 | 14 bảng, UUID PK, FK, index, unique, JSONB, Alembic, transaction an toàn, idempotency | ✅ | `models/__init__.py`, `migrations/versions/0001_initial_schema.py` (+ task_leases, enrollment_tokens); `alembic check` sạch |
| 5 | Endpoints auth/user/generations/assets/health + WS | ✅ | `docs/API.md`, `docs/openapi.json` |
| 5 | JWT, refresh rotation, Argon2, cô lập tenant, rate limit, validation, Idempotency-Key | ✅ | `test_api.py` |
| 6 | Request/response mẫu, 10 trạng thái, không tạo trùng khi retry | ✅ | `test_idempotency_*` (cả 8 retry đồng thời) |
| 7 | Giao thức worker (13 message), mTLS, heartbeat 20–30 s, lease/timeout, 1 job/slot, signed URL upload, metadata, SQLite outbox, tự reconnect, không retry khi không rõ kết quả | ✅ | `workers/`, `FlowAgent/`; `resilience.sh` 7/7; thêm `TASK_CANCEL`, `SESSION_CHECK` |
| 8 | `ImageProvider` (generate/get_status/collect_result); Mock; Approved; GoogleFlowConnector DISABLED; không bypass; không lưu mật khẩu/cookie | ✅ | `providers/` (Python + C#); tests xác nhận disabled |
| 8 | ApprovedImageProvider với API thật | 🟡 | Template HTTP; cần map sang API nhà cung cấp |
| 9 | Scheduler: worker ONLINE, slot trống, kiểm tra authorization + session, lease, chống trùng, nhiều worker, timeout, DLQ, recovery khi offline, không đổi tài khoản để né giới hạn | ✅ | `scheduler/dispatcher.py`; tests offline/requeue/DLQ/limited |
| 10 | PNG/JPEG/WebP, MIME thật, size, SHA-256, thumbnail, metadata, bucket private, signed URL có hạn, không lưu ảnh trong DB | ✅ | `storage/images.py`; tests checksum/mime/corrupt; bucket private (403 khi không ký) |
| 11 | Reserve → settle/release, khoá giao dịch, không trừ 2 lần, ledger, settle 1 lần/generation | ✅ | partial unique index + `credit_state`; tests |
| 12 | WS `/ws/v1/events`, 7 loại event, event_id/event/timestamp/data, JWT, heartbeat, reconnect, REST dự phòng | ✅ | replay `?since=`; web client tự chuyển sang REST khi mất WS (đã quan sát được) |
| 13 | Compose: backend, postgres, redis, minio, scheduler; healthcheck, volume, env, restart, migration, Dockerfile tối ưu; không commit secret | ✅ | `docker-compose.yml`, multi-stage Dockerfile, `.gitignore` |
| 14 | JWT, RBAC, mTLS, enrollment token dùng 1 lần, rate limit, validation, private storage, signed URL, audit log, CORS allowlist, giới hạn kích thước request, tenant isolation | ✅ | tests; kiểm tra âm tính mTLS qua nginx |
| 14 | Secret management | 🟡 | `.env` (chmod 600) + hướng dẫn Docker secrets/Vault; chưa tích hợp secret manager cụ thể |
| 14 | Database backup | ✅ | `scripts/backup-db.sh` / `restore-db.sh` |
| 15 | Tests: 14 nhóm + E2E MockImageProvider | ✅ | 59 backend + 14 agent + E2E + resilience + UI |
| 16 | .env.example, compose, migration, publish script, install/uninstall.ps1, README local + production, API docs, OpenAPI | ✅ | `docs/`, `windows-agent/` |
| 17 | Không hardcode credential, không giả lập thành công, chỉ COMPLETED sau khi ảnh đã lưu + xác minh, không bật Google Flow | ✅ | `handle_task_completed` từ chối nếu chưa xác minh đủ ảnh (`test_task_completed_rejected_before_assets_verified`) |

## B. Yêu cầu lượt 2 (QA/DevOps)

| Yêu cầu | Trạng thái | Bằng chứng |
|---|---|---|
| Cài deps, migration, Compose, unit + integration tests | ✅ | `docs/TEST-REPORT.md` |
| Sửa lỗi đến khi E2E MockImageProvider chạy được | ✅ | 2 lỗi agent tìm ra bằng failure injection, đã sửa |

## C. Yêu cầu lượt 3

| Yêu cầu | Trạng thái | Bằng chứng |
|---|---|---|
| Màn hình web demo cho mobile + web client test hệ thống | ✅ | `/app/` (mobile-first; tạo ảnh, realtime, kịch bản lỗi, kiểm tra tải); `scripts/ui-check.js` |
| Bản cài đặt cho máy trạm | ✅ | `scripts/package-agent.sh` → `FlowAgent-1.1.0-win-x64.zip` (exe self-contained, `Install-FlowAgent.cmd`, README tiếng Việt); `install.ps1 -DryRun` đã chạy với cấu hình thật; agent thứ 2 enroll bằng file cấu hình do trang quản trị xuất |
| Hướng dẫn cài server trên macOS | ✅/🟡 | `docs/MACOS-SERVER.md` + `init-env.sh`/`bootstrap.sh`; cấu hình LAN-IP + bridge đã chạy E2E (trên Linux). Chưa chạy trên một máy Mac thật |
| Tối ưu tốc độ, không lỗi | ✅ | throughput 0.96 → 1.97 job/s (2 slot), 3.7 job/s (4 slot); p95 19.8 s → 10.0 s; job đơn 1.2 s → 1.0 s |
| Kiểm tra lại toàn bộ yêu cầu, test kỹ | ✅ | tài liệu này + `docs/TEST-REPORT.md` |
| Vẽ lại mô hình luồng chạy | ✅ | `docs/ARCHITECTURE.md` (Mermaid) |
| Trang quản lý máy trạm: đăng nhập (Google Flow), trạng thái máy, kết quả, tiến trình | ✅ | `/admin/`: Tổng quan, Máy trạm, Phiên đăng nhập (trạng thái + "Kiểm tra lại phiên"), Công việc (tiến trình + ảnh), Cần xử lý, Thêm máy trạm, Nhật ký |
| — tự động đăng nhập Google Flow từ trang quản trị | ⛔ | Cố ý không làm: không lưu mật khẩu, cookie hay token, không tự động hoá đăng nhập. Người vận hành tự đăng nhập trên máy trạm |
| Ứng dụng Flutter thật | ⛔ | Không nằm trong yêu cầu; web client `/app/` dùng đúng API mà Flutter sẽ dùng |
