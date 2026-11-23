# Tóm tắt kiến trúc, rủi ro & assumptions (Bước A + B)

Nguồn: `docs/PROJECT_SPEC.md`. File này là bản rút gọn dùng để rà soát trước khi triển khai milestone tiếp theo.

## A. Tóm tắt yêu cầu & kiến trúc

- **Sản phẩm**: app tạo ảnh AI từ prompt. Flutter (iOS/Android) → FastAPI → PostgreSQL (source of truth) → transactional outbox → Redis/Celery → worker → provider adapter → private R2/S3 → signed URL.
- **Provider**: giao diện thống nhất; `MockImageProvider` cho dev/test; `FlowBrowserProvider` **tắt mặc định** (`FLOW_BROWSER_ENABLED=false`), chỉ bật sau Milestone 0 (xác minh quyền/ToS/ownership); `approved_api` cho provider chính thức.
- **Credit**: `balance`/`reserved`, reserve → settle/release, mọi ghi qua transaction + row lock + idempotency key + ledger bất biến.
- **Job**: state machine `QUEUED → DISPATCHED → RUNNING → UPLOADING → COMPLETED` (+ `FAILED/CANCELLED/AUTH_REQUIRED/PROVIDER_LIMITED`), có lease/heartbeat; retry chỉ cho lỗi transient.
- **Bất biến an ninh**: không tự vượt CAPTCHA/2FA/rate limit; không hardcode secret; ảnh không lưu trong DB; kiểm tra ownership ở mọi lookup.
- **Quy mô**: >5.000 user đăng ký (không phải concurrent); capacity worker phải dựa trên benchmark.

Roadmap M0–M8 xem `PROJECT_SPEC.md` §13. **M1 (tài liệu này đi kèm) = foundation**: FastAPI + health checks, Alembic baseline, Docker Compose, CI.

## B. Rủi ro / assumptions / điểm chưa nhất quán trong spec

| # | Vấn đề | Xử lý trong M1 |
|---|--------|----------------|
| 1 | Google Flow chưa xác minh có public API / cho phép automation trong SaaS thương mại | Không viết code Flow; flag tắt, config **từ chối khởi động** nếu bật ở `production` |
| 2 | Shared account pool: rủi ro vi phạm ToS/né quota | Bảng `flow_accounts` chỉ lưu `secret_ref`; chưa có logic dùng |
| 3 | Payment/IAP (Apple/Google) chưa chốt | Chưa có bảng `payments`, `credit_packages` (spec đánh dấu "add") — thêm ở migration sau khi chốt rail |
| 4 | State machine trong spec dùng chữ HOA, cột `status` default `'queued'` (chữ thường); thiếu trạng thái *reconciliation* (spec §2.2 nhắc nhưng sơ đồ không có) | **Quyết định mở**: chốt bộ giá trị + thêm `RECONCILING` và `CHECK`/enum ở M3 |
| 5 | Chưa có bảng `refresh_tokens` dù yêu cầu rotation/revoke | Thêm ở M2 |
| 6 | `.env.example` của spec lặp mật khẩu DB ở `DB_PASSWORD` và `DATABASE_URL` → dễ lệch | Compose ghi đè `DATABASE_URL` từ `DB_PASSWORD` (một nguồn) |
| 7 | Spec đặt gốc monorepo là `ai-image-platform/`; repo hiện tại là `eu` | Dùng gốc repo hiện tại làm monorepo (không lồng thư mục); `eu.txt` có sẵn giữ nguyên |
| 8 | Giá credit, cloud/region, retention, SLO chưa chốt | Giá do config server-side (M2/M3); còn lại là quyết định của chủ dự án |
| 9 | Redis/Celery `acks_late` + visibility timeout có thể giao trùng task | Worker phải idempotent (M3); outbox là nguồn sự thật, không phải Redis |
| 10 | Ledger bất biến chỉ ở tầng ứng dụng thì chưa đủ | Thêm trigger DB chặn `UPDATE/DELETE` trên `credit_transactions` (có test) |
| 11 | `docker compose up` **chưa được chạy thử** trong môi trường phát triển này (không có Docker daemon) | Compose đã qua `docker compose config`; cần chạy thử trên máy có Docker trước khi coi M1 là xong |
