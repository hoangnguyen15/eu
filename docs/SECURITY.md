# Security

Yêu cầu đầy đủ: `PROJECT_SPEC.md` §8. Điểm đã được cưỡng chế bằng code ở M1:

- `Settings` từ chối khởi động ở staging/production nếu `JWT_SECRET` là placeholder/<32 ký tự, `DATABASE_URL` còn `CHANGE_ME`, hoặc (production) `IMAGE_PROVIDER=mock` / `FLOW_BROWSER_ENABLED=true`.
- `/health/ready` chỉ trả tên loại lỗi, không trả chi tiết kết nối.
- Container chạy non-root; Postgres/Redis không publish port ra host; API bind `127.0.0.1`.
- `.env` bị gitignore; chỉ `.env.example` được commit.
- Ledger `credit_transactions` append-only ở tầng DB (trigger).

Báo lỗi bảo mật: liên hệ chủ dự án (chưa thiết lập kênh riêng).
