# Cài đặt máy chủ trên macOS

Hướng dẫn dựng **máy chủ trung tâm** (API, scheduler, gateway mTLS, PostgreSQL, Redis, MinIO) trên một máy Mac trong mạng LAN. Các máy trạm Windows và điện thoại kết nối tới Mac này qua địa chỉ IP LAN.

```
 Điện thoại / trình duyệt ──http://<IP-Mac>:8000/app/───┐
 Quản trị viên ────────────http://<IP-Mac>:8000/admin/──┤
                                                         ▼
                                              ┌─────── Mac (Docker Desktop) ───────┐
 Máy trạm Windows ──https://<IP-Mac>:8443 ──► │ agent-gateway (mTLS) → backend      │
   (FlowAgent)     ──http://<IP-Mac>:9000 ──► │ minio (ảnh, signed URL)            │
                                              │ scheduler · postgres · redis       │
                                              └────────────────────────────────────┘
```

## 1. Yêu cầu
* macOS 13 (Ventura) trở lên, Apple Silicon (M1/M2/M3/M4) hoặc Intel; tối thiểu 8 GB RAM, 20 GB ổ trống.
* **Docker Desktop for Mac** 4.30+ (https://www.docker.com/products/docker-desktop/). Trong *Settings → Resources* cấp ít nhất 4 GB RAM, 2 CPU.
* Git (cài cùng Xcode Command Line Tools: `xcode-select --install`).
* Mac nên có **IP cố định** trong LAN (đặt DHCP reservation trên router), vì máy trạm và điện thoại dùng IP này.

Tất cả image dùng trong compose (python:3.12-slim, postgres:16-alpine, redis:7-alpine, nginx:1.27-alpine, pgsty/minio, .NET 8) đều có bản `arm64`, chạy native trên Apple Silicon.

## 2. Cài đặt (khoảng 10 phút)
```bash
git clone https://github.com/hoangnguyen15/eu.git flow-platform
cd flow-platform

# Tạo .env: mật khẩu ngẫu nhiên + tự phát hiện IP LAN (en0/en1)
./scripts/init-env.sh --admin-email ban@congty.com
#   → in ra PUBLIC_HOST=192.168.x.y và mật khẩu admin (lưu trong .env)
#   Nếu dò sai IP: ./scripts/init-env.sh --force --host 192.168.1.10

# Build + khởi động + tạo tài khoản admin
./scripts/bootstrap.sh
```
Khi xong, script in ra các địa chỉ:

| Trang | Địa chỉ |
|---|---|
| Web client (demo mobile) | `http://<IP-Mac>:8000/app/` (mở trên điện thoại cùng Wi-Fi) |
| Trang quản trị | `http://<IP-Mac>:8000/admin/` |
| Gateway cho máy trạm | `https://<IP-Mac>:8443` |

Kiểm tra sức khoẻ: `curl http://<IP-Mac>:8000/api/v1/health` → `"status":"ok"`.

## 3. Cho phép kết nối từ LAN
* *System Settings → Network → Firewall*: nếu bật firewall, cho phép **Docker** nhận kết nối đến (macOS sẽ hỏi lần đầu, chọn *Allow*).
* Các cổng cần mở trong LAN: **8000** (API + web), **8443** (gateway máy trạm), **9000** (tải ảnh qua signed URL).
* Không mở các cổng này ra Internet nếu chưa đặt TLS/reverse proxy (xem `docs/DEPLOYMENT.md`).

## 4. Giữ máy chủ luôn chạy
* *Docker Desktop → Settings → General*: bật **Start Docker Desktop when you sign in**. Các container có `restart: unless-stopped` nên tự chạy lại.
* *System Settings → Energy* (hoặc *Battery → Options*): bật **Prevent automatic sleeping when the display is off** và **Wake for network access**. Với MacBook, cắm sạc thường xuyên.
* Có thể giữ máy thức bằng: `caffeinate -dims &`.

## 5. Thêm máy trạm Windows
1. Trên Mac, build gói cài đặt (chỉ cần Docker, không cần Windows):
   ```bash
   ./scripts/package-agent.sh      # → dist/FlowAgent-<version>-win-x64.zip (+ .sha256)
   ```
2. Vào **Trang quản trị → Phiên đăng nhập**: thêm tài khoản provider mà máy trạm sẽ dùng, ghi rõ căn cứ được phép sử dụng.
3. **Trang quản trị → Thêm máy trạm**: nhập tên máy và số slot, chọn tài khoản + profile, rồi bấm *Tạo cấu hình cài đặt*. Trình duyệt sẽ tải về `flowagent-config-<tên>.json` (token dùng một lần, hết hạn sau 24 giờ).
4. Trên máy trạm: giải nén zip, chép file cấu hình vào cùng thư mục và đổi tên thành `flowagent-config.json`. Sau đó chuột phải `Install-FlowAgent.cmd` → *Run as administrator*. Chi tiết xem `README-CAI-DAT.txt` trong gói.
5. Máy trạm sẽ hiện **ONLINE** trong mục *Máy trạm* sau vài giây.

> Đăng nhập Google Flow/provider luôn do người vận hành tự làm trên chính máy trạm. Hệ thống không lưu mật khẩu, cookie hay token. Sau khi đăng nhập, bấm *Kiểm tra lại phiên* trong trang quản trị.

## 6. Chạy thử toàn bộ quy trình ngay trên Mac (không cần máy Windows)
```bash
./scripts/e2e.sh          # chạy FlowAgent (.NET) trong container + kiểm tra ~30 điểm
./scripts/resilience.sh   # thử mất kết nối gateway, restart / kill agent
```
`init-env.sh` đã đặt `AGENT_NETWORK_MODE=bridge`, nên agent demo kết nối tới `https://<IP-Mac>:8443` giống hệt máy trạm thật. Sau đó mở `http://<IP-Mac>:8000/app/` trên điện thoại để tạo ảnh và bấm *Kiểm tra tải*.

## 7. Vận hành
| Việc | Lệnh |
|---|---|
| Xem trạng thái | `docker compose ps` |
| Xem log | `docker compose logs -f backend scheduler agent-gateway` |
| Dừng / chạy lại | `docker compose stop` / `docker compose up -d` |
| Cập nhật phiên bản | `git pull && ./scripts/bootstrap.sh` (migration tự chạy) |
| Sao lưu DB | `./scripts/backup-db.sh ~/flow-backups` (đặt lịch bằng `crontab -e`, ví dụ `0 2 * * *`) |
| Khôi phục DB | `docker compose stop backend scheduler && ./scripts/restore-db.sh <file>` |

**Đổi IP của Mac**: `./scripts/init-env.sh --set-host <IP-mới>` (giữ nguyên mọi mật khẩu) → `./scripts/bootstrap.sh`. Chứng chỉ gateway được cấp lại cho IP mới với CA cũ, nên máy trạm đã cài không phải enroll lại — chỉ cần sửa `ServerBaseUrl` trong `C:\Program Files\FlowAgent\appsettings.json` của từng máy trạm và khởi động lại dịch vụ (`sc stop FlowAgent && sc start FlowAgent`). Vì vậy nên đặt IP cố định từ đầu.

## 8. Xử lý sự cố
| Triệu chứng | Cách xử lý |
|---|---|
| Điện thoại không mở được trang | Cùng mạng Wi-Fi? Firewall cho phép Docker? Thử `http://<IP-Mac>:8000/api/v1/health` |
| Ảnh không hiện trên điện thoại | `PUBLIC_HOST` trong `.env` phải là IP LAN (không phải `localhost`); cổng 9000 phải mở |
| Máy trạm không lên ONLINE | Từ máy trạm, chạy PowerShell: `Test-NetConnection <IP-Mac> -Port 8443`; xem Event Viewer (nguồn FlowAgent); token còn hạn? |
| Máy trạm báo lỗi chứng chỉ | IP của Mac đã đổi: làm theo mục "Đổi IP" ở trên |
| Thông lượng thấp | Mỗi tài khoản provider bị giới hạn `MAX_CONCURRENT_JOBS_PER_ACCOUNT` job đồng thời (mặc định 2). Muốn tăng thông lượng, thêm tài khoản/máy trạm; chỉ nâng giới hạn khi provider cho phép |
