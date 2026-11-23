# Backend (FastAPI) — Milestone 1: Foundation

Gồm: FastAPI app factory, `/health/live` + `/health/ready`, cấu hình `pydantic-settings` có validation theo môi trường, JSON logging + `X-Request-ID`, Alembic baseline (spec §4.1) kèm ledger append-only.

## Chạy local (không Docker)

```bash
cd services/backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export DATABASE_URL=postgresql+asyncpg://USER:PW@localhost:5432/image_platform
export REDIS_URL=redis://localhost:6379/0
alembic upgrade head
uvicorn app.main:app --reload
curl localhost:8000/health/live ; curl localhost:8000/health/ready
```

## Docker Compose (từ gốc repo)

```bash
cp .env.example .env   # đổi DB_PASSWORD, JWT_SECRET
docker compose -f infrastructure/docker-compose.yml --env-file .env up --build
```

Service `migrate` chạy `alembic upgrade head` rồi mới khởi động `api`.

## Test

```bash
pytest -q                                   # unit: health, config (không cần DB)
# Test migration/DB thật — DROP SCHEMA public trên DB này, dùng DB bỏ đi:
TEST_DATABASE_URL=postgresql+asyncpg://USER:PW@localhost:5432/image_test \
TEST_REDIS_URL=redis://localhost:6379/0 pytest -q
ruff check . && ruff format --check .
```

Migration:  `alembic upgrade head` / `alembic downgrade base` (cần biến `DATABASE_URL`).

## Phạm vi ngoài M1
Auth, wallet, job API, outbox publisher, provider, storage (M2–M6). Bảng `payments`, `credit_packages`, `refresh_tokens` chưa có — xem `docs/ARCHITECTURE_AND_RISKS.md`.
