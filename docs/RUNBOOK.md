# Runbook (M1)

| Triệu chứng | Kiểm tra |
|-------------|----------|
| `/health/ready` = 503 | Đọc `checks` trong body: `database` / `redis`. Kiểm tra container healthy, `DB_PASSWORD`, `REDIS_URL` |
| `migrate` thoát lỗi | `docker compose logs migrate`; chạy tay `alembic current` / `alembic upgrade head` |
| API không khởi động, lỗi `JWT_SECRET`/`DATABASE_URL` | Validation môi trường trong `app/core/config.py` — đặt giá trị thật |
| Worker không nhận task | `celery -A celery_app inspect ping`; kiểm tra `REDIS_URL` |

Mục backup/restore, alert, SLO: bổ sung ở M7.
