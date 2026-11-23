**AI IMAGE GENERATION PLATFORM**

Technical Architecture & Claude Code Handoff

*Tài liệu tổng hợp kiến trúc, database, API, source structure, deployment và roadmap*

| **Hạng mục**       | **Quyết định đã chốt**                                                             |
|--------------------|------------------------------------------------------------------------------------|
| Sản phẩm           | Ứng dụng tạo ảnh AI từ prompt                                                      |
| Mobile             | Flutter – iOS và Android                                                           |
| Backend            | Python + FastAPI                                                                   |
| Image provider     | Google Flow qua browser connector – chỉ bật sau khi xác minh quyền và tính khả thi |
| Tài khoản provider | Shared account pool do hệ thống quản lý, có ủy quyền hợp lệ                        |
| Billing            | Credit theo job/ảnh, có reserve – settle – refund                                  |
| Queue              | Redis + Celery; transactional outbox                                               |
| Database           | PostgreSQL                                                                         |
| Image storage      | Cloudflare R2 hoặc S3-compatible private bucket                                    |
| Infrastructure     | Cloud deployment, Docker; mở rộng worker độc lập                                   |
| Quy mô mục tiêu    | Large: trên 5.000 user đăng ký; concurrency/throughput phải benchmark              |

Mục đích tài liệu: đưa trực tiếp cho Claude Code để khởi tạo repository, triển khai từng module và tạo bộ mã nguồn có thể kiểm thử. Tài liệu này là blueprint kỹ thuật; không khẳng định Google Flow cung cấp API công khai hoặc cho phép browser automation trong mô hình SaaS.

# 0. Hướng dẫn sử dụng với Claude Code

Không yêu cầu Claude Code sinh toàn bộ hệ thống trong một lần. Hãy đưa tài liệu này vào thư mục docs/PROJECT_SPEC.md, sau đó yêu cầu Claude Code thực hiện theo milestone. Trước tiên, yêu cầu Claude đọc toàn bộ spec, lập implementation plan và xác định các điểm chưa rõ; không tự bịa API của Google Flow.

## Prompt khởi động dành cho Claude Code

> Bạn là Principal Software Architect kiêm Senior Flutter/Python Engineer.  
> Hãy đọc toàn bộ docs/PROJECT_SPEC.md trước khi viết code.  
>   
> Mục tiêu: xây dựng AI Image Generation SaaS theo đúng quyết định và yêu cầu trong tài liệu.  
> Quy tắc:  
> 1. Không giả định Google Flow có public API.  
> 2. Flow browser connector phải bị feature-flag tắt mặc định và chỉ triển khai sau khi xác minh quyền sử dụng, điều khoản và quy trình đăng nhập hợp lệ.  
> 3. Tạo MockImageProvider để phát triển/test end-to-end khi chưa có provider thật.  
> 4. Không tự động vượt CAPTCHA, 2FA, rate limit hoặc cơ chế chống bot.  
> 5. Credit phải dùng transaction, row locking, idempotency và ledger bất biến.  
> 6. Job dispatch phải dùng transactional outbox; worker phải có lease/heartbeat và xử lý retry an toàn.  
> 7. Không hardcode secrets; dùng .env.example cho local và secret manager cho production.  
> 8. Viết test cho mọi nghiệp vụ quan trọng; không đánh dấu milestone hoàn thành nếu test chưa chạy.  
> 9. Triển khai theo milestone, mỗi milestone phải có README, migration, tests và lệnh chạy.  
> 10. Trước khi sửa cấu trúc lớn, trình bày kế hoạch và các trade-off.  
>   
> Bắt đầu bằng:  
> A. Tóm tắt yêu cầu và kiến trúc.  
> B. Liệt kê rủi ro/assumptions.  
> C. Tạo repository skeleton.  
> D. Triển khai Milestone 1: backend foundation, database migration, health checks và Docker Compose local.  
> Chưa triển khai browser automation thực tế.

# 1. Product scope

## 1.1 MVP scope

- Đăng ký, đăng nhập, refresh token, logout, hồ sơ người dùng.

- Màn hình nhập prompt, chọn aspect ratio và số ảnh trong giới hạn provider.

- Tạo job bất đồng bộ; trả job_id ngay; xem trạng thái và lịch sử.

- Gallery hiển thị ảnh hoàn thành; tải ảnh từ private object storage qua signed URL.

- Ví credit, lịch sử giao dịch, reserve khi tạo job, settle khi thành công, release/refund khi thất bại theo chính sách.

- Admin dashboard tối thiểu: user, job, provider health, worker health, credit ledger và audit log.

- Mock provider để kiểm thử end-to-end trước khi kết nối provider thật.

## 1.2 Không thuộc MVP

- Tạo video, image-to-image, inpainting, batch generation nâng cao.

- Marketplace prompt, social feed, referral và subscription phức tạp.

- Multi-region active-active.

- Tự động vượt xác thực, CAPTCHA, quota hoặc biện pháp chống bot của nhà cung cấp.

# 2. Kiến trúc hệ thống

> Flutter iOS/Android  
> \|  
> \| HTTPS REST + WebSocket/SSE (optional)  
> v  
> Cloudflare WAF/CDN -\> Load Balancer  
> \|  
> v  
> FastAPI API Cluster  
> \|-- Auth & User Service  
> \|-- Job Service  
> \|-- Credit/Wallet Service  
> \|-- Asset Service  
> \|-- Admin & Audit Service  
> \|  
> +---- PostgreSQL (source of truth)  
> \|  
> +---- Transactional Outbox  
> \|  
> v  
> Outbox Publisher  
> \|  
> v  
> Redis / Celery  
> \|  
> v  
> Worker Orchestrator  
> \|  
> v  
> Provider Adapter  
> \| \|  
> v v  
> FlowBrowserProvider Official/Approved Image API  
> (disabled by default)  
> \|  
> v  
> Image Validation  
> \|  
> v  
> Private R2 / S3  
> \|  
> v  
> Signed URL -\> Flutter

Nguyên tắc: API không điều khiển browser trực tiếp; worker không nhận credential của end user; database là nguồn sự thật; Redis chỉ là broker/cache; file ảnh không lưu trong PostgreSQL.

## 2.1 Các service

| **Service**      | **Trách nhiệm**                                                              |
|------------------|------------------------------------------------------------------------------|
| Flutter app      | Auth, prompt form, job progress, gallery, wallet, profile                    |
| FastAPI          | REST API, authorization, validation, job creation, wallet transaction        |
| PostgreSQL       | Users, wallets, ledger, jobs, assets, accounts metadata, audit, outbox       |
| Redis/Celery     | Queue, dispatch, retry có kiểm soát, worker coordination                     |
| Worker           | Nhận job, gọi provider, validate output, upload storage, cập nhật trạng thái |
| Provider adapter | Interface thống nhất; mock provider; provider được phê duyệt                 |
| R2/S3            | Private object storage, lifecycle, signed download URL                       |
| Admin            | Quản lý user, job, provider status, worker status, audit                     |
| Observability    | Metrics, logs, traces, error tracking và cảnh báo                            |

## 2.2 Job state machine

> QUEUED -\> DISPATCHED -\> RUNNING -\> UPLOADING -\> COMPLETED  
> \| \| \| \|  
> +----------+-----------+-------------+--\> FAILED  
> +----------------------------------------\> CANCELLED  
> +----------------------------------------\> AUTH_REQUIRED  
> +----------------------------------------\> PROVIDER_LIMITED

Yêu cầu: mọi trạng thái chuyển đổi phải được validate; job có lease/timeout; retry chỉ áp dụng lỗi transient và phải chống tạo ảnh trùng. Nếu không xác định được provider đã nhận request hay chưa, chuyển sang trạng thái cần reconciliation thay vì retry mù.

# 3. Shared provider account management

Shared account pool là tài khoản do tổ chức sở hữu hoặc có quyền sử dụng rõ ràng. Không dùng account rotation để né quota, không chia sẻ credential trái phép, không tự động vượt CAPTCHA/2FA. Tính khả thi của việc dùng Google Flow browser automation trong SaaS phải được xác minh trước khi bật.

| **Field/Status**     | **Mô tả**                                                                  |
|----------------------|----------------------------------------------------------------------------|
| flow_accounts.id     | UUID nội bộ                                                                |
| label                | Tên định danh nội bộ, không nhất thiết lưu email                           |
| status               | DISABLED, READY, AUTH_REQUIRED, COOLDOWN, LIMITED, ERROR                   |
| secret_ref           | Tham chiếu đến secret manager; không lưu password/cookie dạng plaintext    |
| max_concurrency      | Giới hạn nội bộ đã được kiểm chứng và được phép                            |
| last_health_check_at | Thời điểm health check                                                     |
| last_error_code      | Mã lỗi đã chuẩn hóa, không chứa secret                                     |
| owner/consent        | Thông tin chứng minh quyền kiểm soát/ủy quyền lưu ở hệ thống audit phù hợp |

Mặc định FLOW_BROWSER_ENABLED=false. Nếu cần xác thực lại, trạng thái AUTH_REQUIRED và admin/account owner xử lý theo luồng chính thức. Browser worker phải chạy sandbox, không có quyền truy cập metadata service hoặc mạng nội bộ.

# 4. Database design

| **Table**             | **Key fields / purpose**                                                                       |
|-----------------------|------------------------------------------------------------------------------------------------|
| users                 | id, email, password_hash, status, role, created_at                                             |
| wallets               | user_id PK, balance, reserved; available = balance - reserved                                  |
| credit_transactions   | id, user_id, job_id, type, amount, idempotency_key, created_at                                 |
| image_jobs            | id, user_id, provider, prompt, aspect_ratio, status, credit_cost, idempotency_key, lease_until |
| image_assets          | id, job_id, storage_key, mime_type, width, height, size_bytes                                  |
| flow_accounts         | id, label, status, secret_ref, max_concurrency, health fields                                  |
| worker_nodes          | id, hostname, status, capacity, heartbeat                                                      |
| job_attempts          | id, job_id, worker_id, status, error_code, timestamps                                          |
| outbox_events         | id, event_type, aggregate_id, payload, published_at                                            |
| audit_logs            | actor_id, action, resource, metadata, created_at                                               |
| payments (add)        | provider, provider_payment_id, user_id, amount, currency, status, verified_at                  |
| credit_packages (add) | SKU, credits, price, currency, active                                                          |

## 4.1 SQL migration baseline

> CREATE EXTENSION IF NOT EXISTS pgcrypto;  
>   
> CREATE TABLE users (  
> id UUID PRIMARY KEY DEFAULT gen_random_uuid(),  
> email VARCHAR(255) UNIQUE NOT NULL,  
> password_hash TEXT NOT NULL,  
> role VARCHAR(30) NOT NULL DEFAULT 'user',  
> status VARCHAR(20) NOT NULL DEFAULT 'active',  
> created_at TIMESTAMPTZ NOT NULL DEFAULT now()  
> );  
>   
> CREATE TABLE wallets (  
> user_id UUID PRIMARY KEY REFERENCES users(id),  
> balance BIGINT NOT NULL DEFAULT 0 CHECK (balance \>= 0),  
> reserved BIGINT NOT NULL DEFAULT 0 CHECK (reserved \>= 0 AND reserved \<= balance),  
> updated_at TIMESTAMPTZ NOT NULL DEFAULT now()  
> );  
>   
> CREATE TABLE image_jobs (  
> id UUID PRIMARY KEY DEFAULT gen_random_uuid(),  
> user_id UUID NOT NULL REFERENCES users(id),  
> provider VARCHAR(50) NOT NULL,  
> prompt TEXT NOT NULL CHECK (length(prompt) BETWEEN 1 AND 4000),  
> aspect_ratio VARCHAR(10) NOT NULL DEFAULT '1:1',  
> number_of_images SMALLINT NOT NULL DEFAULT 1 CHECK (number_of_images BETWEEN 1 AND 4),  
> status VARCHAR(30) NOT NULL DEFAULT 'queued',  
> credit_cost BIGINT NOT NULL CHECK (credit_cost \>= 0),  
> idempotency_key VARCHAR(150) NOT NULL,  
> lease_until TIMESTAMPTZ,  
> created_at TIMESTAMPTZ NOT NULL DEFAULT now(),  
> completed_at TIMESTAMPTZ,  
> UNIQUE(user_id, idempotency_key)  
> );  
>   
> CREATE TABLE credit_transactions (  
> id UUID PRIMARY KEY DEFAULT gen_random_uuid(),  
> user_id UUID NOT NULL REFERENCES users(id),  
> job_id UUID REFERENCES image_jobs(id),  
> type VARCHAR(30) NOT NULL,  
> amount BIGINT NOT NULL,  
> idempotency_key VARCHAR(150) UNIQUE NOT NULL,  
> created_at TIMESTAMPTZ NOT NULL DEFAULT now()  
> );  
>   
> CREATE TABLE image_assets (  
> id UUID PRIMARY KEY DEFAULT gen_random_uuid(),  
> job_id UUID NOT NULL REFERENCES image_jobs(id),  
> storage_key TEXT NOT NULL UNIQUE,  
> mime_type VARCHAR(100) NOT NULL,  
> width INT,  
> height INT,  
> size_bytes BIGINT,  
> created_at TIMESTAMPTZ NOT NULL DEFAULT now()  
> );  
>   
> CREATE TABLE flow_accounts (  
> id UUID PRIMARY KEY DEFAULT gen_random_uuid(),  
> label VARCHAR(100) NOT NULL,  
> status VARCHAR(30) NOT NULL DEFAULT 'disabled',  
> secret_ref TEXT,  
> max_concurrency INT NOT NULL DEFAULT 1 CHECK (max_concurrency \> 0),  
> last_health_check_at TIMESTAMPTZ,  
> last_error_code VARCHAR(100),  
> created_at TIMESTAMPTZ NOT NULL DEFAULT now()  
> );  
>   
> CREATE TABLE worker_nodes (  
> id UUID PRIMARY KEY DEFAULT gen_random_uuid(),  
> hostname VARCHAR(255) NOT NULL,  
> status VARCHAR(30) NOT NULL DEFAULT 'offline',  
> capacity INT NOT NULL DEFAULT 1 CHECK (capacity \> 0),  
> last_heartbeat TIMESTAMPTZ  
> );  
>   
> CREATE TABLE job_attempts (  
> id UUID PRIMARY KEY DEFAULT gen_random_uuid(),  
> job_id UUID NOT NULL REFERENCES image_jobs(id),  
> worker_id UUID REFERENCES worker_nodes(id),  
> status VARCHAR(30) NOT NULL,  
> error_code VARCHAR(100),  
> started_at TIMESTAMPTZ NOT NULL DEFAULT now(),  
> finished_at TIMESTAMPTZ  
> );  
>   
> CREATE TABLE outbox_events (  
> id UUID PRIMARY KEY DEFAULT gen_random_uuid(),  
> event_type VARCHAR(100) NOT NULL,  
> aggregate_id UUID NOT NULL,  
> payload JSONB NOT NULL,  
> created_at TIMESTAMPTZ NOT NULL DEFAULT now(),  
> published_at TIMESTAMPTZ  
> );  
>   
> CREATE TABLE audit_logs (  
> id UUID PRIMARY KEY DEFAULT gen_random_uuid(),  
> actor_id UUID REFERENCES users(id),  
> action VARCHAR(100) NOT NULL,  
> resource_type VARCHAR(100),  
> resource_id UUID,  
> metadata JSONB,  
> created_at TIMESTAMPTZ NOT NULL DEFAULT now()  
> );  
>   
> CREATE INDEX idx_jobs_status_created ON image_jobs(status, created_at);  
> CREATE INDEX idx_jobs_user_created ON image_jobs(user_id, created_at DESC);  
> CREATE INDEX idx_attempts_job ON job_attempts(job_id);  
> CREATE INDEX idx_outbox_pending ON outbox_events(created_at)  
> WHERE published_at IS NULL;

Credit semantics: balance là tổng credit đã sở hữu; reserved là phần đang giữ; available = balance - reserved. Khi reserve: tăng reserved. Khi thành công: giảm balance và reserved. Khi thất bại được hoàn: chỉ giảm reserved. Mỗi lần ghi phải chạy trong transaction, lock wallet row, ghi ledger với idempotency key và đảm bảo không thể settle/refund hai lần.

# 5. API specification

| **Method** | **Endpoint**                     | **Purpose**                              |
|------------|----------------------------------|------------------------------------------|
| POST       | /v1/auth/register                | Register                                 |
| POST       | /v1/auth/login                   | Login                                    |
| POST       | /v1/auth/refresh                 | Refresh token                            |
| POST       | /v1/auth/logout                  | Logout/revoke refresh token              |
| GET        | /v1/users/me                     | Current user                             |
| POST       | /v1/jobs                         | Create image job; returns 202 + job_id   |
| GET        | /v1/jobs                         | Paginated user job history               |
| GET        | /v1/jobs/{job_id}                | Job status and assets                    |
| POST       | /v1/jobs/{job_id}/cancel         | Request cancellation                     |
| GET        | /v1/assets/{asset_id}            | Get authorized asset metadata/signed URL |
| DELETE     | /v1/assets/{asset_id}            | Delete user's asset                      |
| GET        | /v1/credits/balance              | Wallet balance and reserved amount       |
| GET        | /v1/credits/transactions         | Credit ledger                            |
| GET        | /v1/credits/packages             | Available packages                       |
| POST       | /v1/payments/checkout            | Create payment                           |
| POST       | /v1/webhooks/payments/{provider} | Verified payment webhook                 |
| GET        | /v1/admin/jobs                   | Admin job monitoring                     |
| GET        | /v1/admin/workers                | Worker health                            |
| GET        | /v1/admin/provider-accounts      | Provider account health                  |

## 5.1 Create job request

> POST /v1/jobs  
> Authorization: Bearer \<access_token\>  
> Idempotency-Key: \<unique-client-request-id\>  
>   
> {  
> "prompt": "A futuristic city at sunset",  
> "aspect_ratio": "16:9",  
> "number_of_images": 1  
> }  
>   
> 202 Accepted  
> {  
> "job_id": "uuid",  
> "status": "queued",  
> "credits_reserved": 5,  
> "poll_after_seconds": 3  
> }

Giá 5 credits chỉ là ví dụ cấu hình. Giá thực tế phải do pricing config ở backend quyết định; không tin giá gửi từ client. API phải kiểm tra ownership ở mọi job/asset lookup. Pagination dùng cursor hoặc keyset pagination.

# 6. Source code monorepo

> ai-image-platform/  
> apps/  
> mobile/ \# Flutter iOS/Android  
> lib/core/{api,auth,config,storage}/  
> lib/features/{auth,generator,jobs,gallery,credits,profile}/  
> test/  
> admin/ \# Next.js admin dashboard  
> services/  
> backend/  
> app/{api/v1,models,schemas,repositories,services,security,db}/  
> alembic/  
> tests/  
> Dockerfile  
> worker/  
> tasks/  
> providers/{base,mock,flow_browser,approved_api}/  
> browser/  
> tests/  
> Dockerfile  
> packages/  
> api_contracts/ \# OpenAPI-generated/client contracts if needed  
> infrastructure/  
> docker-compose.yml  
> nginx/  
> monitoring/  
> terraform/  
> database/  
> migrations/  
> docs/  
> PROJECT_SPEC.md  
> RUNBOOK.md  
> SECURITY.md  
> .env.example  
> README.md

## 6.1 Flutter packages (suggested)

- flutter_riverpod hoặc bloc: state management (chọn một, không trộn tùy tiện).

- dio: HTTP client, interceptor và retry có kiểm soát.

- go_router: navigation.

- flutter_secure_storage: lưu refresh token an toàn.

- cached_network_image: hiển thị ảnh cache.

- freezed + json_serializable: immutable models và JSON serialization.

- firebase_messaging: push notifications nếu dùng FCM.

## 6.2 Backend packages (suggested)

- fastapi, uvicorn, pydantic-settings.

- sqlalchemy 2.x async, asyncpg, alembic.

- celery, redis.

- argon2-cffi hoặc bcrypt cho password hashing; JWT library đã được kiểm tra.

- boto3 hoặc aioboto3 cho S3-compatible storage.

- httpx, structlog, prometheus-client, sentry-sdk.

- pytest, pytest-asyncio, httpx test client, testcontainers (nếu phù hợp).

# 7. Docker Compose local/staging

> name: ai-image-platform  
>   
> services:  
> api:  
> build: ../services/backend  
> restart: unless-stopped  
> env_file: ../.env  
> command: uvicorn app.main:app --host 0.0.0.0 --port 8000  
> ports:  
> - "127.0.0.1:8000:8000"  
> depends_on:  
> postgres:  
> condition: service_healthy  
> redis:  
> condition: service_healthy  
> networks: \[internal\]  
>   
> worker:  
> build: ../services/worker  
> restart: unless-stopped  
> env_file: ../.env  
> command: celery -A celery_app worker --loglevel=info --concurrency=2  
> depends_on:  
> postgres:  
> condition: service_healthy  
> redis:  
> condition: service_healthy  
> networks: \[internal\]  
>   
> postgres:  
> image: postgres:16  
> restart: unless-stopped  
> environment:  
> POSTGRES_DB: image_platform  
> POSTGRES_USER: imageapp  
> POSTGRES_PASSWORD: \${DB_PASSWORD:?required}  
> volumes:  
> - postgres_data:/var/lib/postgresql/data  
> healthcheck:  
> test: \["CMD-SHELL", "pg_isready -U imageapp -d image_platform"\]  
> interval: 10s  
> timeout: 5s  
> retries: 5  
> networks: \[internal\]  
>   
> redis:  
> image: redis:7-alpine  
> restart: unless-stopped  
> command: \["redis-server", "--appendonly", "yes"\]  
> volumes:  
> - redis_data:/data  
> healthcheck:  
> test: \["CMD", "redis-cli", "ping"\]  
> interval: 10s  
> timeout: 3s  
> retries: 5  
> networks: \[internal\]  
>   
> volumes:  
> postgres_data:  
> redis_data:  
>   
> networks:  
> internal:  
> driver: bridge

Compose trên là điểm khởi đầu local/staging, không phải cấu hình HA production. Production cần managed PostgreSQL với backup/PITR, Redis managed/HA, private networking, TLS ingress, secret manager, object storage, monitoring và nhiều worker node.

## 7.1 .env.example

> APP_ENV=local  
> DATABASE_URL=postgresql+asyncpg://imageapp:CHANGE_ME@postgres:5432/image_platform  
> DB_PASSWORD=CHANGE_ME  
> REDIS_URL=redis://redis:6379/0  
>   
> JWT_SECRET=REPLACE_WITH_RANDOM_SECRET  
> JWT_ACCESS_TTL_MINUTES=15  
> JWT_REFRESH_TTL_DAYS=30  
>   
> IMAGE_PROVIDER=mock  
> FLOW_BROWSER_ENABLED=false  
>   
> S3_ENDPOINT=https://ACCOUNT_ID.r2.cloudflarestorage.com  
> S3_BUCKET=generated-images  
> S3_ACCESS_KEY_ID=REPLACE_ME  
> S3_SECRET_ACCESS_KEY=REPLACE_ME

# 8. Security requirements

- Không commit secret; production secrets dùng cloud secret manager/KMS.

- Password hash bằng Argon2id; access token ngắn hạn; refresh token rotation và revoke.

- MFA cho admin; RBAC và audit log cho mọi thao tác nhạy cảm.

- Rate limit theo user, IP và endpoint; prompt length/content validation.

- Private bucket; signed URL thời hạn ngắn; kiểm tra ownership trước khi cấp URL.

- Worker chạy non-root, container sandbox, filesystem hạn chế, egress allowlist.

- Browser worker không truy cập cloud metadata endpoint, database port hoặc mạng quản trị.

- Không log password, cookies, access token, session storage hoặc prompt nhạy cảm.

- Encrypt data at rest/in transit; backup encryption và restore drill.

- Có data retention policy và quy trình xóa user/assets.

- Thanh toán chỉ cộng credit sau webhook/provider verification; chống replay webhook.

- Không tự động vượt CAPTCHA, 2FA, quota, rate limit hoặc bot protections.

# 9. Cloud production topology

| **Layer**     | **MVP/Pilot**                    | **Production scale**                                  |
|---------------|----------------------------------|-------------------------------------------------------|
| Edge          | Cloudflare WAF/CDN               | Cloudflare WAF/CDN + rate limits + TLS                |
| API           | 2 API replicas hoặc 1 node pilot | Load balancer + autoscaled API replicas               |
| Database      | Managed PostgreSQL               | Managed HA PostgreSQL, PITR, read replica nếu cần     |
| Queue         | Managed Redis                    | Managed Redis HA; evaluate broker semantics           |
| Workers       | 1–2 worker nodes                 | Separate autoscaled worker pool; provider quota-aware |
| Storage       | Private R2/S3 bucket             | R2/S3 + lifecycle + CDN signed delivery               |
| Observability | Sentry + basic metrics           | Prometheus/Grafana, centralized logs, alerting        |
| Secrets       | Local env only for dev           | Cloud secret manager + KMS                            |

Không ước tính số browser worker bằng số user đăng ký. Worker capacity phải dựa trên measured job duration, RAM/browser, provider concurrency/quota và error rate. Không tăng worker để né giới hạn nhà cung cấp.

# 10. Capacity planning & SLO

Mục tiêu 'large' hiện được hiểu là \>5.000 registered users, không đồng nghĩa 5.000 concurrent users. Trước khi đặt số worker hoặc cam kết throughput, chạy load test và đo:

- Peak concurrent active users và create-job requests/minute.

- Provider generation latency P50/P95/P99.

- Worker memory per browser session và cold-start time.

- Queue wait time, retry rate, stuck-job rate.

- Provider rejection/rate-limit/auth-required rate.

- Image size, storage growth, egress cost và CDN cache hit rate.

SLO ban đầu cần được xác định sau pilot. Không công bố ETA cho user nếu chưa có dữ liệu; có thể hiển thị trạng thái queue và thời gian chờ ước tính từ metrics thực tế.

# 11. Observability & operations

| **Metric/Alert**                 | **Mục đích**                   |
|----------------------------------|--------------------------------|
| queue_depth / oldest_job_age     | Phát hiện queue backlog        |
| job_success_rate / failure_rate  | Theo dõi chất lượng provider   |
| job_duration_p50/p95             | Theo dõi thời gian tạo ảnh     |
| worker_heartbeat_age             | Phát hiện worker chết          |
| provider_auth_required_count     | Phát hiện session cần xác thực |
| provider_rate_limited_count      | Phát hiện quota/rate limit     |
| credit_ledger_reconciliation     | Đối soát ví và giao dịch       |
| storage_upload_failure_rate      | Phát hiện lỗi lưu ảnh          |
| db_connections / replication_lag | Theo dõi database              |

Stack đề xuất: Prometheus + Grafana, centralized logs (Loki hoặc dịch vụ cloud), Sentry cho lỗi app/backend, Alertmanager hoặc cloud alerts. Correlation ID phải đi xuyên suốt API -\> job -\> attempt -\> provider -\> asset.

# 12. CI/CD, migrations và release

1.  Pull request: format, lint, type-check, unit tests, API tests, dependency/security scan.

2.  Build immutable Docker images và Flutter artifacts; gắn version/commit SHA.

3.  Deploy staging; chạy Alembic migrations theo quy trình backward-compatible.

4.  Integration tests với MockImageProvider; test reserve/settle/refund và outbox.

5.  Load test queue/API/storage; không load-test Google Flow trái điều khoản hoặc vượt quota.

6.  Canary deploy backend; theo dõi error rate, latency và credit reconciliation.

7.  Promote production; giữ rollback image và migration strategy.

8.  Backup/restore drill định kỳ; kiểm tra alert và runbook.

# 13. Roadmap triển khai

| **Milestone**             | **Deliverable**                                                 | **Exit criteria**                                              |
|---------------------------|-----------------------------------------------------------------|----------------------------------------------------------------|
| M0 – Discovery            | Xác minh provider, legal/ToS, pricing, quota, account ownership | Có quyết định provider được phép dùng; nếu chưa, chỉ dùng mock |
| M1 – Foundation           | Monorepo, FastAPI, config, Docker, PostgreSQL, Alembic          | Health checks, migrations, CI pass                             |
| M2 – Auth & Wallet        | Register/login/refresh, wallet, ledger, credit reserve/settle   | Concurrency tests không tạo số dư âm/trừ trùng                 |
| M3 – Jobs & Queue         | Job API, outbox publisher, Celery worker, state machine         | Job không mất; retry/idempotency tests pass                    |
| M4 – Flutter MVP          | Auth, prompt, job status, gallery, wallet                       | E2E test với mock provider trên iOS/Android                    |
| M5 – Storage & Admin      | R2/S3, signed URL, admin, audit                                 | Ownership/security tests pass                                  |
| M6 – Provider integration | Approved provider adapter; Flow browser chỉ nếu được phép       | Provider conformance tests và policy review pass               |
| M7 – Hardening            | Monitoring, load/security tests, backup/restore                 | Runbook, SLO, recovery tests pass                              |
| M8 – Pilot/Production     | Canary, limited rollout, scaling                                | No critical incidents; reconciliation and rollback proven      |

Lịch 8 tuần chỉ là kế hoạch tham khảo. M0 là cổng chặn: không triển khai browser automation thương mại trước khi xác minh quyền và điều khoản.

# 14. Acceptance criteria tổng thể

- Một user có thể đăng nhập Flutter, tạo job bằng prompt và nhận job_id.

- Job được xử lý bất đồng bộ; mobile xem được trạng thái và kết quả.

- Mock provider tạo fixture image để kiểm thử toàn bộ luồng.

- Không thể đọc job/asset của user khác.

- Không thể tiêu credit âm; idempotent request không tạo job hoặc debit trùng.

- Worker crash không làm mất job; lease recovery hoạt động.

- Job thất bại được release credit theo policy; job thành công settle đúng một lần.

- Ảnh được lưu private; signed URL hết hạn; xóa asset thực sự xóa object theo policy.

- Admin actions có RBAC, MFA và audit log.

- Monitoring cảnh báo queue backlog, worker down, provider failure và reconciliation mismatch.

- Provider thật chỉ được bật khi có quyền sử dụng và test tích hợp đạt.

# 15. Rủi ro, assumptions và quyết định còn mở

| **Vấn đề**                    | **Trạng thái / hành động**                                                |
|-------------------------------|---------------------------------------------------------------------------|
| Google Flow public API        | Chưa xác minh có API phù hợp cho text-to-image; không giả định có.        |
| Playwright/Browser automation | Chưa xác minh được cho SaaS thương mại; feature flag OFF mặc định.        |
| Shared account pool           | Chỉ dùng tài khoản thuộc quyền kiểm soát/ủy quyền hợp lệ; không né quota. |
| Pricing per image             | Chưa chốt giá credit; cấu hình server-side theo provider cost.            |
| Payment rails                 | Chưa chốt provider; phải kiểm tra Apple/Google in-app purchase policy.    |
| Target load                   | 5.000+ registered users; cần concurrency và throughput target thực tế.    |
| Cloud vendor/region           | Chưa chốt; chọn region gần người dùng và đánh giá data residency/cost.    |
| Retention                     | Chưa chốt thời hạn lưu prompt, ảnh, log; cần privacy policy.              |

# 16. Việc Claude Code phải làm đầu tiên

9.  Tạo docs/PROJECT_SPEC.md từ tài liệu này và README triển khai.

10. Tạo monorepo skeleton đúng cấu trúc đã đề xuất.

11. Tạo backend FastAPI với /health/live và /health/ready.

12. Tạo Alembic migration cho users, wallets, image_jobs, credit_transactions, image_assets, outbox_events.

13. Tạo Docker Compose local với PostgreSQL, Redis, API và worker.

14. Tạo MockImageProvider trả fixture image; không bật Flow browser.

15. Viết tests cho create job, idempotency, reserve/settle/release credits, ownership và outbox.

16. Tạo OpenAPI spec và generate/maintain typed API client cho Flutter.

17. Chỉ sau khi milestone trên pass mới triển khai Flutter MVP.

18. Dừng và yêu cầu quyết định của chủ dự án trước mọi tích hợp Google Flow browser thật.

**END OF TECHNICAL HANDOFF**
