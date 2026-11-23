# AI Image Generation Platform

Flutter + FastAPI + PostgreSQL + Redis/Celery. Spec: [`docs/PROJECT_SPEC.md`](docs/PROJECT_SPEC.md) · Rủi ro & assumptions: [`docs/ARCHITECTURE_AND_RISKS.md`](docs/ARCHITECTURE_AND_RISKS.md).

| Path | Nội dung | Trạng thái |
|------|----------|-----------|
| `services/backend` | FastAPI, Alembic | **M1 xong** (xem README ở đó) |
| `services/worker` | Celery worker, provider adapters | Skeleton (M3) |
| `apps/mobile`, `apps/admin` | Flutter / Next.js | Chưa bắt đầu (M4/M5) |
| `infrastructure` | Docker Compose, monitoring, terraform | Compose local (M1) |

**Chưa có** tích hợp Google Flow; `FLOW_BROWSER_ENABLED=false` và bị từ chối ở production cho đến khi hoàn tất M0.
